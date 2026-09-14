"""SQL adapter. Serialized writes trade throughput for deterministic MVP behavior."""

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy import JSON, Column, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint, create_engine, event, insert, select, update

from mateo.core import DomainError

metadata = MetaData()
clients = Table("clients", metadata, Column("client_id", String, primary_key=True), Column("owner", String, nullable=False), Column("revision", Integer, nullable=False, default=0))
records = Table("records", metadata, Column("record_id", String, primary_key=True), Column("client_id", String, ForeignKey("clients.client_id", ondelete="CASCADE"), nullable=False), Column("kind", String, nullable=False), Column("data", JSON, nullable=False))
audit = Table("audit", metadata, Column("audit_id", String, primary_key=True), Column("client_id", String, ForeignKey("clients.client_id", ondelete="CASCADE"), nullable=False), Column("data", JSON, nullable=False))
outbox = Table("outbox", metadata, Column("outbox_id", Integer, primary_key=True, autoincrement=True), Column("client_id", String, ForeignKey("clients.client_id", ondelete="CASCADE"), nullable=False), Column("revision", Integer, nullable=False), Column("snapshot", JSON, nullable=False), Column("status", String, nullable=False), Column("attempts", Integer, nullable=False, default=0), Column("error_code", String), UniqueConstraint("client_id", "revision"))
idempotency = Table("idempotency", metadata, Column("key", String, primary_key=True), Column("owner", String, nullable=False), Column("client_id", String), Column("digest", String, nullable=False), Column("response", JSON, nullable=False))
counters = Table("counters", metadata, Column("name", String, primary_key=True), Column("value", Integer, nullable=False))
mutex = Table("mutex", metadata, Column("id", Integer, primary_key=True))
reservations = Table("provider_reservations", metadata, Column("path", Text, primary_key=True), Column("remote_id", String, nullable=False))
tokens = Table("tokens", metadata, Column("token_hash", String, primary_key=True), Column("owner", String, nullable=False), Column("role", String, nullable=False))


class Repository(Protocol):
    def transaction(self): ...


class SQLRepository:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 60} if url.startswith("sqlite") else {}, pool_pre_ping=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def configure(dbapi, _):
                dbapi.isolation_level = None
                dbapi.execute("PRAGMA foreign_keys=ON")
                dbapi.execute("PRAGMA journal_mode=WAL")
            @event.listens_for(self.engine, "begin")
            def begin(conn):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
        metadata.create_all(self.engine)
        # Single-writer initialization is required before starting production replicas.
        with self.engine.begin() as conn:
            if not conn.execute(select(mutex)).first():
                conn.execute(insert(mutex).values(id=1))

    @contextmanager
    def transaction(self):
        with self.engine.begin() as conn:
            conn.execute(select(mutex).where(mutex.c.id == 1).with_for_update())
            yield Transaction(conn)


