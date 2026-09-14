"""Pure calculations. Decimal strings avoid accidental currency rounding claims."""

from decimal import Decimal, InvalidOperation
from typing import Any

FACTORS = "target_customer_fit useful_foot_traffic rent_economics visibility accessibility competitive_context morning_demand operational_suitability growth_potential".split()
REQUIRED = {
    "break_even": ["fixed_costs", "price", "variable_cost", "operating_days"],
    "unit_economics": ["price", "variable_cost"], "roi": ["investment", "net_profit"],
    "payback": ["investment", "monthly_cash_flow"],
    "cash_flow": ["opening_cash", "monthly_revenue", "variable_cost_ratio", "fixed_costs", "months"],
    "sensitivity": ["price", "variable_cost", "fixed_costs", "transactions", "change_ratio"],
}


def calculate(operation: str, inputs: dict, currency: str, assumptions: list[str] | None = None) -> dict:
    result: dict[str, Any] = {"operation": operation, "currency": currency, "inputs_confirmed": [], "inputs_missing": [], "assumptions": list(assumptions or []), "warnings": [], "confidence": "LOW", "results": {}}
    if operation == "location_score":
        return location_score(inputs, result)
    if operation not in REQUIRED:
        raise ValueError("Unknown calculator")
    values = {}
    for name in REQUIRED[operation]:
        fact = inputs.get(name, {})
        if fact.get("status", "UNKNOWN") == "UNKNOWN" or fact.get("value") is None:
            result["inputs_missing"].append(name)
            continue
        try:
            if isinstance(fact["value"], bool):
                raise ValueError("boolean is not a number")
            value = Decimal(str(fact["value"]))
            if not value.is_finite():
                raise ValueError("nonfinite")
            values[name] = value
        except (InvalidOperation, ValueError):
            result["warnings"].append(f"Invalid numeric input: {name}")
            result["inputs_missing"].append(name)
            continue
        if fact["status"] == "CONFIRMED":
            result["inputs_confirmed"].append(name)
        else:
            result["assumptions"].append(f"{name} uses {fact['status']} input")
    if result["inputs_missing"]:
        return result
    v = values
    nonnegative = set(v) - {"net_profit", "opening_cash", "monthly_cash_flow", "change_ratio"}
    if any(v[k] < 0 for k in nonnegative):
        result["warnings"].append("Costs, volumes and investment must be nonnegative")
        return result
    out = {}
    if operation in {"break_even", "unit_economics", "sensitivity"}:
        margin = v["price"] - v["variable_cost"]
        if v["price"] <= 0 or margin <= 0:
            result["warnings"].append("Price and contribution margin must be positive; break-even is unreachable")
            return result
        out.update(contribution_per_unit=margin, contribution_margin_ratio=margin / v["price"])
        if operation == "break_even":
            if v["operating_days"] <= 0 or v["operating_days"] > 31 or v["operating_days"] != int(v["operating_days"]):
                result["warnings"].append("operating_days must be an integer from 1 to 31")
                return result
            units = v["fixed_costs"] / margin
            out.update(monthly_transactions=units, daily_transactions=units / v["operating_days"], monthly_sales=units * v["price"], daily_sales=units * v["price"] / v["operating_days"])
        if operation == "sensitivity":
            if abs(v["change_ratio"]) > 1:
                result["warnings"].append("change_ratio must be between -1 and 1")
                return result
            out.update(base_profit=margin * v["transactions"] - v["fixed_costs"], changed_profit=(v["price"] * (1 + v["change_ratio"]) - v["variable_cost"]) * v["transactions"] - v["fixed_costs"])
    elif operation == "roi":
        if v["investment"] <= 0:
            result["warnings"].append("Investment must be positive")
            return result
        out["roi_ratio"] = v["net_profit"] / v["investment"]
    elif operation == "payback":
        if v["monthly_cash_flow"] <= 0:
            result["warnings"].append("Payback is unreachable with nonpositive cash flow")
            return result
        out["months"] = v["investment"] / v["monthly_cash_flow"]
    elif operation == "cash_flow":
        if not 0 <= v["variable_cost_ratio"] <= 1 or not 1 <= v["months"] <= 120 or v["months"] != int(v["months"]):
            result["warnings"].append("Cost ratio must be 0..1 and months an integer 1..120")
            return result
        cash = v["monthly_revenue"] * (1 - v["variable_cost_ratio"]) - v["fixed_costs"]
        out.update(monthly_cash_generation=cash, closing_cash=v["opening_cash"] + cash * v["months"], revenue=v["monthly_revenue"], gross_margin_ratio=1 - v["variable_cost_ratio"], contribution=v["monthly_revenue"] * (1 - v["variable_cost_ratio"]), ebitda_estimate=cash)
        if v["variable_cost_ratio"] < 1:
            out["break_even_sales"] = v["fixed_costs"] / (1 - v["variable_cost_ratio"])
        result["warnings"].append("Simplified cash model excludes tax, debt, depreciation, seasonality and capital expenditure unless included explicitly")
    result["results"] = {k: str(value) for k, value in out.items()}
    result["confidence"] = "MEDIUM" if result["assumptions"] else "HIGH"
    return result


def location_score(inputs: dict, result: dict) -> dict:
    weighted = Decimal(0)
    total = Decimal(0)
    for name in FACTORS:
        fact = inputs.get(name, {})
        value = fact.get("value")
        if fact.get("status", "UNKNOWN") == "UNKNOWN" or not isinstance(value, dict):
            result["inputs_missing"].append(name)
            continue
        try:
            score, weight = Decimal(str(value["score"])), Decimal(str(value["weight"]))
            if not score.is_finite() or not weight.is_finite() or not 0 <= score <= 100 or not 0 < weight <= 100 or not value.get("evidence"):
                raise ValueError("invalid score")
        except (KeyError, InvalidOperation, ValueError):
            result["inputs_missing"].append(name)
            continue
        weighted += score * weight
        total += weight
        if fact["status"] == "CONFIRMED":
            result["inputs_confirmed"].append(name)
        else:
            result["assumptions"].append(f"{name} is {fact['status']}")
    # No invented points or misleading normalized total for an incomplete assessment.
    result["results"] = {"total": str(weighted / total) if total and not result["inputs_missing"] else None,
                         "missing_information": result["inputs_missing"], "critical_risks": [],
                         "recommendation": "Collect missing evidence before deciding" if result["inputs_missing"] else "Review evidence and financial feasibility before committing"}
    if not result["inputs_missing"]:
        result["confidence"] = "MEDIUM" if result["assumptions"] else "HIGH"
    return result
