"""
KPI configuration, scoring logic, and financial projections.
Single source of truth — used by backend scoring + frontend config.
"""

KPI_CONFIG = {
    "calls":          {"weight": 15, "label_en": "Calls",         "label_ar": "المكالمات",        "input_type": "number",   "target_type": "fixed",     "target": 2000},
    "meetings":       {"weight": 8,  "label_en": "Meetings",      "label_ar": "الاجتماعات",       "input_type": "number",   "target_type": "leads_pct", "target_pct": 0.20},
    "crm_pct":        {"weight": 10, "label_en": "CRM Update",    "label_ar": "تحديث CRM",        "input_type": "percent",  "target_type": "fixed",     "target": 95},
    "deals":          {"weight": 10, "label_en": "Deals",         "label_ar": "الصفقات",          "input_type": "number",   "target_type": "leads_pct", "target_pct": 0.03},
    "reports":        {"weight": 8,  "label_en": "Reports",       "label_ar": "التقارير",         "input_type": "number",   "target_type": "fixed",     "target": 4},
    "reservations":   {"weight": 7,  "label_en": "Reservations",  "label_ar": "الحجوزات",         "input_type": "number",   "target_type": "leads_pct", "target_pct": 0.07},
    "attitude":       {"weight": 4,  "label_en": "Attitude",      "label_ar": "السلوك",           "input_type": "passfail", "target_type": "fixed",     "target": 100},
    "presentation":   {"weight": 4,  "label_en": "Presentation",  "label_ar": "العرض",            "input_type": "passfail", "target_type": "fixed",     "target": 100},
    "followup_pct":   {"weight": 15, "label_en": "Follow-up",     "label_ar": "متابعة العملاء",   "input_type": "percent",  "target_type": "fixed",     "target": 100},
    "behaviour":      {"weight": 4,  "label_en": "Behaviour",     "label_ar": "التصرف",           "input_type": "passfail", "target_type": "fixed",     "target": 100},
    "appearance":     {"weight": 4,  "label_en": "Appearance",    "label_ar": "المظهر",           "input_type": "passfail", "target_type": "fixed",     "target": 100},
    "attendance_pct": {"weight": 7,  "label_en": "Attendance",    "label_ar": "الحضور",           "input_type": "percent",  "target_type": "fixed",     "target": 100},
    "hr_roles":       {"weight": 4,  "label_en": "HR Roles",      "label_ar": "التزامات HR",      "input_type": "passfail", "target_type": "fixed",     "target": 100},
}

SALES_FIELDS = ["fresh_leads", "calls", "meetings", "crm_pct", "deals",
                "reports", "reservations", "followup_pct", "attendance_pct"]

DATAENTRY_FIELDS = ["attitude", "presentation", "behaviour", "appearance", "hr_roles"]

RATINGS = [
    (90, "Excellent", "ممتاز"),
    (75, "V.Good",    "جيد جداً"),
    (55, "Good",      "جيد"),
    (40, "Medium",    "متوسط"),
    (25, "Weak",      "ضعيف"),
    (0,  "Bad",       "ضعيف جداً"),
]

# Financial projection defaults (can be overridden via env or settings)
FINANCIAL_DEFAULTS = {
    "avg_deal_value_egp": 2_500_000,      # متوسط قيمة الصفقة
    "commission_rate": 0.025,              # 2.5% عمولة
    "avg_reservation_value_egp": 150_000,  # قيمة الحجز
    "reservation_commission_rate": 0.01,   # 1% عمولة حجز
}


def get_target(key: str, fresh_leads: float) -> float:
    cfg = KPI_CONFIG[key]
    if cfg["target_type"] == "fixed":
        return float(cfg["target"])
    if cfg["target_type"] == "leads_pct":
        return float(fresh_leads) * float(cfg["target_pct"])
    return 100.0


def get_rating(total_score: float):
    """Returns (label_en, label_ar)"""
    for threshold, en, ar in RATINGS:
        if total_score >= threshold:
            return en, ar
    return "Bad", "ضعيف جداً"


