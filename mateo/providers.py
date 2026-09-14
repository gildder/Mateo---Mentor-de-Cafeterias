"""Document/index adapters. Google imports are lazy and never needed in mock mode."""

import io
import json
import os
from pathlib import Path

from mateo.core import documents, index_rows


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def directory_paths(client_id: str):
    base = f"CLIENTES/{client_id}"
    return ["", "00_INDEX", "CLIENTES", "SISTEMA", base] + [f"{base}/{part}" for part in ("sesiones", "investigaciones", "documentos", "analisis")]


class MockGoogle:
    def __init__(self, root: Path):
        self.root = root

    def prepare(self, tx, snapshot):
        pass  # Local paths are deterministic and require no remote allocation.

    def sync(self, snapshot, reservations):
        drive = self.root / "drive" / "MATEO CRM"
        for folder in directory_paths(snapshot["client_id"]):
            (drive / folder).mkdir(parents=True, exist_ok=True)
        for path, content in documents(snapshot).items():
            atomic_write(drive / path, content)
        for tab, rows in index_rows(snapshot).items():
            path = self.root / "sheets" / "MATEO CRM — MASTER INDEX" / f"{tab}.json"
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            merged = {row[0]: row for row in current}
            merged.update({row[0]: row for row in rows})
            atomic_write(path, json.dumps(list(merged.values()), ensure_ascii=False, indent=2))


class GoogleWorkspace:
    """One application writer owns the workbook. Manual row reordering is unsupported."""
    def __init__(self, drive, sheets, spreadsheet_id: str, parent_id: str | None = None):
        self.drive, self.sheets = drive, sheets
        self.spreadsheet_id, self.parent_id = spreadsheet_id, parent_id

    @classmethod
    def from_environment(cls):
        import google.auth
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        scopes = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/spreadsheets"]
        # Authorized-user JSON or ADC (service account / Cloud Run identity). Never log credentials.
        oauth_file = os.getenv("GOOGLE_OAUTH_TOKEN_FILE")
        credentials = Credentials.from_authorized_user_file(oauth_file, scopes) if oauth_file else google.auth.default(scopes=scopes)[0]
        spreadsheet_id = os.environ["GOOGLE_SPREADSHEET_ID"]
        return cls(build("drive", "v3", credentials=credentials, cache_discovery=False), build("sheets", "v4", credentials=credentials, cache_discovery=False), spreadsheet_id, os.getenv("GOOGLE_DRIVE_PARENT_ID"))

    def prepare(self, tx, snapshot):
        # Reservations commit BEFORE any create request, so a timeout cannot lose the ID.
        paths = directory_paths(snapshot["client_id"]) + list(documents(snapshot))
        for path in paths:
            tx.reserve(path, lambda: self.drive.files().generateIds(count=1, space="drive", type="files").execute()["ids"][0])

    def _exists(self, remote_id):
        from googleapiclient.errors import HttpError
        try:
            return self.drive.files().get(fileId=remote_id, fields="id", supportsAllDrives=True).execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise

    def _put(self, path, ids, content=None):
        from googleapiclient.http import MediaIoBaseUpload
        identifier = ids[path]
        if content is None:
            mime = "application/vnd.google-apps.folder"
        else:
            mime = "application/json" if path.endswith(".json") else "text/markdown"
        parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
        parent = ids[parent_path] if path else self.parent_id
        body = {"id": identifier, "name": path.rsplit("/", 1)[-1] if path else "MATEO CRM", "mimeType": mime}
        if parent:
            body["parents"] = [parent]
        media = MediaIoBaseUpload(io.BytesIO(content.encode()), mimetype=mime, resumable=False) if content is not None else None
        if self._exists(identifier):
            if media:
                self.drive.files().update(fileId=identifier, media_body=media, fields="id", supportsAllDrives=True).execute()
            return
        self.drive.files().create(body=body, media_body=media, fields="id", supportsAllDrives=True).execute()

    def sync(self, snapshot, ids):
        for path in directory_paths(snapshot["client_id"]):
            self._put(path, ids)
        for path, content in documents(snapshot).items():
            self._put(path, ids, content)
        api = self.sheets.spreadsheets()
        existing = api.get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties.title").execute()
        titles = {s["properties"]["title"] for s in existing.get("sheets", [])}
        tabs = index_rows(snapshot)
        missing = [name for name in tabs if name not in titles]
        if missing:
            api.batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": [{"addSheet": {"properties": {"title": name}}} for name in missing]}).execute()
        for name, rows in tabs.items():
            old = api.values().get(spreadsheetId=self.spreadsheet_id, range=f"'{name}'!A:D").execute().get("values", [])
            positions = {str(row[0]): index + 1 for index, row in enumerate(old) if row}
            updates = [{"range": f"'{name}'!A1:D1", "values": [["record_id", "client_id", "data_json", "sync_status"]]}]
            next_row = max(len(old) + 1, 2)
            for row in rows:
                number = positions.get(row[0])
                if number is None:
                    number, next_row = next_row, next_row + 1
                updates.append({"range": f"'{name}'!A{number}:D{number}", "values": [row]})
            api.values().batchUpdate(spreadsheetId=self.spreadsheet_id, body={"valueInputOption": "RAW", "data": updates}).execute()


class SyncEngine:
    def __init__(self, repository, provider, max_attempts=5):
        self.repository, self.provider, self.max_attempts = repository, provider, max_attempts

    def sync(self, client_id, owner, retry=False):
        # Phase 1 reserves IDs durably; phase 2 serializes remote writes in revision order.
        try:
            with self.repository.transaction() as tx:
                tx.authorize(client_id, owner)
                for entry in tx.pending(client_id):
                    self.provider.prepare(tx, entry["snapshot"])
        except Exception:
            return "FAILED"
        with self.repository.transaction() as tx:
            tx.authorize(client_id, owner)
            for entry in tx.pending(client_id):
                if entry["attempts"] >= self.max_attempts and not retry:
                    return "FAILED"
                try:
                    self.provider.sync(entry["snapshot"], tx.reservation_map())
                except Exception:
                    tx.mark_sync(entry["outbox_id"], "FAILED")
                    return "FAILED"
                tx.mark_sync(entry["outbox_id"], "SYNCED")
        return "SYNCED"
