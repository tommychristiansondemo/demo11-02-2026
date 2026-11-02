"""planning-calc-mcp: deterministic retirement & insurance math (no data store).

Runs on AgentCore Runtime with the MCP protocol: stateless streamable HTTP on 0.0.0.0:8000/mcp.
"""
import time

from mcp.server.fastmcp import FastMCP

import calc
from jsonlog import get_logger, log_event

log = get_logger("planning-calc-mcp")
mcp = FastMCP("planning-calc-mcp", host="0.0.0.0", stateless_http=True,
              instructions="Deterministic, illustrative planning calculations. Rates are decimals (0.06 = 6%).")


def _run(name, fn, **kwargs):
    t0 = time.perf_counter()
    try:
        out = fn(**kwargs)
        log_event(log, f"tool.{name}", ok=True, ms=round((time.perf_counter() - t0) * 1000, 2))
        out["disclaimer"] = "Illustrative calculation based on stated assumptions; not a guarantee of future results."
        return out
    except calc.CalcError as e:
        log_event(log, f"tool.{name}", ok=False, error=str(e))
        raise ValueError(str(e)) from e


@mcp.tool()
def retirement_projection(current_age: int, retirement_age: int, current_balance: float, annual_contribution: float,
                          expected_return: float = 0.06, inflation: float = 0.025, contribution_growth: float = 0.02,
                          annual_income: float = 0.0, income_replacement_pct: float = 0.8,
                          social_security_annual: float = 0.0, withdrawal_rate: float = 0.04) -> dict:
    """Project retirement savings with compound growth, growing annual contributions (include employer match) and inflation.
    Compares the inflation-adjusted balance to the nest egg needed to fund
    annual_income * income_replacement_pct minus Social Security at the given withdrawal rate. Returns on_track and funded_ratio."""
    return _run("retirement_projection", calc.retirement_projection, current_age=current_age,
                retirement_age=retirement_age, current_balance=current_balance,
                annual_contribution=annual_contribution, expected_return=expected_return, inflation=inflation,
                contribution_growth=contribution_growth, annual_income=annual_income,
                income_replacement_pct=income_replacement_pct, social_security_annual=social_security_annual,
                withdrawal_rate=withdrawal_rate)


@mcp.tool()
def life_insurance_needs(method: str, annual_income: float, existing_coverage: float = 0.0, debts: float = 0.0,
                         mortgage_balance: float = 0.0, num_children: int = 0,
                         education_cost_per_child: float = 100000.0, income_years: int = 10,
                         final_expenses: float = 15000.0, liquid_assets: float = 0.0,
                         income_replacement_pct: float = 0.7, discount_rate: float = 0.03) -> dict:
    """Life insurance need and coverage gap. method='DIME' (Debt+Income x years+Mortgage+Education) or
    'income_replacement' (present value of replaced income + debts + mortgage + education).
    existing_coverage should total all in-force life death benefits (note non-portable group life)."""
    return _run("life_insurance_needs", calc.life_insurance_needs, method=method, annual_income=annual_income,
                existing_coverage=existing_coverage, debts=debts, mortgage_balance=mortgage_balance,
                num_children=num_children, education_cost_per_child=education_cost_per_child,
                income_years=income_years, final_expenses=final_expenses, liquid_assets=liquid_assets,
                income_replacement_pct=income_replacement_pct, discount_rate=discount_rate)


@mcp.tool()
def required_minimum_distribution(age: int, prior_year_end_balance: float, birth_year: int = 0) -> dict:
    """Required minimum distribution from a tax-deferred account using the IRS Uniform Lifetime Table and SECURE 2.0 start ages."""
    return _run("required_minimum_distribution", calc.required_minimum_distribution, age=age,
                prior_year_end_balance=prior_year_end_balance, birth_year=birth_year or None)


@mcp.tool()
def savings_gap(target_amount: float, current_savings: float, years: int, expected_return: float = 0.06,
                current_annual_contribution: float = 0.0) -> dict:
    """Annual contribution required to reach a nominal target in N years, and the shortfall at current contributions."""
    return _run("savings_gap", calc.savings_gap, target_amount=target_amount, current_savings=current_savings,
                years=years, expected_return=expected_return, current_annual_contribution=current_annual_contribution)


if __name__ == "__main__":
    log_event(log, "startup")
    mcp.run(transport="streamable-http")
