from datetime import date

import pytest
from pydantic import ValidationError

from mateo.calculators import FACTORS, calculate
from mateo.contracts import Fact, SessionClose
from mateo.core import DomainError, apply_fact_changes, get_next_actions, persistence_message, resolve_fact, unknown


def fact(value, status="CONFIRMED"):
    return {"value": value, "status": status, "source": "user_statement", "updated_at": "2026-09-10T20:00:00Z"}


def test_unknown_and_validation():
    assert Fact().model_dump(mode="json") == unknown()
    for bad in [dict(value=1), dict(value=2, status="CONFIRMED"), dict(secret=1), fact(1) | {"updated_at": "2026-01-01T00:00:00"}]:
        with pytest.raises(ValidationError):
            Fact(**bad)


def test_conflict_does_not_overwrite_or_promote():
    old = {"capital": fact(45000)}
    changed, conflicts = apply_fact_changes(old, [{"field": "capital", "fact": fact(60000)}])
    assert changed == old
    assert resolve_fact(old, conflicts[0], True)["capital"]["value"] == 60000
    assert resolve_fact(old, conflicts[0], False) == old
    with pytest.raises(DomainError):
        resolve_fact({"capital": fact(100)}, conflicts[0], True)
    _, conflicts = apply_fact_changes(old, [{"field": "capital", "fact": unknown()}])
    with pytest.raises(DomainError):
        resolve_fact(old, conflicts[0], True)
    changed, _ = apply_fact_changes({}, [{"field": "capital", "fact": fact(10, "ESTIMATED")}])
    assert changed["capital"]["status"] == "ESTIMATED"
    with pytest.raises(DomainError):
        apply_fact_changes({}, [{"field": "x", "fact": unknown()}] * 2)


@pytest.mark.parametrize("operation,values,key,expected", [
    ("break_even", [1000, 10, 5, 20], "daily_transactions", "10"),
    ("unit_economics", [10, 4], "contribution_per_unit", "6"),
    ("roi", [100, 20], "roi_ratio", "0.2"),
    ("payback", [100, 20], "months", "5"),
    ("cash_flow", [100, 1000, 0.4, 300, 2], "closing_cash", "700.0"),
    ("sensitivity", [10, 4, 200, 100, 0.1], "changed_profit", "500.0"),
])
def test_calculators(operation, values, key, expected):
    from mateo.calculators import REQUIRED
    result = calculate(operation, {k: fact(v) for k, v in zip(REQUIRED[operation], values)}, "EUR")
    assert result["results"][key] == expected
    assert result["confidence"] == "HIGH"


@pytest.mark.parametrize("operation,values", [("roi", [0, 2]), ("payback", [1, 0]), ("unit_economics", [0, 1]), ("unit_economics", [2, 3]), ("break_even", [2, 5, 1, 0]), ("cash_flow", [0, 1, 1.5, 1, 1]), ("sensitivity", [5, 1, 1, 1, 2]), ("roi", [-1, 1])])
def test_bad_financial_inputs(operation, values):
    from mateo.calculators import REQUIRED
    r = calculate(operation, {k: fact(v) for k, v in zip(REQUIRED[operation], values)}, "EUR")
    assert r["warnings"] and r["confidence"] == "LOW"


def test_missing_and_estimates():
    assert calculate("roi", {}, "EUR")["inputs_missing"] == ["investment", "net_profit"]
    for value in ["NaN", "nonsense", True]:
        assert calculate("roi", {"investment": fact(value), "net_profit": fact(2)}, "EUR")["inputs_missing"]
    assert calculate("roi", {"investment": fact(1, "ESTIMATED"), "net_profit": fact(2)}, "EUR")["confidence"] == "MEDIUM"
    with pytest.raises(ValueError):
        calculate("bad", {}, "EUR")


def test_location_evidence():
    assert calculate("location_score", {}, "EUR")["results"]["total"] is None
    inputs = {k: fact({"score": 80, "weight": 1, "evidence": "fictional survey"}) for k in FACTORS}
    assert calculate("location_score", inputs, "EUR")["results"]["total"] == "80"
    inputs[FACTORS[0]] = fact({"score": 101, "weight": 1, "evidence": "bad"})
    assert calculate("location_score", inputs, "EUR")["confidence"] == "LOW"


def test_agenda_and_messages():
    actions = [{"action_id": "1", "title": "A", "status": "PENDING", "priority": "LOW", "due_date": "2026-01-01"}, {"action_id": "2", "status": "PENDING", "priority": "HIGH", "due_date": None, "dependency": "1"}, {"action_id": "3", "status": "COMPLETED"}]
    agenda = get_next_actions(actions, date(2026, 9, 10), date(2026, 10, 1))
    assert [a["action_id"] for a in agenda] == ["1", "2"]
    assert agenda[1]["blocked"]
    assert "no tengo confirmación" in persistence_message(False)
    assert "pendiente" in persistence_message(True, "FAILED")
    assert persistence_message(True, "SYNCED") == "Tu expediente fue actualizado."


def test_session_confirmed_validation():
    with pytest.raises(ValidationError):
        SessionClose(client_id="MT-2026-000001", session_id="SES-000001", summary="X", new_confirmed_facts=[{"field": "capital", "fact": fact(1, "ESTIMATED")}])