def compute_score(entry: dict):
    """
    Compute weighted KPI score.
    Returns: (total_score, rating_en, breakdown_dict)
    """
    L = float(entry.get("fresh_leads") or 0)
    breakdown = {}
    total = 0.0

    for key, cfg in KPI_CONFIG.items():
        actual = float(entry.get(key) or 0)
        target = get_target(key, L)

        if cfg["input_type"] == "passfail":
            achievement = actual / 100.0
        else:
            achievement = min(actual / target, 1.0) if target > 0 else 0.0

        weighted = achievement * cfg["weight"]
        total += weighted

        breakdown[key] = {
            "label_en": cfg["label_en"],
            "label_ar": cfg["label_ar"],
            "actual": actual,
            "target": round(target, 2),
            "achievement_pct": round(achievement * 100, 1),
            "weight": cfg["weight"],
            "weighted_score": round(weighted, 2),
        }

    total = round(total, 2)
    rating_en, _ = get_rating(total)
    return total, rating_en, breakdown


# ─── Team Leader KPI Config ────────────────────────────────────────────────────

TL_KPI_CONFIG = {
    "calls":            {"weight": 15, "label_en": "Team Calls",        "label_ar": "مكالمات الفريق",    "source": "team_sum",       "base_target": 2000,  "threshold_pct": 0.90},
    "meetings":         {"weight": 7,  "label_en": "Team Meetings",     "label_ar": "اجتماعات الفريق",   "source": "team_leads_sum", "leads_pct": 0.20,    "threshold_pct": 0.90},
    "deals":            {"weight": 8,  "label_en": "Team Deals",        "label_ar": "صفقات الفريق",      "source": "team_leads_sum", "leads_pct": 0.03,    "threshold_pct": 0.70},
    "reports":          {"weight": 8,  "label_en": "Reporting",         "label_ar": "التقارير",          "source": "manual",         "target": 4},
    "reservations":     {"weight": 5,  "label_en": "Team Reservations", "label_ar": "حجوزات الفريق",     "source": "team_leads_sum", "leads_pct": 0.07,    "threshold_pct": 0.70},
    "clients_pipeline": {"weight": 10, "label_en": "Clients Pipeline",  "label_ar": "قائمة العملاء",     "source": "manual",         "target": 80},
    # CRM split: team-aggregate (auto from reports) vs TL's own (manual by manager).
    "crm_pct_team":     {"weight": 5,  "label_en": "CRM (Team Avg)",    "label_ar": "متابعة CRM للفريق", "source": "team_avg",       "team_field": "crm_pct", "target": 95},
    "crm_pct":          {"weight": 5,  "label_en": "CRM (Own)",         "label_ar": "متابعة CRM (الخاصة)","source": "manual",         "target": 95},
    "attitude":         {"weight": 3,  "label_en": "Attitude",          "label_ar": "السلوك",            "source": "manual",         "target": 100, "passfail": True},
    "presentation":     {"weight": 3,  "label_en": "Presentation",      "label_ar": "العرض",             "source": "manual",         "target": 100, "passfail": True},
    "followup_pct":     {"weight": 15, "label_en": "Team Follow-up",    "label_ar": "متابعة الفريق",     "source": "team_sum",       "base_target": 100,   "threshold_pct": 1.00},
    "behaviour":        {"weight": 3,  "label_en": "Behaviour",         "label_ar": "التصرف",            "source": "manual",         "target": 100, "passfail": True},
    "appearance":       {"weight": 3,  "label_en": "Appearance",        "label_ar": "المظهر",            "source": "manual",         "target": 100, "passfail": True},
    "attendance_pct":   {"weight": 7,  "label_en": "Attendance",        "label_ar": "الحضور",            "source": "manual",         "target": 100, "passfail": True},
    "hr_roles":         {"weight": 3,  "label_en": "HR Roles",          "label_ar": "التزامات HR",       "source": "manual",         "target": 100, "passfail": True},
}

