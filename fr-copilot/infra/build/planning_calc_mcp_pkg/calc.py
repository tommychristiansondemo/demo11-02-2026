"""Deterministic planning math for FR Copilot. Pure functions, no I/O.

All results are illustrative and depend entirely on the stated assumptions.
"""
from __future__ import annotations

import math

# IRS Uniform Lifetime Table (effective 2022), distribution period by age.
UNIFORM_LIFETIME = {
    72: 27.4, 73: 26.5, 74: 25.5, 75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2,
    81: 19.4, 82: 18.5, 83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2, 87: 14.4, 88: 13.7, 89: 12.9,
    90: 12.2, 91: 11.5, 92: 10.8, 93: 10.1, 94: 9.5, 95: 8.9, 96: 8.4, 97: 7.8, 98: 7.3, 99: 6.8,
    100: 6.4, 101: 6.0, 102: 5.6, 103: 5.2, 104: 4.9, 105: 4.6, 106: 4.3, 107: 4.1, 108: 3.9,
    109: 3.7, 110: 3.5, 111: 3.4, 112: 3.3, 113: 3.1, 114: 3.0, 115: 2.9, 116: 2.8, 117: 2.7,
    118: 2.5, 119: 2.3, 120: 2.0,
}


class CalcError(ValueError):
    """Raised for invalid inputs (surfaced to the caller as a tool error)."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise CalcError(msg)


def _r(x: float) -> float:
    return round(x, 2)


def retirement_projection(current_age: int, retirement_age: int, current_balance: float,
                          annual_contribution: float, expected_return: float = 0.06,
                          inflation: float = 0.025, contribution_growth: float = 0.02,
                          annual_income: float = 0.0, income_replacement_pct: float = 0.8,
                          social_security_annual: float = 0.0, withdrawal_rate: float = 0.04) -> dict:
    """Year-by-year compound growth with end-of-year contributions, then compare to need.

    Need (today's dollars) = annual_income * income_replacement_pct - social_security_annual.
    Target nest egg = need / withdrawal_rate. On track if projected real balance >= target.
    """
    _require(0 <= current_age < retirement_age <= 80, "require 0 <= current_age < retirement_age <= 80")
    _require(current_balance >= 0 and annual_contribution >= 0, "balances and contributions must be >= 0")
    _require(-0.05 <= expected_return <= 0.15, "expected_return must be between -0.05 and 0.15")
    _require(0 <= inflation <= 0.10, "inflation must be between 0 and 0.10")
    _require(0 < withdrawal_rate <= 0.10, "withdrawal_rate must be in (0, 0.10]")
    years = retirement_age - current_age
    bal, contrib, total_contrib = float(current_balance), float(annual_contribution), 0.0
    schedule = []
    for y in range(1, years + 1):
        bal = bal * (1 + expected_return) + contrib
        total_contrib += contrib
        if y % 5 == 0 or y == years:
            schedule.append({"age": current_age + y, "balance_nominal": _r(bal),
                             "balance_today_dollars": _r(bal / (1 + inflation) ** y)})
        contrib *= 1 + contribution_growth
    real = bal / (1 + inflation) ** years
    need = max(annual_income * income_replacement_pct - social_security_annual, 0.0)
    target = need / withdrawal_rate
    sustainable_income = real * withdrawal_rate
    funded = (real / target) if target > 0 else None
    return {
        "years_to_retirement": years,
        "projected_balance_nominal": _r(bal),
        "projected_balance_today_dollars": _r(real),
        "total_contributions": _r(total_contrib),
        "sustainable_annual_income_today_dollars": _r(sustainable_income),
        "annual_income_need_from_savings_today_dollars": _r(need),
        "target_nest_egg_today_dollars": _r(target),
        "target_nest_egg_nominal": _r(target * (1 + inflation) ** years),
        "funded_ratio": round(funded, 3) if funded is not None else None,
        "on_track": (funded is None) or funded >= 1.0,
        "shortfall_today_dollars": _r(max(target - real, 0.0)),
        "schedule": schedule,
        "assumptions": {"expected_return": expected_return, "inflation": inflation,
                        "contribution_growth": contribution_growth, "withdrawal_rate": withdrawal_rate,
                        "income_replacement_pct": income_replacement_pct,
                        "contribution_timing": "end of year"},
    }


def life_insurance_needs(method: str, annual_income: float, existing_coverage: float = 0.0,
                         debts: float = 0.0, mortgage_balance: float = 0.0, num_children: int = 0,
                         education_cost_per_child: float = 100000.0, income_years: int = 10,
                         final_expenses: float = 15000.0, liquid_assets: float = 0.0,
                         income_replacement_pct: float = 0.7, discount_rate: float = 0.03) -> dict:
    """DIME (Debt + Income + Mortgage + Education) or income-replacement (present value) need."""
    method = method.strip().upper().replace("-", "_").replace(" ", "_")
    _require(method in ("DIME", "INCOME_REPLACEMENT"), "method must be 'DIME' or 'income_replacement'")
    _require(annual_income >= 0 and existing_coverage >= 0, "amounts must be >= 0")
    _require(0 <= num_children <= 12 and 1 <= income_years <= 40, "num_children 0-12, income_years 1-40")
    if method == "DIME":
        parts = {"debt_and_final_expenses": debts + final_expenses,
                 "income": annual_income * income_years,
                 "mortgage": mortgage_balance,
                 "education": num_children * education_cost_per_child}
    else:
        _require(0 <= discount_rate <= 0.10, "discount_rate must be 0-0.10")
        annual = annual_income * income_replacement_pct
        pv = annual * income_years if discount_rate == 0 else annual * (1 - (1 + discount_rate) ** -income_years) / discount_rate
        parts = {"present_value_of_income": pv, "debt_and_final_expenses": debts + final_expenses,
                 "mortgage": mortgage_balance, "education": num_children * education_cost_per_child}
    gross = sum(parts.values())
    net_need = max(gross - liquid_assets, 0.0)
    gap = max(net_need - existing_coverage, 0.0)
    return {
        "method": method,
        "components": {k: _r(v) for k, v in parts.items()},
        "gross_need": _r(gross),
        "less_liquid_assets": _r(liquid_assets),
        "total_need": _r(net_need),
        "existing_coverage": _r(existing_coverage),
        "coverage_gap": _r(gap),
        "has_gap": gap > 0,
        "coverage_ratio": round(existing_coverage / net_need, 3) if net_need else None,
        "assumptions": {"income_years": income_years, "education_cost_per_child": education_cost_per_child,
                        "final_expenses": final_expenses,
                        **({"income_replacement_pct": income_replacement_pct, "discount_rate": discount_rate}
                           if method == "INCOME_REPLACEMENT" else {})},
    }


def rmd_start_age(birth_year: int) -> int:
    """SECURE 2.0: 72 if born <= 1950, 73 if born 1951-1959, 75 if born 1960+."""
    if birth_year <= 1950:
        return 72
    return 73 if birth_year <= 1959 else 75


def required_minimum_distribution(age: int, prior_year_end_balance: float, birth_year: int | None = None) -> dict:
    """RMD = prior year-end balance / Uniform Lifetime Table factor (illustrative; ignores spouse >10y younger)."""
    _require(0 < age <= 125 and prior_year_end_balance >= 0, "age 1-125 and balance >= 0 required")
    start = rmd_start_age(birth_year) if birth_year else 73
    if age < start:
        return {"rmd_required": False, "rmd_start_age": start, "years_until_rmd": start - age,
                "rmd_amount": 0.0, "note": "No RMD required yet at this age."}
    factor = UNIFORM_LIFETIME[min(max(age, 72), 120)]
    return {"rmd_required": True, "rmd_start_age": start, "distribution_period": factor,
            "rmd_amount": _r(prior_year_end_balance / factor),
            "rmd_pct_of_balance": round(100 / factor, 2), "table": "IRS Uniform Lifetime Table (2022+)"}


def savings_gap(target_amount: float, current_savings: float, years: int, expected_return: float = 0.06,
                current_annual_contribution: float = 0.0) -> dict:
    """Required level end-of-year contribution to reach target_amount (nominal) in `years`."""
    _require(target_amount >= 0 and current_savings >= 0 and current_annual_contribution >= 0, "amounts must be >= 0")
    _require(1 <= years <= 60, "years must be 1-60")
    _require(-0.05 <= expected_return <= 0.15, "expected_return must be between -0.05 and 0.15")
    growth = (1 + expected_return) ** years
    fv_savings = current_savings * growth
    annuity = years if expected_return == 0 else (growth - 1) / expected_return
    fv_current = fv_savings + current_annual_contribution * annuity
    remaining = target_amount - fv_savings
    required = max(remaining / annuity, 0.0)
    gap = max(target_amount - fv_current, 0.0)
    return {
        "target_amount": _r(target_amount),
        "projected_with_current_contributions": _r(fv_current),
        "projected_shortfall": _r(gap),
        "required_annual_contribution": _r(required),
        "additional_annual_contribution_needed": _r(max(required - current_annual_contribution, 0.0)),
        "additional_monthly_contribution_needed": _r(max(required - current_annual_contribution, 0.0) / 12),
        "on_track": gap == 0,
        "assumptions": {"expected_return": expected_return, "years": years, "contribution_timing": "end of year"},
    }


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
