from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from mateo.contracts import ResearchCreate


@pytest.mark.parametrize("value", ["2024-02-29", date(2024, 2, 29)])
def test_research_date_validation_and_json_round_trip(value):
    research = ResearchCreate(topic="Rent", question="What is the rent?", date=value)

    assert research.date == date(2024, 2, 29)
    assert research.model_dump(mode="json")["date"] == "2024-02-29"
    assert ResearchCreate.model_validate_json(research.model_dump_json()) == research


def test_research_date_default_is_evaluated_for_each_instance(monkeypatch):
    first_time = datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc)
    second_time = datetime(2026, 9, 14, 0, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("mateo.contracts.now", lambda: first_time)
    first = ResearchCreate(topic="Rent", question="What is the rent?")
    monkeypatch.setattr("mateo.contracts.now", lambda: second_time)
    second = ResearchCreate(topic="Rent", question="What is the rent?")

    assert first.date == first_time.date()
    assert second.date == second_time.date()
    assert second.model_dump(mode="json")["date"] == "2026-09-14"


@pytest.mark.parametrize("value", ["not-a-date", "2025-02-29", None])
def test_research_date_rejects_invalid_values(value):
    with pytest.raises(ValidationError) as error:
        ResearchCreate(topic="Rent", question="What is the rent?", date=value)

    assert error.value.errors()[0]["loc"] == ("date",)


def test_app_startup_and_research_date_openapi_schema(tmp_path):
    from fastapi.testclient import TestClient

    from mateo.api import create_app
    from mateo.providers import MockGoogle
    from mateo.settings import Settings

    settings = Settings(f"sqlite:///{tmp_path / 'db.sqlite'}", tmp_path / "storage")
    app = create_app(settings, provider=MockGoogle(settings.storage))
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()["components"]["schemas"]["ResearchCreate"]
        assert schema["properties"]["date"]["type"] == "string"
        assert schema["properties"]["date"]["format"] == "date"
        assert "date" not in schema["required"]
