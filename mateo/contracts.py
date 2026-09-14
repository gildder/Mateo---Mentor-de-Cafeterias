"""Versioned boundary contracts; never used as ORM entities."""

from datetime import date as Date
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, model_validator


def now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


ClientID = Annotated[str, Field(pattern=r"^MT-\d{4}-\d{6}$")]
SessionID = Annotated[str, Field(pattern=r"^SES-\d{6}$")]
Title = Annotated[str, Field(min_length=1, max_length=300)]


class FactStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    ESTIMATED = "ESTIMATED"
    HYPOTHESIS = "HYPOTHESIS"
    BENCHMARK = "BENCHMARK"


class Fact(Contract):
    value: JsonValue = None
    status: FactStatus = FactStatus.UNKNOWN
    source: str | None = Field(default=None, max_length=500)
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def check_status(self):
        if self.status == FactStatus.UNKNOWN and self.value is not None:
            raise ValueError("UNKNOWN must have a null value")
        if self.status != FactStatus.UNKNOWN:
            if self.value is None or not self.source or not self.updated_at:
                raise ValueError("Known values need value, source and updated_at")
        if self.updated_at is not None and self.updated_at.utcoffset() is None:
            raise ValueError("updated_at requires a timezone")
        return self


class FactUpdate(Contract):
    field: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    fact: Fact


class ClientCreate(Contract):
    facts: list[FactUpdate] = Field(default_factory=list, max_length=100)


class ClientResolve(Contract):
    client_id: ClientID


class ProjectCreate(Contract):
    title: Title
    facts: list[FactUpdate] = Field(default_factory=list, max_length=100)
    opening_date: Date | None = None


class SessionStart(Contract):
    client_id: ClientID
    chat_url: HttpUrl | None = None
    objective: str = Field(default="", max_length=2000)


class Priority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ActionStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class ActionCreate(Contract):
    title: Title
    description: str = Field(default="", max_length=4000)
    owner: str = Field(default="entrepreneur", min_length=1, max_length=100)
    priority: Priority = Priority.MEDIUM
    status: ActionStatus = ActionStatus.PENDING
    session_id: SessionID | None = None
    due_date: Date | None = None
    dependency: str | None = Field(default=None, pattern=r"^ACT-[a-f0-9]{32}$")
    expected_result: str = Field(default="", max_length=2000)


class ActionState(Contract):
    status: ActionStatus


class NoteCreate(Contract):
    title: Title
    description: str = Field(default="", max_length=4000)
    session_id: SessionID | None = None


class ResearchCreate(Contract):
    topic: Title
    question: str = Field(min_length=1, max_length=2000)
    session_id: SessionID | None = None
    date: Date = Field(default_factory=lambda: now().date())
    sources: list[HttpUrl] = Field(default_factory=list, max_length=30)
    findings: str = Field(default="", max_length=10000)
    reliability: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    implications: str = Field(default="", max_length=4000)
    recommended_action: str = Field(default="", max_length=2000)


class SessionClose(Contract):
    client_id: ClientID
    session_id: SessionID
    objective: str = Field(default="", max_length=2000)
    new_confirmed_facts: list[FactUpdate] = Field(default_factory=list, max_length=100)
    updated_confirmed_facts: list[FactUpdate] = Field(default_factory=list, max_length=100)
    decisions: list[NoteCreate] = Field(default_factory=list, max_length=100)
    hypotheses: list[NoteCreate] = Field(default_factory=list, max_length=100)
    resolved_hypotheses: list[str] = Field(default_factory=list, max_length=100)
    risks: list[NoteCreate] = Field(default_factory=list, max_length=100)
    opportunities: list[str] = Field(default_factory=list, max_length=100)
    recommendations: list[str] = Field(default_factory=list, max_length=100)
    actions: list[ActionCreate] = Field(default_factory=list, max_length=100)
    research: list[ResearchCreate] = Field(default_factory=list, max_length=100)
    research_needed: list[str] = Field(default_factory=list, max_length=100)
    documents_requested: list[str] = Field(default_factory=list, max_length=100)
    next_session_objective: str = Field(default="", max_length=2000)
    summary: str = Field(min_length=1, max_length=10000)
    chat_url: HttpUrl | None = None

    @model_validator(mode="after")
    def confirmed_only(self):
        updates = self.new_confirmed_facts + self.updated_confirmed_facts
        if any(u.fact.status != FactStatus.CONFIRMED for u in updates):
            raise ValueError("Confirmed fact lists require CONFIRMED facts")
        if len({u.field for u in updates}) != len(updates):
            raise ValueError("Duplicate fact fields in session update")
        return self


class ConflictResolution(Contract):
    accept: bool


class EnvelopeMetadata(Contract):
    language: str = Field(default="es", pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    timestamp: datetime

    @model_validator(mode="after")
    def timezone_required(self):
        if self.timestamp.utcoffset() is None:
            raise ValueError("timestamp requires a timezone")
        return self


class MateoCRMEnvelope(Contract):
    schema_version: Literal["1.0"] = "1.0"
    operation: Literal["create_client", "resolve_client", "start_session", "close_session", "create_action", "create_decision", "create_risk", "create_research", "calculate"]
    client_id: ClientID | None = None
    session_id: SessionID | None = None
    source: Literal["gemini_gem", "rest", "apps_script", "n8n", "make", "zapier"]
    chat_url: HttpUrl | None = None
    payload: dict[str, Any]
    metadata: EnvelopeMetadata


class CalculationRequest(Contract):
    operation: Literal["break_even", "unit_economics", "roi", "payback", "cash_flow", "sensitivity", "location_score"]
    inputs: dict[str, Fact]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    assumptions: list[str] = Field(default_factory=list, max_length=30)