class Transaction:
    def __init__(self, conn):
        self.conn = conn

    def next_id(self, kind: str) -> str:
        year = datetime.now(timezone.utc).year
        name = f"client-{year}" if kind == "client" else kind
        value = self.conn.execute(select(counters.c.value).where(counters.c.name == name)).scalar_one_or_none()
        value = (value or 0) + 1
        if value > 999999:
            raise DomainError("identifier_space_exhausted", 503)
        if value == 1:
            self.conn.execute(insert(counters).values(name=name, value=value))
        else:
            self.conn.execute(update(counters).where(counters.c.name == name).values(value=value))
        return f"MT-{year}-{value:06d}" if kind == "client" else f"SES-{value:06d}"

    def authorize(self, client_id: str, owner: str):
        row = self.conn.execute(select(clients).where(clients.c.client_id == client_id, clients.c.owner == owner)).mappings().first()
        if not row:
            raise DomainError("client_not_found", 404)
        return dict(row)

    def create_client(self, client_id: str, owner: str):
        self.conn.execute(insert(clients).values(client_id=client_id, owner=owner, revision=0))

    def list_clients(self, owner: str):
        return [dict(row) for row in self.conn.execute(select(clients.c.client_id, clients.c.revision).where(clients.c.owner == owner)).mappings()]

    def put(self, client_id: str, kind: str, record_id: str, data: dict):
        existing = self.conn.execute(select(records.c.client_id).where(records.c.record_id == record_id)).scalar_one_or_none()
        if existing is not None and existing != client_id:
            raise DomainError("record_not_found", 404)
        if existing:
            self.conn.execute(update(records).where(records.c.record_id == record_id).values(data=data))
        else:
            self.conn.execute(insert(records).values(record_id=record_id, client_id=client_id, kind=kind, data=data))

    def get(self, client_id: str, record_id: str, kind: str | None = None):
        stmt = select(records.c.data).where(records.c.client_id == client_id, records.c.record_id == record_id)
        if kind:
            stmt = stmt.where(records.c.kind == kind)
        data = self.conn.execute(stmt).scalar_one_or_none()
        if data is None:
            raise DomainError("record_not_found", 404)
        return dict(data)

    def all(self, client_id: str, kind: str):
        return list(self.conn.execute(select(records.c.data).where(records.c.client_id == client_id, records.c.kind == kind).order_by(records.c.record_id)).scalars())

    def log(self, client_id: str, event_id: str, data: dict):
        self.conn.execute(insert(audit).values(audit_id=event_id, client_id=client_id, data=data))

    def snapshot(self, client_id: str):
        client = self.conn.execute(select(clients).where(clients.c.client_id == client_id)).mappings().one()
        return {"client_id": client_id, "revision": client["revision"], "records": [dict(row) for row in self.conn.execute(select(records).where(records.c.client_id == client_id).order_by(records.c.record_id)).mappings()], "audit": [dict(row) for row in self.conn.execute(select(audit).where(audit.c.client_id == client_id).order_by(audit.c.audit_id)).mappings()]}

    def enqueue(self, client_id: str):
        self.conn.execute(update(clients).where(clients.c.client_id == client_id).values(revision=clients.c.revision + 1))
        snapshot = self.snapshot(client_id)
        self.conn.execute(insert(outbox).values(client_id=client_id, revision=snapshot["revision"], snapshot=snapshot, status="PENDING", attempts=0))
        return snapshot["revision"]

    def replay(self, key: str, owner: str, digest: str):
        row = self.conn.execute(select(idempotency).where(idempotency.c.key == key, idempotency.c.owner == owner)).mappings().first()
        if row:
            if row["digest"] != digest:
                raise DomainError("idempotency_body_mismatch")
            return row["response"]

    def remember(self, key: str, owner: str, client_id: str, digest: str, response: dict):
        self.conn.execute(insert(idempotency).values(key=key, owner=owner, client_id=client_id, digest=digest, response=response))

    def pending(self, client_id: str):
        return [dict(r) for r in self.conn.execute(select(outbox).where(outbox.c.client_id == client_id, outbox.c.status != "SYNCED").order_by(outbox.c.revision)).mappings()]

    def mark_sync(self, outbox_id: int, status: str):
        self.conn.execute(update(outbox).where(outbox.c.outbox_id == outbox_id).values(status=status, attempts=outbox.c.attempts + 1, error_code="provider_unavailable" if status == "FAILED" else None))

    def reserve(self, path: str, generate):
        value = self.conn.execute(select(reservations.c.remote_id).where(reservations.c.path == path)).scalar_one_or_none()
        if not value:
            value = generate()
            self.conn.execute(insert(reservations).values(path=path, remote_id=value))
        return value

    def reservation_map(self):
        return dict(self.conn.execute(select(reservations)).all())

    def add_token(self, raw: str, owner: str, role="client"):
        self.conn.execute(insert(tokens).values(token_hash=hashlib.sha256(raw.encode()).hexdigest(), owner=owner, role=role))

    def principal(self, raw: str):
        row = self.conn.execute(select(tokens.c.owner, tokens.c.role).where(tokens.c.token_hash == hashlib.sha256(raw.encode()).hexdigest())).mappings().first()
        if not row:
            raise DomainError("unauthorized", 401)
        return dict(row)

def body_digest(operation: str, client_id: str | None, payload: dict):
    return hashlib.sha256(json.dumps([operation, client_id, payload], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