# Auto = pulled/aggregated from team. Manual = filled by Sales Manager on TL eval page.
TL_AUTO_FIELDS   = ["calls", "meetings", "deals", "reservations", "followup_pct", "crm_pct_team"]
TL_MANUAL_FIELDS = ["reports", "clients_pipeline", "crm_pct", "attitude", "presentation",
                    "behaviour", "appearance", "attendance_pct", "hr_roles"]


def compute_tl_score(tl_entry: dict, team_entries: list):
    """
    Compute KPI score for a Team Leader.
    tl_entry: TL's own kpi_entries row (manual fields + own crm_pct).
    team_entries: kpi_entries rows for all submitted sales under the TL.
    """
    n = len(team_entries)
    breakdown = {}
    total = 0.0

    for key, cfg in TL_KPI_CONFIG.items():
        source = cfg["source"]
        weight = cfg["weight"]

        if source == "team_sum":
            actual = sum(float(e.get(key) or 0) for e in team_entries)
            raw_target = n * cfg["base_target"]
            target = raw_target * cfg["threshold_pct"]
            achievement = min(actual / target, 1.0) if target > 0 else 0.0

        elif source == "team_leads_sum":
            actual = sum(float(e.get(key) or 0) for e in team_entries)
            team_leads = sum(float(e.get("fresh_leads") or 0) for e in team_entries)
            raw_target = team_leads * cfg["leads_pct"]
            target = raw_target * cfg["threshold_pct"]
            achievement = min(actual / target, 1.0) if target > 0 else 0.0

        elif source == "team_plus_self":
            actual = sum(float(e.get(key) or 0) for e in team_entries) + float(tl_entry.get(key) or 0)
            target = (n + 1) * cfg["target_each"]
            achievement = min(actual / target, 1.0) if target > 0 else 0.0

        elif source == "team_avg":
            # Average a field across the team (e.g. team CRM%). `team_field`
            # points to the column name in kpi_entries since the config key
            # may differ (e.g. crm_pct_team → team_field="crm_pct").
            team_key = cfg.get("team_field", key)
            values = [float(e.get(team_key) or 0) for e in team_entries]
            actual = (sum(values) / len(values)) if values else 0.0
            target = float(cfg["target"])
            achievement = min(actual / target, 1.0) if target > 0 else 0.0

        else:  # manual
            actual = float(tl_entry.get(key) or 0)
            target = float(cfg["target"])
            if cfg.get("passfail"):
                achievement = actual / 100.0
            else:
                achievement = min(actual / target, 1.0) if target > 0 else 0.0

        weighted = achievement * weight
        total += weighted

        breakdown[key] = {
            "label_en": cfg["label_en"],
            "label_ar": cfg["label_ar"],
            "actual": round(actual, 2),
            "target": round(target, 2),
            "achievement_pct": round(achievement * 100, 1),
            "weight": weight,
            "weighted_score": round(weighted, 2),
            "source": source,
        }

    total = round(total, 2)
    rating_en, _ = get_rating(total)
    return total, rating_en, breakdown


def compute_financials(entry: dict, settings: dict = None):
    """
    Translate KPI numbers into financial projections.
    Returns dict with revenue, commission, and deal value metrics.
    """
    s = {**FINANCIAL_DEFAULTS, **(settings or {})}
    deals = float(entry.get("deals") or 0)
    reservations = float(entry.get("reservations") or 0)

    deal_revenue = deals * s["avg_deal_value_egp"]
    reservation_revenue = reservations * s["avg_reservation_value_egp"]
    total_revenue = deal_revenue + reservation_revenue

    deal_commission = deal_revenue * s["commission_rate"]
    reservation_commission = reservation_revenue * s["reservation_commission_rate"]
    total_commission = deal_commission + reservation_commission

    return {
        "deals_count": int(deals),
        "reservations_count": int(reservations),
        "deal_revenue": round(deal_revenue, 2),
        "reservation_revenue": round(reservation_revenue, 2),
        "total_revenue": round(total_revenue, 2),
        "deal_commission": round(deal_commission, 2),
        "reservation_commission": round(reservation_commission, 2),
        "total_commission": round(total_commission, 2),
        "avg_deal_value": s["avg_deal_value_egp"],
        "commission_rate_pct": s["commission_rate"] * 100,
    }
