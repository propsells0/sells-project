"""
Marketing dashboard computation engine — Section 05 of the requirements.

All formulas come straight from markr.txt (the campaign manager's spec). Pure
functions with no DB / Flask dependencies — easy to test and reuse from any
endpoint or worker.

Exported entry point:
    compute_dashboard(inputs, actuals=None, periods=None) -> dict
        Returns a fully-populated payload matching the 12 sections in the
        spec's "11) المطلوب من الإخراج" section.

Single-source-of-truth for:
    - 3-scenario target plans (Conservative / Balanced / Aggressive)
    - Recommended scenario selection
    - Spend Progress % + Dynamic Targets by Spend
    - Time Progress % + Time-Based Targets
    - Status (On Track / Warning / Critical) for 3 categories
    - Period performance + 5-day health check
    - Operational formulas (CPL, Cost per Deal, Close Rate, Revenue Proxy)

Status thresholds intentionally match the markr.txt rules verbatim, including
the asymmetric ones (e.g. spend pacing is Critical >120% OR <80%).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional


# ─── Scenario constants (markr.txt §3.3) ──────────────────────────────────────

SCENARIOS = ("conservative", "balanced", "aggressive")
SCENARIO_RATIOS = {"conservative": 0.15, "balanced": 0.16, "aggressive": 0.17}

# Funnel assumption ranges (markr.txt §4)
MEETINGS_MIN_PER_DEAL = 5
MEETINGS_MAX_PER_DEAL = 10
MEETINGS_MID_PER_DEAL = 7.5
QUALIFIED_LEADS_PER_DEAL = 25
FOLLOWUPS_MIN_PER_DEAL = 40
FOLLOWUPS_MAX_PER_DEAL = 50
FOLLOWUPS_MID_PER_DEAL = 45

# Status thresholds — one source of truth for all three Status types.
STATUS_VOLUME_ON_TRACK = 0.95   # ≥95% of target
STATUS_VOLUME_WARNING  = 0.80   # 80% to <95% — anything below 80% is Critical
STATUS_EFFICIENCY_WARN = 1.10   # CPL/Cost per Deal: ≤ target On Track; up to 110% Warning; >110% Critical
STATUS_CLOSE_RATE_WARN = 0.90   # Close rate: ≥ target On Track; 90-100% Warning; <90% Critical
STATUS_PACING_LOW_WARN  = 0.80
STATUS_PACING_LOW_OK    = 0.90
STATUS_PACING_HIGH_OK   = 1.10
STATUS_PACING_HIGH_WARN = 1.20


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_div(num, den):
    """Division that returns None when den is 0/None — caller renders as N/A."""
    if den is None or den == 0:
        return None
    return num / den


def _ceil_int(x: float) -> int:
    """Round-up integer (markr.txt §3.7: 'تقريب Expected Deals لأعلى')."""
    return int(math.ceil(x)) if x else 0


def _f(x, default=0.0):
    """Coerce to float with a safe default."""
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _i(x, default=0):
    try:
        return int(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _parse_date(d):
    """Accept date / 'YYYY-MM-DD' / None."""
    if d is None or d == "":
        return None
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d))
    except ValueError:
        return None


# ─── Status calculators ───────────────────────────────────────────────────────

def status_volume(actual: float, target: Optional[float]) -> str:
    """Volume KPIs: ≥95% On Track, 80-<95% Warning, <80% Critical."""
    if target is None or target == 0:
        return "n_a"
    ratio = actual / target
    if ratio >= STATUS_VOLUME_ON_TRACK:
        return "on_track"
    if ratio >= STATUS_VOLUME_WARNING:
        return "warning"
    return "critical"


def status_cpl_or_cost_per_deal(actual: Optional[float], target: Optional[float]) -> str:
    """Cost-style KPIs: ≤ target On Track; up to 110% Warning; >110% Critical."""
    if actual is None or target is None or target == 0:
        return "n_a"
    if actual <= target:
        return "on_track"
    if actual <= target * STATUS_EFFICIENCY_WARN:
        return "warning"
    return "critical"


def status_close_rate(actual: Optional[float], target: Optional[float]) -> str:
    """Close rate: ≥ target On Track; 90-100% Warning; <90% Critical."""
    if actual is None or target is None or target == 0:
        return "n_a"
    if actual >= target:
        return "on_track"
    if actual >= target * STATUS_CLOSE_RATE_WARN:
        return "warning"
    return "critical"


def status_spend_pacing(actual_spend: float, planned_spend: Optional[float]) -> str:
    """Time pacing for spend: 90-110% On Track; 80-90% or 110-120% Warning;
    outside 80-120% Critical."""
    if planned_spend is None or planned_spend == 0:
        return "n_a"
    ratio = actual_spend / planned_spend
    if STATUS_PACING_LOW_OK <= ratio <= STATUS_PACING_HIGH_OK:
        return "on_track"
    if (STATUS_PACING_LOW_WARN <= ratio < STATUS_PACING_LOW_OK
        or STATUS_PACING_HIGH_OK < ratio <= STATUS_PACING_HIGH_WARN):
        return "warning"
    return "critical"


# ─── Scenario plan + target funnel ────────────────────────────────────────────

def _scenario_plan(net_commission: float, close_rate: float, budget: float, ratio: float) -> dict:
    """One scenario row — Target Cost per Deal / CPL / Expected Leads / Deals."""
    target_cost_per_deal = net_commission * ratio
    target_cpl = target_cost_per_deal * close_rate if close_rate else None
    expected_leads = budget / target_cpl if target_cpl else None
    expected_deals = expected_leads * close_rate if expected_leads is not None else None
    target_deals = _ceil_int(expected_deals) if expected_deals is not None else 0
    return {
        "scenario_ratio": ratio,
        "target_cost_per_deal": round(target_cost_per_deal, 2),
        "target_cpl": round(target_cpl, 2) if target_cpl is not None else None,
        "expected_leads": int(round(expected_leads)) if expected_leads is not None else 0,
        "expected_deals": round(expected_deals, 2) if expected_deals is not None else 0,
        "target_deals": target_deals,
    }


def _funnel_for_target_deals(target_deals: int) -> dict:
    """markr.txt §4 — funnel ranges per scenario based on target_deals.
    Mid-point values use ceiling (matches the spec's round-up convention
    for target_deals); min/max are exact integer products."""
    return {
        "min_meetings":          target_deals * MEETINGS_MIN_PER_DEAL,
        "max_meetings":          target_deals * MEETINGS_MAX_PER_DEAL,
        "expected_meetings":     _ceil_int(target_deals * MEETINGS_MID_PER_DEAL),
        "expected_qualified_leads": target_deals * QUALIFIED_LEADS_PER_DEAL,
        "min_follow_ups":        target_deals * FOLLOWUPS_MIN_PER_DEAL,
        "max_follow_ups":        target_deals * FOLLOWUPS_MAX_PER_DEAL,
        "expected_follow_ups":   _ceil_int(target_deals * FOLLOWUPS_MID_PER_DEAL),
    }


def select_recommended_scenario(scenarios: dict, requested: Optional[str] = None) -> str:
    """Pick the scenario that survives:
       1) target_cpl is sensible (not None and > 0)
       2) target_deals > 0
       3) prefer the one explicitly requested (markr.txt §5: respect human
          judgment) when it satisfies (1) and (2). Default fallback: balanced.
    """
    if requested in SCENARIOS:
        s = scenarios[requested]
        if s.get("target_cpl") and s.get("target_deals", 0) > 0:
            return requested
    for key in ("balanced", "conservative", "aggressive"):
        s = scenarios.get(key, {})
        if s.get("target_cpl") and s.get("target_deals", 0) > 0:
            return key
    return "balanced"


# ─── Time pacing + Spend progress ─────────────────────────────────────────────

def time_pacing(start_date, end_date, current_date) -> dict:
    """Returns days/progress dict, or zeros if dates incomplete."""
    sd = _parse_date(start_date)
    ed = _parse_date(end_date)
    cd = _parse_date(current_date) or date.today()
    if not sd or not ed or ed <= sd:
        return {
            "available": False,
            "total_days": 0, "elapsed_days": 0, "time_progress": 0.0,
            "start_date": sd.isoformat() if sd else None,
            "end_date":   ed.isoformat() if ed else None,
            "current_date": cd.isoformat(),
        }
    total = (ed - sd).days + 1
    # Clamp elapsed to [0, total] — review-date before start or after end.
    elapsed = max(0, min(total, (cd - sd).days + 1))
    return {
        "available": True,
        "total_days": total,
        "elapsed_days": elapsed,
        "time_progress": round(elapsed / total, 4),
        "start_date": sd.isoformat(),
        "end_date":   ed.isoformat(),
        "current_date": cd.isoformat(),
    }


# ─── Period helpers ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PeriodRow:
    period_kind: str
    period_index: int
    period_label: str
    period_start: Optional[date]
    period_end:   Optional[date]
    spend: float
    leads: int
    qualified_leads: int
    meetings: int
    follow_ups: int
    deals: int
    notes: Optional[str] = None


def _period_metrics(p: PeriodRow, net_commission: float, prev: Optional[PeriodRow] = None) -> dict:
    """Computes CPL / Cost per Deal / Close Rate / Revenue Proxy + delta vs previous."""
    spend = _f(p.spend); leads = _i(p.leads); deals = _i(p.deals)
    cpl  = _safe_div(spend, leads)
    cpd  = _safe_div(spend, deals)
    crr  = _safe_div(deals, leads)
    revp = deals * net_commission
    delta = None
    if prev is not None:
        prev_spend = _f(prev.spend)
        delta = _safe_div(spend - prev_spend, prev_spend)
    return {
        "kind":  p.period_kind,
        "index": p.period_index,
        "label": p.period_label,
        "start": p.period_start.isoformat() if p.period_start else None,
        "end":   p.period_end.isoformat() if p.period_end else None,
        "spend": round(spend, 2),
        "leads": leads,
        "qualified_leads": _i(p.qualified_leads),
        "meetings": _i(p.meetings),
        "follow_ups": _i(p.follow_ups),
        "deals": deals,
        "cpl":           round(cpl, 2) if cpl is not None else None,
        "cost_per_deal": round(cpd, 2) if cpd is not None else None,
        "close_rate":    round(crr, 4) if crr is not None else None,
        "revenue_proxy": round(revp, 2),
        "delta_spend_vs_prev": round(delta, 4) if delta is not None else None,
        "notes": p.notes,
    }


def _five_day_health(period: dict, target_cpl: Optional[float],
                     target_cpd: Optional[float], target_close_rate: Optional[float]) -> dict:
    """markr.txt §11.8 — overall verdict for a 5-day window."""
    cpl_s = status_cpl_or_cost_per_deal(period.get("cpl"),           target_cpl)
    cpd_s = status_cpl_or_cost_per_deal(period.get("cost_per_deal"), target_cpd)
    cr_s  = status_close_rate(period.get("close_rate"),              target_close_rate)
    statuses = [cpl_s, cpd_s, cr_s]
    if "critical" in statuses:
        verdict = "critical"
    elif "warning" in statuses:
        verdict = "watch"
    elif all(s == "on_track" for s in statuses):
        verdict = "excellent"
    elif "n_a" in statuses and not any(s == "critical" or s == "warning" for s in statuses):
        verdict = "good"
    else:
        verdict = "good"
    return {
        "verdict":     verdict,
        "cpl_status":  cpl_s,
        "cpd_status":  cpd_s,
        "close_rate_status": cr_s,
    }


# ─── Main entry point ────────────────────────────────────────────────────────

def compute_dashboard(
    inputs: dict,
    actuals: Optional[dict] = None,
    periods: Optional[list] = None,
) -> dict:
    """
    Build the full dashboard payload.

    `inputs`:
        campaign_name, avg_unit_price, commission_input, commission_type
        ('percentage' | 'fixed'), tax_rate (decimal e.g. 0.19),
        expected_close_rate (decimal e.g. 0.02), campaign_budget,
        start_date, end_date, review_date,
        recommended_scenario (one of SCENARIOS or None — auto-selected).

    `actuals`: cumulative {actual_spend, actual_leads, actual_qualified_leads,
        actual_meetings, actual_follow_ups, actual_deals}. Optional.

    `periods`: list[PeriodRow]. Optional. The function buckets by period_kind
        and emits separate sections (daily / 5_day / weekly / monthly).

    Returned dict mirrors the 12 sections in markr.txt §11, but as a single
    payload — the API layer hands it back to the frontend, which renders.
    """
    actuals = actuals or {}
    periods = list(periods or [])

    # ── Section 1: Campaign Overview + commissions ────────────────────────
    avg_price       = _f(inputs.get("avg_unit_price"))
    comm_input      = _f(inputs.get("commission_input"))
    comm_type       = (inputs.get("commission_type") or "percentage").strip().lower()
    tax_rate        = _f(inputs.get("tax_rate"))
    close_rate      = _f(inputs.get("expected_close_rate"))
    budget          = _f(inputs.get("campaign_budget"))

    # Storage convention (matching marketing_campaigns table):
    #   - commission_input is stored as a whole-number percent for type='percentage'
    #     (e.g. 3.0 means 3%, NOT 300%). Inconsistent with tax_rate/close_rate which
    #     are stored as decimals — but the existing 7 live rows follow this rule, so
    #     don't touch the DB convention; normalize here.
    #   - For type='fixed', commission_input is the raw money amount.
    if comm_type == "percentage":
        gross_commission = avg_price * (comm_input / 100.0)
    else:
        gross_commission = comm_input
    net_commission = gross_commission * (1 - tax_rate)

    overview = {
        "campaign_name":      inputs.get("campaign_name") or "",
        "avg_unit_price":     round(avg_price, 2),
        "commission_input":   round(comm_input, 4),
        "commission_type":    comm_type,
        "tax_rate":           round(tax_rate, 4),
        "gross_commission":   round(gross_commission, 2),
        "net_commission":     round(net_commission, 2),
        "expected_close_rate": round(close_rate, 4),
        "campaign_budget":    round(budget, 2),
    }

    # ── Section 2: Scenarios ──────────────────────────────────────────────
    scenarios = {
        key: _scenario_plan(net_commission, close_rate, budget, ratio)
        for key, ratio in SCENARIO_RATIOS.items()
    }
    recommended = select_recommended_scenario(scenarios, inputs.get("recommended_scenario"))
    selected = scenarios[recommended]
    funnel = _funnel_for_target_deals(selected["target_deals"])

    # ── Section 3: Master Funnel Target ───────────────────────────────────
    full_target_revenue_proxy = selected["target_deals"] * net_commission
    master = {
        "scenario":             recommended,
        "budget":               round(budget, 2),
        "target_cpl":           selected["target_cpl"],
        "target_cost_per_deal": selected["target_cost_per_deal"],
        "target_close_rate":    round(close_rate, 4),
        "target_leads":         selected["expected_leads"],
        "target_qualified_leads": funnel["expected_qualified_leads"],
        "target_meetings":      funnel["expected_meetings"],
        "target_follow_ups":    funnel["expected_follow_ups"],
        "target_deals":         selected["target_deals"],
        "target_revenue_proxy": round(full_target_revenue_proxy, 2),
        "funnel_ranges":        funnel,
    }

    # ── Section 4: Dynamic by Spend ───────────────────────────────────────
    actual_spend  = _f(actuals.get("actual_spend"))
    actual_leads  = _i(actuals.get("actual_leads"))
    actual_qleads = _i(actuals.get("actual_qualified_leads"))
    actual_mtg    = _i(actuals.get("actual_meetings"))
    actual_fup    = _i(actuals.get("actual_follow_ups"))
    actual_deals  = _i(actuals.get("actual_deals"))
    actuals_present = any(actuals.get(k) is not None for k in
        ("actual_spend","actual_leads","actual_qualified_leads",
         "actual_meetings","actual_follow_ups","actual_deals"))

    spend_progress = _safe_div(actual_spend, budget) or 0.0
    actual_cpl     = _safe_div(actual_spend, actual_leads)
    actual_cpd     = _safe_div(actual_spend, actual_deals)
    actual_close   = _safe_div(actual_deals, actual_leads)
    actual_revp    = actual_deals * net_commission

    def _dyn_row(name: str, full_target: float, actual_value: float) -> dict:
        dyn_target = full_target * spend_progress
        achievement = _safe_div(actual_value, dyn_target)
        return {
            "kpi":              name,
            "full_target":      round(full_target, 2) if full_target else 0,
            "spend_progress":   round(spend_progress, 4),
            "dynamic_target":   round(dyn_target, 2),
            "actual":           round(actual_value, 2) if isinstance(actual_value, float) else actual_value,
            "achievement":      round(achievement, 4) if achievement is not None else None,
            "status":           status_volume(actual_value, dyn_target),
        }

    dynamic_by_spend = {
        "spend_progress":   round(spend_progress, 4),
        "actual_spend":     round(actual_spend, 2),
        "rows": [
            _dyn_row("leads",            master["target_leads"],            actual_leads),
            _dyn_row("qualified_leads",  master["target_qualified_leads"],  actual_qleads),
            _dyn_row("meetings",         master["target_meetings"],         actual_mtg),
            _dyn_row("follow_ups",       master["target_follow_ups"],       actual_fup),
            _dyn_row("deals",            master["target_deals"],            actual_deals),
            _dyn_row("revenue_proxy",    master["target_revenue_proxy"],    actual_revp),
        ],
        "efficiency_rows": [
            {"kpi": "cpl",
             "target": master["target_cpl"], "actual": round(actual_cpl, 2) if actual_cpl is not None else None,
             "status": status_cpl_or_cost_per_deal(actual_cpl, master["target_cpl"])},
            {"kpi": "cost_per_deal",
             "target": master["target_cost_per_deal"], "actual": round(actual_cpd, 2) if actual_cpd is not None else None,
             "status": status_cpl_or_cost_per_deal(actual_cpd, master["target_cost_per_deal"])},
            {"kpi": "close_rate",
             "target": master["target_close_rate"], "actual": round(actual_close, 4) if actual_close is not None else None,
             "status": status_close_rate(actual_close, master["target_close_rate"])},
        ],
        "actuals_present": actuals_present,
    }

    # ── Section 5: Time Pacing ────────────────────────────────────────────
    pacing = time_pacing(inputs.get("start_date"), inputs.get("end_date"), inputs.get("review_date"))
    if pacing["available"]:
        time_progress = pacing["time_progress"]
        planned_spend = budget * time_progress

        def _time_row(name: str, full_target: float, actual_value: float) -> dict:
            t_target = full_target * time_progress
            achievement = _safe_div(actual_value, t_target)
            return {
                "kpi":             name,
                "full_target":     round(full_target, 2) if full_target else 0,
                "time_progress":   round(time_progress, 4),
                "time_target":     round(t_target, 2),
                "actual":          actual_value,
                "achievement":     round(achievement, 4) if achievement is not None else None,
                "status":          status_volume(actual_value, t_target),
            }

        time_pacing_section = {
            **pacing,
            "planned_spend":       round(planned_spend, 2),
            "actual_spend":        round(actual_spend, 2),
            "spend_pacing_status": status_spend_pacing(actual_spend, planned_spend),
            "rows": [
                _time_row("leads",           master["target_leads"],           actual_leads),
                _time_row("qualified_leads", master["target_qualified_leads"], actual_qleads),
                _time_row("meetings",        master["target_meetings"],        actual_mtg),
                _time_row("follow_ups",      master["target_follow_ups"],      actual_fup),
                _time_row("deals",           master["target_deals"],           actual_deals),
                _time_row("revenue_proxy",   master["target_revenue_proxy"],   actual_revp),
            ],
        }
    else:
        time_pacing_section = {**pacing, "rows": []}

    # ── Section 6: Actual vs Target Summary ───────────────────────────────
    summary_rows = []
    for vol_row in dynamic_by_spend["rows"]:
        kpi = vol_row["kpi"]
        # find matching time row
        time_row = next((r for r in time_pacing_section.get("rows", []) if r["kpi"] == kpi), None)
        summary_rows.append({
            "kpi":                    kpi,
            "full_target":            vol_row["full_target"],
            "dynamic_target_by_spend": vol_row["dynamic_target"],
            "time_based_target":      time_row["time_target"] if time_row else None,
            "actual":                 vol_row["actual"],
            "variance_vs_spend":      _safe_div(vol_row["actual"], vol_row["dynamic_target"]),
            "variance_vs_time":       (_safe_div(vol_row["actual"], time_row["time_target"]) if time_row else None),
            "status_vs_spend":        vol_row["status"],
            "status_vs_time":         time_row["status"] if time_row else "n_a",
        })

    # ── Section 7: Period Performance ─────────────────────────────────────
    by_kind = {"daily": [], "5_day": [], "weekly": [], "monthly": []}
    for p in periods:
        if not isinstance(p, PeriodRow):
            # tolerant: accept dicts too
            p = PeriodRow(**p)
        if p.period_kind in by_kind:
            by_kind[p.period_kind].append(p)
    for k in by_kind:
        by_kind[k].sort(key=lambda x: x.period_index)

    period_sections = {}
    for kind, rows in by_kind.items():
        out_rows, prev = [], None
        for r in rows:
            metrics = _period_metrics(r, net_commission, prev)
            metrics["status"] = status_volume(metrics["deals"], master["target_deals"] / max(1, len(rows)))
            out_rows.append(metrics)
            prev = r
        period_sections[kind] = out_rows

    # ── Section 8: 5-Day Health Check ─────────────────────────────────────
    health_check = []
    for p in period_sections.get("5_day", []):
        h = _five_day_health(p, master["target_cpl"], master["target_cost_per_deal"], close_rate)
        h["period"] = p["label"]
        h["period_index"] = p["index"]
        health_check.append(h)
    last_window_summary = health_check[-1]["verdict"] if health_check else None

    return {
        "overview":          overview,
        "scenarios":         scenarios,
        "scenario_funnels":  {k: _funnel_for_target_deals(v["target_deals"]) for k, v in scenarios.items()},
        "recommended":       recommended,
        "master":            master,
        "actuals":           {
            "actual_spend":           round(actual_spend, 2),
            "actual_leads":           actual_leads,
            "actual_qualified_leads": actual_qleads,
            "actual_meetings":        actual_mtg,
            "actual_follow_ups":      actual_fup,
            "actual_deals":           actual_deals,
            "actual_cpl":             round(actual_cpl, 2) if actual_cpl is not None else None,
            "actual_cost_per_deal":   round(actual_cpd, 2) if actual_cpd is not None else None,
            "actual_close_rate":      round(actual_close, 4) if actual_close is not None else None,
            "actual_revenue_proxy":   round(actual_revp, 2),
            "present":                actuals_present,
        },
        "dynamic_by_spend":  dynamic_by_spend,
        "time_pacing":       time_pacing_section,
        "summary":           summary_rows,
        "periods":           period_sections,
        "health_check_5day": {
            "rows":              health_check,
            "last_window":       last_window_summary,
        },
    }
