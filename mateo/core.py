"""Pure domain policies: no database, web, or provider imports."""

import copy
import json
from datetime import date
from typing import Any

PROFILE_FIELDS = "name preferred_name country city timezone language entrepreneurial_experience hospitality_experience coffee_experience current_profession project_availability available_capital currency financing_access partners strengths support_needed personal_goals business_goals risk_tolerance target_date notes".split()
PROJECT_FIELDS = "project_name country city neighborhood stage concept coffee_shop_type target_customer customer_problem value_proposition differentiation service_model location size_m2 capacity opening_hours menu_summary target_average_ticket target_transactions_day target_monthly_sales available_budget capex working_capital financing rent payroll_estimate equipment suppliers competitors opening_target decisions_pending hypotheses risks opportunities kpis".split()


class DomainError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


def unknown() -> dict[str, Any]:
    return {"value": None, "status": "UNKNOWN", "source": None, "updated_at": None}


def empty_facts(fields: list[str]) -> dict[str, Any]:
    return {name: unknown() for name in fields}


def apply_fact_changes(facts: dict, updates: list[dict]) -> tuple[dict, list[dict]]:
    """Return copy and conflicts; confirmed facts are never silently replaced/downgraded."""
    result = copy.deepcopy(facts)
    conflicts = []
    seen = set()
    for update in updates:
        field, new = update["field"], update["fact"]
        if field in seen:
            raise DomainError("duplicate_fact_field", 422)
        seen.add(field)
        old = result.get(field, unknown())
        if old["status"] == "CONFIRMED" and (
            old["value"] != new["value"] or new["status"] != "CONFIRMED"
        ):
            conflicts.append({"field": field, "old": old, "new": new, "requires_confirmation": True})
        else:
            result[field] = copy.deepcopy(new)
    return result, conflicts


def resolve_fact(facts: dict, conflict: dict, accept: bool) -> dict:
    if not conflict["requires_confirmation"]:
        raise DomainError("conflict_already_resolved")
    if facts.get(conflict["field"], unknown()) != conflict["old"]:
        raise DomainError("conflict_stale")
    result = copy.deepcopy(facts)
    if accept:
        # Explicit confirmation is not permission to relabel a hypothesis as a fact.
        if conflict["new"]["status"] != "CONFIRMED":
            raise DomainError("confirmed_fact_downgrade_forbidden")
        result[conflict["field"]] = copy.deepcopy(conflict["new"])
    return result


