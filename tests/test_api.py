from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mateo.api import create_app
from mateo.providers import MockGoogle
from mateo.settings import Settings


def fact(value, status="CONFIRMED"):
    return {"value": value, "status": status, "source": "user_statement", "updated_at": "2026-09-10T20:00:00Z"}


@pytest.fixture
def app(tmp_path):
    app = create_app(Settings(f"sqlite:///{tmp_path / 'db.sqlite'}", tmp_path / "storage"))
    with app.state.repo.transaction() as tx:
        tx.add_token("fictional-token-a", "tenant-a", "admin")
        tx.add_token("fictional-token-b", "tenant-b", "admin")
        tx.add_token("fictional-client-token", "tenant-a", "client")
    yield app
    app.state.repo.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def headers(owner="a", key=None):
    return {"Authorization": f"Bearer fictional-token-{owner}", "Idempotency-Key": key or uuid4().hex}


def create(client, owner="a", facts=None):
    response = client.post("/gem/client/create", headers=headers(owner), json={"facts": [{"field": k, "fact": fact(v)} for k, v in (facts or {}).items()]})
    assert response.status_code == 200, response.text
    assert response.json()["persisted"]
    return response.json()["client_id"]


def test_auth_and_ownership(client):
    assert client.get("/clients").status_code == 401
    assert client.post("/clients", json={}, headers={"Authorization": "Bearer fictional-client-token", "Idempotency-Key": "test-key-123"}).status_code == 403
    a = create(client, facts={"name": "Laura", "city": "Madrid", "available_capital": 80000, "currency": "EUR"})
    b = create(client, "b", {"name": "Carlos", "city": "Bogotá", "available_capital": 35000, "currency": "USD"})
    response = client.get(f"/gem/client/{b}/context", headers=headers("b"))
    assert response.status_code == 200
    for forbidden in ["Laura", "Madrid", "80000", "EUR"]:
        assert forbidden not in response.text
    for path in [f"/gem/client/{a}/context", f"/clients/{a}/export", f"/clients/{a}/sessions"]:
        assert client.get(path, headers=headers("b")).status_code == 404
    assert client.post("/gem/client/resolve", json={"client_id": a}, headers=headers("b")).status_code == 404
    assert client.post(f"/gem/client/{a}/action", json={"title": "Leak"}, headers=headers("b")).status_code == 404


def test_e2e_session_project_sync(client, app):
    a = create(client, facts={"name": "Laura"})
    assert client.post(f"/clients/{a}/projects", json={"title": "Fictional coffee shop", "opening_date": "2027-01-01"}, headers=headers()).status_code == 200
    start = client.post("/gem/session/start", json={"client_id": a, "objective": "Validate location"}, headers=headers()).json()
    sid = start["data"]["session_id"]
    close = {"client_id": a, "session_id": sid, "summary": "Fictional session", "new_confirmed_facts": [{"field": "available_capital", "fact": fact(80000)}], "actions": [{"title": "Visit site", "priority": "HIGH", "due_date": "2026-09-01"}], "decisions": [{"title": "Compare rent"}], "risks": [{"title": "Lease risk"}], "hypotheses": [{"title": "Morning traffic"}], "research": [{"topic": "Rent", "question": "What is typical?", "sources": ["https://example.com"]}], "next_session_objective": "Review evidence"}
    result = client.post("/gem/session/close", json=close, headers=headers())
    assert result.status_code == 200, result.text
    assert result.json()["sync_status"] == "SYNCED"
    assert client.post("/gem/session/close", json=close, headers=headers()).status_code == 200
    assert len(client.get(f"/clients/{a}/actions", headers=headers()).json()) == 1
    assert client.post("/gem/session/close", json={**close, "summary": "Different"}, headers=headers()).status_code == 409
    context = client.get(f"/gem/client/{a}/context", headers=headers()).json()
    assert context["next_focus"] == "Review evidence"
    assert context["open_actions"][0]["overdue"]
    assert "history" not in context
    storage = app.state.sync.provider.root
    for suffix in [f"drive/MATEO CRM/CLIENTES/{a}/sesiones/{sid}.md", f"drive/MATEO CRM/CLIENTES/{a}/sesiones/{sid}.json", "sheets/MATEO CRM — MASTER INDEX/SESSIONS.json"]:
        assert (storage / suffix).exists()
    assert len(client.get(f"/clients/{a}/export", headers=headers()).json()["audit"]) >= 4


