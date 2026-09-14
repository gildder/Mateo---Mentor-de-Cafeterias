"""HTTP boundary and composition root."""

import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mateo.contracts import ActionCreate, ActionState, CalculationRequest, ClientCreate, ClientResolve, ConflictResolution, MateoCRMEnvelope, NoteCreate, ProjectCreate, ResearchCreate, SessionClose, SessionStart
from mateo.core import DomainError, persistence_message
from mateo.providers import GoogleWorkspace, MockGoogle, SyncEngine
from mateo.repository import SQLRepository
from mateo.service import CRM
from mateo.settings import Settings

logger = logging.getLogger("mateo")


def create_app(settings: Settings | None = None, provider=None):
    settings = settings or Settings.from_environment()
    repo = SQLRepository(settings.database_url)
    crm = CRM(repo)
    selected = provider or (MockGoogle(settings.storage) if settings.mock else GoogleWorkspace.from_environment())
    sync = SyncEngine(repo, selected)

    @asynccontextmanager
    async def lifespan(app):
        yield
        repo.engine.dispose()

    app = FastAPI(title="Mateo CRM", version="1.0", lifespan=lifespan)
    app.state.repo, app.state.crm, app.state.sync = repo, crm, sync
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": exc.code})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"error": "invalid_request", "fields": [".".join(str(x) for x in e["loc"]) for e in exc.errors()]})

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logger.error(json.dumps({"event": "request_failed", "error_type": type(exc).__name__}))
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    @app.middleware("http")
    async def safe_log(request: Request, call_next):
        # No paths/query/body/headers: all may carry personal information or credentials.
        if int(request.headers.get("content-length", "0")) > 1_000_000:
            return JSONResponse(status_code=413, content={"error": "request_too_large"})
        response = await call_next(request)
        logger.info(json.dumps({"event": "request", "method": request.method, "status": response.status_code}))
        return response

    security = HTTPBearer(auto_error=False)
    def principal(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]):
        if credentials is None:
            raise DomainError("unauthorized", 401)
        with repo.transaction() as tx:
            return tx.principal(credentials.credentials)
    Principal = Annotated[dict, Depends(principal)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]

    def write(p, key, operation, client_id, data):
        if operation == "create_client" and p["role"] != "admin":
            raise DomainError("client_creation_requires_admin", 403)
        response = crm.execute(p["owner"], key, operation, client_id, data)
        status = sync.sync(response["client_id"], p["owner"])
        return {**response, "sync_status": status, "message": persistence_message(True, status)}

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "1.0"}

    @app.post("/gem/client/create")
    @app.post("/clients")
    def create_client(body: ClientCreate, p: Principal, key: Key):
        return write(p, key, "create_client", None, body.model_dump(mode="json"))

    @app.get("/clients")
    def list_clients(p: Principal):
        with repo.transaction() as tx:
            return tx.list_clients(p["owner"])

    @app.post("/gem/client/resolve")
    def resolve(body: ClientResolve, p: Principal):
        return crm.read(p["owner"], body.client_id)

    @app.get("/gem/client/{client_id}/context")
    @app.get("/clients/{client_id}")
    @app.get("/clients/{client_id}/summary")
    def context(client_id: str, p: Principal):
        return crm.read(p["owner"], client_id)

    @app.get("/clients/{client_id}/export")
    def export(client_id: str, p: Principal):
        return crm.read(p["owner"], client_id, "export")

    @app.post("/clients/{client_id}/projects")
    def project(client_id: str, body: ProjectCreate, p: Principal, key: Key):
        return write(p, key, "create_project", client_id, body.model_dump(mode="json"))

    @app.patch("/clients/{client_id}/profile")
    @app.patch("/clients/{client_id}")
    def patch_profile(client_id: str, body: ClientCreate, p: Principal, key: Key):
        return write(p, key, "patch_profile", client_id, body.model_dump(mode="json"))

    @app.patch("/clients/{client_id}/project")
    def patch_project(client_id: str, body: ClientCreate, p: Principal, key: Key):
        return write(p, key, "patch_project", client_id, body.model_dump(mode="json"))

    @app.post("/gem/session/start")
    def start(body: SessionStart, p: Principal, key: Key):
        return write(p, key, "start_session", body.client_id, body.model_dump(mode="json"))

    @app.post("/gem/session/close")
    def close(body: SessionClose, p: Principal, key: Key):
        return write(p, key, "close_session", body.client_id, body.model_dump(mode="json"))

    @app.get("/clients/{client_id}/sessions/{session_id}")
    def session(client_id: str, session_id: str, p: Principal):
        return crm.read(p["owner"], client_id, "session", session_id)

    @app.patch("/clients/{client_id}/actions/{action_id}")
    def action_state(client_id: str, action_id: str, body: ActionState, p: Principal, key: Key):
        return write(p, key, "action_state", client_id, {**body.model_dump(mode="json"), "action_id": action_id})

    @app.post("/clients/{client_id}/conflicts/{conflict_id}/resolve")
    def conflict(client_id: str, conflict_id: str, body: ConflictResolution, p: Principal, key: Key):
        return write(p, key, "resolve_conflict", client_id, {**body.model_dump(), "conflict_id": conflict_id})

    @app.post("/clients/{client_id}/sync/google")
    def retry(client_id: str, p: Principal):
        with repo.transaction() as tx:
            tx.authorize(client_id, p["owner"])
        return {"persisted": True, "sync_status": sync.sync(client_id, p["owner"], retry=True)}

    def register_note(kind, model):
        def endpoint(client_id: str, body, p: Principal, key: Key):
            return write(p, key, "create_" + kind, client_id, body.model_dump(mode="json"))
        endpoint.__annotations__["body"] = model
        app.post(f"/gem/client/{{client_id}}/{kind}", name="gem_create_" + kind)(endpoint)
        app.post(f"/clients/{{client_id}}/{kind + 's' if kind != 'research' else kind}", name="create_" + kind)(endpoint)
    for kind, model in [("action", ActionCreate), ("decision", NoteCreate), ("risk", NoteCreate), ("research", ResearchCreate)]:
        register_note(kind, model)

    def register_read(path, kind):
        def endpoint(client_id: str, p: Principal):
            return crm.read(p["owner"], client_id, kind)
        app.get(f"/clients/{{client_id}}/{path}", name="get_" + path)(endpoint)
    for path, kind in [("profile", "profile"), ("project", "project"), ("sessions", "session"), ("actions", "action"), ("agenda", "agenda"), ("decisions", "decision"), ("risks", "risk"), ("research", "research"), ("conflicts", "conflict")]:
        register_read(path, kind)

    @app.post("/gem/client/{client_id}/calculate")
    def calculation(client_id: str, body: CalculationRequest, p: Principal, key: Key):
        return write(p, key, "calculate", client_id, body.model_dump(mode="json"))

    @app.post("/gem/envelope")
    def envelope(body: MateoCRMEnvelope, p: Principal, key: Key):
        models = {"create_client": ClientCreate, "resolve_client": ClientResolve, "start_session": SessionStart, "close_session": SessionClose, "create_action": ActionCreate, "create_decision": NoteCreate, "create_risk": NoteCreate, "create_research": ResearchCreate, "calculate": CalculationRequest}
        payload = dict(body.payload)
        for field in ("client_id", "session_id", "chat_url"):
            external = getattr(body, field)
            if external is not None:
                external = str(external)
                if field in payload and payload[field] != external:
                    raise DomainError("envelope_identity_mismatch", 422)
                if field in models[body.operation].model_fields:
                    payload[field] = external
        from pydantic import ValidationError
        try:
            parsed = models[body.operation].model_validate(payload)
        except ValidationError:
            raise DomainError("invalid_envelope_payload", 422) from None
        if body.operation == "resolve_client":
            return crm.read(p["owner"], parsed.client_id)
        if body.operation != "create_client" and not body.client_id:
            raise DomainError("client_id_required", 422)
        if body.operation == "create_client" and body.client_id:
            raise DomainError("new_client_id_is_server_assigned", 422)
        return write(p, key, body.operation, body.client_id, parsed.model_dump(mode="json"))

    return app