def get_next_actions(actions: list[dict], today: date, opening_date: date | None = None) -> list[dict]:
    by_id = {a["action_id"]: a for a in actions}
    result = []
    for action in actions:
        if action["status"] in {"COMPLETED", "CANCELLED"}:
            continue
        item = copy.deepcopy(action)
        due = date.fromisoformat(item["due_date"]) if item.get("due_date") else None
        dependency = by_id.get(item.get("dependency"))
        blocked = item["status"] == "BLOCKED" or bool(dependency and dependency["status"] != "COMPLETED")
        overdue = bool(due and due < today)
        item.update(overdue=overdue, blocked=blocked, opening_related=bool(due and opening_date and due <= opening_date))
        item["horizon"] = "NOW" if overdue or item["priority"] == "HIGH" else "NEXT" if due else "LATER"
        result.append(item)
    return sorted(result, key=lambda a: (not a["overdue"], not a["blocked"], {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[a["priority"]], not a["opening_related"], a.get("due_date") or "9999-12-31", a["action_id"]))


def compact_context(snapshot: dict) -> dict:
    records = snapshot["records"]
    def kind(name):
        return [r["data"] for r in records if r["kind"] == name]
    profiles = kind("profile")
    projects = kind("project")
    sessions = sorted(kind("session"), key=lambda s: s["session_id"])
    latest = next((s for s in reversed(sessions) if s["status"] == "CLOSED"), {})
    known_profile = {k: v for k, v in (profiles[0]["facts"] if profiles else {}).items() if v["status"] != "UNKNOWN"}
    project = projects[-1] if projects else {}
    known_project = {k: v for k, v in project.get("facts", {}).items() if v["status"] != "UNKNOWN"}
    return {
        "schema_version": "1.0", "client_id": snapshot["client_id"],
        "revision": snapshot["revision"], "profile_summary": known_profile,
        "project_summary": known_project, "current_numbers": {k: v for k, v in {**known_profile, **known_project}.items() if isinstance(v["value"], (int, float)) and not isinstance(v["value"], bool)},
        "open_decisions": kind("decision")[-10:], "open_risks": kind("risk")[-10:],
        "open_actions": get_next_actions(kind("action"), date.today())[:20],
        "active_hypotheses": [h for h in kind("hypothesis") if h["status"] == "OPEN"][-10:],
        "last_session": {k: latest[k] for k in ("session_id", "summary", "closed_at", "chat_url") if k in latest},
        "next_focus": latest.get("next_session_objective", ""),
        "truncated": any(len(kind(k)) > n for k, n in [("decision", 10), ("risk", 10), ("action", 20), ("hypothesis", 10)]),
    }


def persistence_message(persisted: bool, sync_status: str | None = None) -> str:
    if not persisted:
        return "He preparado la actualización del expediente, pero no tengo confirmación de almacenamiento externo."
    if sync_status in {"PENDING", "FAILED"}:
        return "Tu expediente fue actualizado. La sincronización documental está pendiente."
    return "Tu expediente fue actualizado."


def markdown(title: str, data: Any) -> str:
    # A four-backtick fence prevents a user-controlled triple-backtick from ending it.
    encoded = json.dumps(data, ensure_ascii=False, indent=2).replace("`", "\\u0060")
    return f"# {title}\n\n```json\n{encoded}\n```\n"


def documents(snapshot: dict) -> dict[str, str]:
    root = f"CLIENTES/{snapshot['client_id']}"
    records = snapshot["records"]
    result = {}
    for kind, filename in [("profile", "perfil"), ("project", "proyecto")]:
        data = [r["data"] for r in records if r["kind"] == kind]
        result[f"{root}/{filename}.json"] = json.dumps(data, ensure_ascii=False, indent=2)
        result[f"{root}/{filename}.md"] = markdown(filename, data)
    for kind, filename in [("action", "agenda"), ("decision", "decisiones"), ("risk", "riesgos")]:
        result[f"{root}/{filename}.md"] = markdown(filename, [r["data"] for r in records if r["kind"] == kind])
    result[f"{root}/resumen_ejecutivo.md"] = markdown("Executive summary", compact_context(snapshot))
    for r in records:
        if r["kind"] in {"session", "research"}:
            folder = "sesiones" if r["kind"] == "session" else "investigaciones"
            for ext in ("md", "json"):
                result[f"{root}/{folder}/{r['record_id']}.{ext}"] = markdown(r["record_id"], r["data"]) if ext == "md" else json.dumps(r["data"], ensure_ascii=False, indent=2)
    return result


def index_rows(snapshot: dict) -> dict[str, list[list[str]]]:
    tabs = {name: [] for name in "CLIENTS PROJECTS SESSIONS ACTIONS DECISIONS RISKS RESEARCH AUDIT".split()}
    client_id = snapshot["client_id"]
    tabs["CLIENTS"] = [[client_id, client_id, json.dumps({"revision": snapshot["revision"]}), "SYNCED"]]
    mapping = {"project": "PROJECTS", "session": "SESSIONS", "action": "ACTIONS", "decision": "DECISIONS", "risk": "RISKS", "research": "RESEARCH"}
    for record in snapshot["records"]:
        if record["kind"] in mapping:
            tabs[mapping[record["kind"]]].append([record["record_id"], client_id, json.dumps(record["data"], ensure_ascii=False), "SYNCED"])
    for audit in snapshot["audit"]:
        tabs["AUDIT"].append([str(audit["audit_id"]), client_id, json.dumps(audit, ensure_ascii=False), "SYNCED"])
    return tabs
