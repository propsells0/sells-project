"""
Tests for app.marketing_logic — verifies every formula in markr.txt.

Run:  PYTHONIOENCODING=utf-8 python scripts/test_marketing_logic.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.marketing_logic import (  # noqa: E402
    compute_dashboard, status_volume, status_cpl_or_cost_per_deal,
    status_close_rate, status_spend_pacing, time_pacing,
    select_recommended_scenario, PeriodRow,
)

_failures = 0


def _check(name, ok, detail=""):
    global _failures
    if ok:
        print(f"  ok  {name}")
    else:
        _failures += 1
        print(f"  FAIL {name}: {detail}")


def _approx(a, b, eps=0.01):
    if a is None or b is None: return a is None and b is None
    return abs(a - b) < eps


def main():
    print("─── Status calculators ───")
    _check("volume on_track",  status_volume(95, 100) == "on_track")
    _check("volume warning",   status_volume(85, 100) == "warning")
    _check("volume critical",  status_volume(50, 100) == "critical")
    _check("volume n_a (zero target)", status_volume(50, 0) == "n_a")
    _check("cpl on_track (≤ target)", status_cpl_or_cost_per_deal(100, 100) == "on_track")
    _check("cpl warning (≤110%)",     status_cpl_or_cost_per_deal(110, 100) == "warning")
    _check("cpl critical (>110%)",    status_cpl_or_cost_per_deal(120, 100) == "critical")
    _check("close on_track (≥ target)", status_close_rate(0.02, 0.02) == "on_track")
    _check("close warning (≥90%)",      status_close_rate(0.0185, 0.02) == "warning")
    _check("close critical (<90%)",     status_close_rate(0.015, 0.02) == "critical")
    _check("pacing on_track (90-110%)", status_spend_pacing(100, 100) == "on_track")
    _check("pacing warning low",        status_spend_pacing(85, 100) == "warning")
    _check("pacing warning high",       status_spend_pacing(115, 100) == "warning")
    _check("pacing critical low",       status_spend_pacing(70, 100) == "critical")
    _check("pacing critical high",      status_spend_pacing(130, 100) == "critical")

    print("\n─── Time pacing ───")
    p = time_pacing("2026-04-01", "2026-04-30", "2026-04-15")
    _check("time pacing total_days", p["total_days"] == 30)
    _check("time pacing elapsed",    p["elapsed_days"] == 15)
    _check("time pacing progress 0.5", _approx(p["time_progress"], 0.5, eps=0.02))
    p2 = time_pacing(None, None, "2026-04-15")
    _check("time pacing unavailable when dates missing", p2["available"] is False)

    print("\n─── Recommended scenario ───")
    scenarios = {
        "conservative": {"target_cpl": 100, "target_deals": 5},
        "balanced":     {"target_cpl": 110, "target_deals": 6},
        "aggressive":   {"target_cpl": 120, "target_deals": 7},
    }
    _check("respects requested when valid",
           select_recommended_scenario(scenarios, "aggressive") == "aggressive")
    _check("falls back to balanced when no preference",
           select_recommended_scenario(scenarios, None) == "balanced")
    _check("falls back when requested is invalid",
           select_recommended_scenario(scenarios, "nonsense") == "balanced")

    print("\n─── Full dashboard: empty actuals ───")
    inputs = {
        "campaign_name":       "Test Campaign",
        "avg_unit_price":      5_000_000,
        "commission_input":    2.5,                  # 2.5% (whole-number percent — matches DB convention)
        "commission_type":     "percentage",
        "tax_rate":            0.19,
        "expected_close_rate": 0.02,                 # 2% (decimal — matches DB convention)
        "campaign_budget":     500_000,
        "start_date":          "2026-04-01",
        "end_date":            "2026-04-30",
        "review_date":         "2026-04-15",
        "recommended_scenario": "balanced",
    }
    d = compute_dashboard(inputs)

    # Gross commission = 5M × 2.5% = 125,000
    _check("gross commission 125,000", _approx(d["overview"]["gross_commission"], 125_000))
    # Net = 125,000 × (1 - 0.19) = 101,250
    _check("net commission 101,250",   _approx(d["overview"]["net_commission"], 101_250))
    # Balanced target_cost_per_deal = 101,250 × 0.16 = 16,200
    _check("balanced target_cost_per_deal 16,200",
           _approx(d["scenarios"]["balanced"]["target_cost_per_deal"], 16_200))
    # Target CPL = 16,200 × 0.02 = 324
    _check("balanced target_cpl 324",
           _approx(d["scenarios"]["balanced"]["target_cpl"], 324))
    # Expected leads = 500,000 / 324 ≈ 1543
    _check("balanced expected_leads ≈ 1543",
           abs(d["scenarios"]["balanced"]["expected_leads"] - 1543) <= 1)
    # Expected deals = 1543 × 0.02 ≈ 30.87 → ceil = 31
    _check("balanced target_deals = 31",
           d["scenarios"]["balanced"]["target_deals"] == 31)
    # Funnel: 31 deals × 25 = 775 qualified leads
    _check("balanced expected_qualified_leads = 775",
           d["master"]["target_qualified_leads"] == 775)
    # Meetings expected = 31 × 7.5 = 232.5 → 233 (round)
    _check("balanced expected_meetings = 233",
           d["master"]["target_meetings"] == 233)
    # Follow-ups expected = 31 × 45 = 1395
    _check("balanced expected_follow_ups = 1395",
           d["master"]["target_follow_ups"] == 1395)
    _check("recommended = balanced", d["recommended"] == "balanced")
    _check("actuals.present = False (no actuals)", d["actuals"]["present"] is False)

    print("\n─── Full dashboard: with actuals ───")
    actuals = {
        "actual_spend":           250_000,   # 50% of budget
        "actual_leads":           600,
        "actual_qualified_leads": 350,
        "actual_meetings":        100,
        "actual_follow_ups":      550,
        "actual_deals":           12,
    }
    d = compute_dashboard(inputs, actuals)

    # Spend progress = 250k / 500k = 0.5
    _check("spend_progress 0.5",
           _approx(d["dynamic_by_spend"]["spend_progress"], 0.5))
    # Dynamic Target Leads = 1543 × 0.5 = 771.5 (or whatever expected_leads was, × 0.5)
    leads_row = next(r for r in d["dynamic_by_spend"]["rows"] if r["kpi"] == "leads")
    _check("dynamic leads target = full_target × 0.5",
           _approx(leads_row["dynamic_target"], leads_row["full_target"] * 0.5))
    # Actual cpl = 250k / 600 ≈ 416.67
    _check("actual_cpl ≈ 416.67", _approx(d["actuals"]["actual_cpl"], 416.67))
    # Status: actual_cpl=416.67 vs target_cpl=324 → 416.67/324 = 1.286 > 110% → critical
    cpl_row = next(r for r in d["dynamic_by_spend"]["efficiency_rows"] if r["kpi"] == "cpl")
    _check("cpl status = critical (>110% of target)", cpl_row["status"] == "critical")
    # Actual close = 12/600 = 0.02 = target → on_track
    cr_row = next(r for r in d["dynamic_by_spend"]["efficiency_rows"] if r["kpi"] == "close_rate")
    _check("close_rate status = on_track (== target)", cr_row["status"] == "on_track")

    print("\n─── Full dashboard: time pacing ───")
    # review_date is 2026-04-15, total = 30 days, elapsed = 15 → time_progress = 0.5
    _check("time pacing available", d["time_pacing"]["available"] is True)
    _check("time pacing total_days = 30", d["time_pacing"]["total_days"] == 30)
    _check("planned_spend = budget × 0.5 = 250,000",
           _approx(d["time_pacing"]["planned_spend"], 250_000, eps=0.02))
    # spend pacing: actual=250k, planned=250k → ratio=1.0 → on_track
    _check("spend_pacing on_track (ratio 1.0)",
           d["time_pacing"]["spend_pacing_status"] == "on_track")

    print("\n─── Periods + 5-day health check ───")
    periods = [
        PeriodRow("5_day", 1, "Days 1-5",   None, None,  80_000, 200, 120, 30, 200, 4),
        PeriodRow("5_day", 2, "Days 6-10",  None, None,  90_000, 200, 110, 25, 180, 3),
        PeriodRow("5_day", 3, "Days 11-15", None, None,  80_000, 200, 120, 35, 220, 5),
    ]
    d = compute_dashboard(inputs, actuals, periods)
    _check("5-day rows count = 3",
           len(d["periods"]["5_day"]) == 3)
    _check("5-day health rows = 3",
           len(d["health_check_5day"]["rows"]) == 3)
    _check("delta vs previous on row 2 set",
           d["periods"]["5_day"][1]["delta_spend_vs_prev"] is not None)
    _check("delta vs previous on row 1 None",
           d["periods"]["5_day"][0]["delta_spend_vs_prev"] is None)
    _check("last window verdict present",
           d["health_check_5day"]["last_window"] in ("excellent","good","watch","critical"))

    print("\n─── Edge: zero divisions don't crash ───")
    bad_inputs = {**inputs, "expected_close_rate": 0, "campaign_budget": 0}
    try:
        d = compute_dashboard(bad_inputs)
        _check("zero close_rate doesn't crash",
               d["scenarios"]["balanced"]["target_cpl"] is None)
    except Exception as e:
        _check("zero close_rate doesn't crash", False, str(e))

    bad_actuals = {"actual_spend": 100, "actual_leads": 0, "actual_deals": 0}
    try:
        d = compute_dashboard(inputs, bad_actuals)
        _check("zero leads → actual_cpl is None", d["actuals"]["actual_cpl"] is None)
    except Exception as e:
        _check("zero leads → actual_cpl is None", False, str(e))

    print("\n─── Fixed-amount commission ───")
    fixed_inputs = {**inputs, "commission_type": "fixed", "commission_input": 200_000}
    d = compute_dashboard(fixed_inputs)
    # Gross = 200k (fixed). Net = 200k × 0.81 = 162,000
    _check("fixed gross = 200,000", _approx(d["overview"]["gross_commission"], 200_000))
    _check("fixed net = 162,000",   _approx(d["overview"]["net_commission"], 162_000))

    print()
    if _failures:
        print(f"❌ {_failures} failure(s)")
        sys.exit(1)
    print("✅ all marketing_logic tests passed")


if __name__ == "__main__":
    main()