def test_fact_conflicts_research_patch(client):
    a = create(client, facts={"available_capital": 45000, "city": "Madrid"})
    patch = {"facts": [{"field": "available_capital", "fact": fact(60000)}]}
    result = client.patch(f"/clients/{a}/profile", json=patch, headers=headers()).json()
    conflict = result["data"]["conflicts"][0]
    profile = client.get(f"/clients/{a}/profile", headers=headers()).json()[0]["facts"]
    assert profile["available_capital"]["value"] == 45000
    assert profile["city"]["value"] == "Madrid"
    assert client.post(f"/clients/{a}/conflicts/{conflict}/resolve", json={"accept": True}, headers=headers()).status_code == 200
    assert client.post(f"/gem/client/{a}/research", json={"topic": "Capital", "question": "Market?", "findings": "999999"}, headers=headers()).status_code == 200
    assert client.get(f"/clients/{a}/profile", headers=headers()).json()[0]["facts"]["available_capital"]["value"] == 60000


def test_idempotency_concurrency(client, app):
    payload = {"facts": []}
    key = "concurrent-key-123"
    def call(_):
        return client.post("/clients", json=payload, headers=headers(key=key))
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(call, range(6)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len({r.json()["client_id"] for r in responses}) == 1
    assert client.post("/clients", json={"facts": [{"field": "name", "fact": fact("Other")}]}, headers=headers(key=key)).status_code == 409
    assert len(client.get("/clients", headers=headers()).json()) == 1


def test_nested_tenant_session_and_envelope(client):
    a, b = create(client), create(client, "b")
    sid = client.post("/gem/session/start", json={"client_id": a}, headers=headers()).json()["data"]["session_id"]
    assert client.post(f"/gem/client/{b}/action", json={"title": "bad", "session_id": sid}, headers=headers("b")).status_code == 404
    envelope = {"schema_version": "1.0", "operation": "create_action", "client_id": b, "source": "gemini_gem", "payload": {"title": "Do this"}, "metadata": {"timestamp": "2026-09-10T20:00:00Z"}}
    assert client.post("/gem/envelope", json=envelope, headers=headers("b")).status_code == 200
    assert client.post("/gem/envelope", json={**envelope, "client_id": a}, headers=headers("b")).status_code == 404
    assert client.post("/gem/envelope", json={**envelope, "payload": {"title": "X", "client_id": a}}, headers=headers("b")).status_code == 422


def test_outbox_failure_survives(client, app):
    class Failing(MockGoogle):
        def sync(self, snapshot, ids):
            super().sync(snapshot, ids)
            raise OSError("never leak credentials here")
    app.state.sync.provider = Failing(app.state.sync.provider.root)
    a = create(client)
    with app.state.repo.transaction() as tx:
        assert tx.pending(a)[0]["status"] == "FAILED"
    app.state.sync.provider = MockGoogle(app.state.sync.provider.root)
    result = client.post(f"/clients/{a}/sync/google", headers=headers()).json()
    assert result["sync_status"] == "SYNCED"
    with app.state.repo.transaction() as tx:
        assert not tx.pending(a)


def test_action_dependencies_and_calculation(client):
    a = create(client)
    first = client.post(f"/gem/client/{a}/action", json={"title": "First"}, headers=headers()).json()["data"]["action_id"]
    second = client.post(f"/gem/client/{a}/action", json={"title": "Second", "dependency": first}, headers=headers()).json()["data"]["action_id"]
    assert client.patch(f"/clients/{a}/actions/{second}", json={"status": "COMPLETED"}, headers=headers()).status_code == 409
    assert client.patch(f"/clients/{a}/actions/{first}", json={"status": "COMPLETED"}, headers=headers()).status_code == 200
    assert client.patch(f"/clients/{a}/actions/{second}", json={"status": "COMPLETED"}, headers=headers()).status_code == 200
    result = client.post(f"/gem/client/{a}/calculate", json={"operation": "roi", "inputs": {"investment": fact(100), "net_profit": fact(30)}, "currency": "EUR"}, headers=headers())
    assert result.status_code == 200
    assert result.json()["data"]["results"]["roi_ratio"] == "0.3"
    assert client.get(f"/clients/{a}/agenda", headers=headers()).json() == []


def test_validation_errors_do_not_echo_inputs(client):
    response = client.post("/clients", json={"password": "DO-NOT-ECHO"}, headers=headers())
    assert response.status_code == 422
    assert "DO-NOT-ECHO" not in response.text
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/openapi.json").status_code == 200
