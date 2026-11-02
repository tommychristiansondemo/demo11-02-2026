import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "planning_calc_mcp"))
import calc  # noqa: E402


def test_projection_zero_return_no_inflation_is_sum():
    r = calc.retirement_projection(60, 65, 1000, 100, expected_return=0, inflation=0, contribution_growth=0)
    assert r["projected_balance_nominal"] == 1500
    assert r["total_contributions"] == 500


def test_projection_compound_matches_closed_form():
    r = calc.retirement_projection(40, 50, 10000, 0, expected_return=0.05, inflation=0)
    assert r["projected_balance_nominal"] == pytest.approx(10000 * 1.05 ** 10, abs=0.01)


def test_projection_on_track_logic():
    r = calc.retirement_projection(46, 65, 272500, 22880, annual_income=142000, social_security_annual=32400)
    assert r["on_track"] is False and 0 < r["funded_ratio"] < 1
    assert r["shortfall_today_dollars"] > 0


def test_projection_validation():
    with pytest.raises(calc.CalcError):
        calc.retirement_projection(65, 60, 0, 0)


def test_dime():
    r = calc.life_insurance_needs("DIME", 100000, existing_coverage=500000, debts=10000, mortgage_balance=200000,
                                  num_children=2, final_expenses=15000)
    assert r["gross_need"] == 10000 + 15000 + 1000000 + 200000 + 200000
    assert r["coverage_gap"] == r["total_need"] - 500000
    assert r["has_gap"]


def test_income_replacement_pv():
    r = calc.life_insurance_needs("income_replacement", 100000, income_years=10, discount_rate=0,
                                  final_expenses=0, income_replacement_pct=1.0)
    assert r["gross_need"] == 1000000


def test_no_gap_when_overinsured():
    r = calc.life_insurance_needs("DIME", 50000, existing_coverage=5_000_000)
    assert r["coverage_gap"] == 0 and not r["has_gap"]


@pytest.mark.parametrize("by,start", [(1950, 72), (1955, 73), (1960, 75)])
def test_rmd_start_age(by, start):
    assert calc.rmd_start_age(by) == start


def test_rmd_amount():
    r = calc.required_minimum_distribution(75, 265000, birth_year=1951)
    assert r["rmd_required"] and r["rmd_amount"] == pytest.approx(265000 / 24.6, abs=0.01)
    assert not calc.required_minimum_distribution(70, 100000, 1956)["rmd_required"]


def test_savings_gap_closed_form():
    r = calc.savings_gap(100000, 0, 10, expected_return=0)
    assert r["required_annual_contribution"] == 10000
    r2 = calc.savings_gap(100000, 0, 10, expected_return=0, current_annual_contribution=10000)
    assert r2["on_track"] and r2["projected_shortfall"] == 0
