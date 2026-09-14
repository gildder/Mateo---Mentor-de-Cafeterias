"""Application use cases; repository dependency is injected through a port."""

import hashlib
from uuid import uuid4

from mateo.calculators import calculate
from mateo.contracts import now
from mateo.core import PROFILE_FIELDS, PROJECT_FIELDS, DomainError, apply_fact_changes, compact_context, empty_facts, get_next_actions, resolve_fact
from mateo.repository import Repository, body_digest


def uid(prefix):
    return f"{prefix}-{uuid4().hex}"


class CRM:
    def __init__(self, repository: Repository):
        self.repository = repository

    def read(self, owner: str, client_id: str, kind="context", record_id=None):
        with self.repository.transaction() as tx:
            tx.authorize(client_id, owner)
            if record_id:
                return tx.get(client_id, record_id, kind)
            if kind == "context":
                return compact_context(tx.snapshot(client_id))
            if kind == "export":
                return tx.snapshot(client_id)
            if kind == "agenda":
                projects = tx.all(client_id, "project")
                from datetime import date
                opening = projects[-1].get("opening_date") if projects else None
                return get_next_actions(tx.all(client_id, "action"), now().date(), date.fromisoformat(opening) if opening else None)
            return tx.all(client_id, kind)

    def execute(self, owner: str, key: str, operation: str, client_id: str | None, payload: dict):
        digest = body_digest(operation, client_id, payload)
        scoped_key = hashlib.sha256(f"{owner}:{key}".encode()).hexdigest()
        with self.repository.transaction() as tx:
            if client_id:
                tx.authorize(client_id, owner)
            replay = tx.replay(scoped_key, owner, digest)
            if replay is not None:
                return replay
            if operation == "create_client":
                client_id = tx.next_id("client")
                tx.create_client(client_id, owner)
                facts, _ = apply_fact_changes(empty_facts(PROFILE_FIELDS), payload["facts"])
                tx.put(client_id, "profile", f"PROFILE-{client_id}", {"client_id": client_id, "facts": facts})
                data = {"client_id": client_id}
            else:
                if not client_id:
                    raise DomainError("client_id_required", 422)
                data = self._operate(tx, owner, client_id, operation, payload)
            tx.log(client_id, uid("AUD"), {"operation": operation, "actor": owner, "timestamp": now().isoformat(), "record_id": data.get("record_id")})
            revision = tx.enqueue(client_id)
            response = {"status": "success", "persisted": True, "sync_status": "PENDING", "client_id": client_id, "revision": revision, "data": data}
            tx.remember(scoped_key, owner, client_id, digest, response)
            return response

    def _updates(self, tx, client_id, target, updates):
        kind = "profile" if target.startswith("PROFILE-") else "project"
        data = tx.get(client_id, target, kind)
        facts, conflicts = apply_fact_changes(data["facts"], updates)
        data["facts"] = facts
        tx.put(client_id, kind, target, data)
        ids = []
        for conflict in conflicts:
            identifier = uid("CON")
            tx.put(client_id, "conflict", identifier, {**conflict, "conflict_id": identifier, "target_id": target, "client_id": client_id, "created_at": now().isoformat()})
            ids.append(identifier)
        return ids

    def _related(self, tx, client_id, payload):
        if payload.get("session_id"):
            tx.get(client_id, payload["session_id"], "session")
        if payload.get("dependency"):
            tx.get(client_id, payload["dependency"], "action")

    def _record(self, tx, client_id, kind, payload):
        self._related(tx, client_id, payload)
        prefixes = {"action": "ACT", "decision": "DEC", "risk": "RSK", "research": "RES", "hypothesis": "HYP"}
        record_id = uid(prefixes[kind])
        data = {**payload, f"{kind}_id": record_id, "client_id": client_id, "created_at": now().isoformat()}
        if kind == "action":
            data["completed_at"] = now().isoformat() if data["status"] == "COMPLETED" else None
        if kind == "hypothesis":
            data["status"] = "OPEN"
        tx.put(client_id, kind, record_id, data)
        return data

    def _operate(self, tx, owner, client_id, operation, p):
        if operation == "create_project":
            if tx.all(client_id, "project"):
                raise DomainError("one_active_project_per_client")
            record_id = uid("PRJ")
            facts, _ = apply_fact_changes(empty_facts(PROJECT_FIELDS), p["facts"])
            data = {**p, "facts": facts, "client_id": client_id, "project_id": record_id}
            tx.put(client_id, "project", record_id, data)
            return data
        if operation in {"patch_profile", "patch_project"}:
            target = f"PROFILE-{client_id}"
            if operation == "patch_project":
                projects = tx.all(client_id, "project")
                if not projects:
                    raise DomainError("project_not_found", 404)
                target = projects[0]["project_id"]
            return {"record_id": target, "conflicts": self._updates(tx, client_id, target, p["facts"])}
        if operation == "start_session":
            session_id = tx.next_id("session")
            data = {**p, "session_id": session_id, "status": "OPEN", "created_at": now().isoformat()}
            tx.put(client_id, "session", session_id, data)
            return {**data, "context": compact_context(tx.snapshot(client_id))}
        if operation == "close_session":
            old = tx.get(client_id, p["session_id"], "session")
            digest = body_digest(operation, client_id, p)
            if old["status"] == "CLOSED":
                if old["close_digest"] != digest:
                    raise DomainError("session_already_closed")
                return {"session_id": p["session_id"], "already_closed": True}
            conflicts = self._updates(tx, client_id, f"PROFILE-{client_id}", p["new_confirmed_facts"] + p["updated_confirmed_facts"])
            for kind in ("action", "decision", "risk", "hypothesis", "research"):
                key = "hypotheses" if kind == "hypothesis" else "research" if kind == "research" else kind + "s"
                for item in p[key]:
                    if item.get("session_id") not in (None, p["session_id"]):
                        raise DomainError("nested_session_mismatch", 422)
                    self._record(tx, client_id, kind, {**item, "session_id": p["session_id"]})
            for hypothesis_id in p["resolved_hypotheses"]:
                hypothesis = tx.get(client_id, hypothesis_id, "hypothesis")
                hypothesis["status"] = "RESOLVED"
                tx.put(client_id, "hypothesis", hypothesis_id, hypothesis)
            tx.put(client_id, "session", p["session_id"], {**old, **p, "chat_url": p["chat_url"] or old.get("chat_url"), "status": "CLOSED", "closed_at": now().isoformat(), "close_digest": digest, "conflicts": conflicts})
            return {"session_id": p["session_id"], "conflicts": conflicts}
        if operation.startswith("create_") and operation[7:] in {"action", "decision", "risk", "research"}:
            return self._record(tx, client_id, operation[7:], p)
        if operation == "action_state":
            action = tx.get(client_id, p["action_id"], "action")
            if p["status"] == "COMPLETED" and action.get("dependency"):
                dependency = tx.get(client_id, action["dependency"], "action")
                if dependency["status"] != "COMPLETED":
                    raise DomainError("dependency_incomplete")
            action.update(status=p["status"], completed_at=now().isoformat() if p["status"] == "COMPLETED" else None)
            tx.put(client_id, "action", p["action_id"], action)
            return action
        if operation == "resolve_conflict":
            conflict = tx.get(client_id, p["conflict_id"], "conflict")
            target = conflict["target_id"]
            kind = "profile" if target.startswith("PROFILE-") else "project"
            data = tx.get(client_id, target, kind)
            data["facts"] = resolve_fact(data["facts"], conflict, p["accept"])
            tx.put(client_id, kind, target, data)
            conflict.update(requires_confirmation=False, accepted=p["accept"], resolved_at=now().isoformat(), resolved_by=owner)
            tx.put(client_id, "conflict", p["conflict_id"], conflict)
            return conflict
        if operation == "calculate":
            return calculate(p["operation"], p["inputs"], p["currency"], p["assumptions"])
        raise DomainError("unsupported_operation", 422)
