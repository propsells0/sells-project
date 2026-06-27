"""
Marketing blueprint — campaign CRUD + actuals tracking + dashboard
"""
import logging
import json
import psycopg2.extras
from decimal import Decimal
from datetime import datetime, date
from flask import Blueprint, request, session, Response
from app.database import get_conn
from app.auth import login_required, role_required
from app.marketing_logic import compute_dashboard, PeriodRow


def _parse_date(s):
    """Accept 'YYYY-MM-DD' / None / empty string. Returns date or None."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s).strip())
    except (ValueError, TypeError):
        return None

log = logging.getLogger(__name__)
marketing_bp = Blueprint("marketing", __name__, url_prefix="/api/marketing")


def _serial(obj):
    if isinstance(obj, Decimal):
        v = float(obj)
        return None if v != v else v
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


def _json(data, status=200):
    return Response(
        json.dumps(data, default=_serial, allow_nan=False, ensure_ascii=False),
        status=status, mimetype="application/json"
    )


# ─── List campaigns ────────────────────────────────────────────────────────────

@marketing_bp.route("/campaigns", methods=["GET"])
@role_required("marketing", "manager", "admin")
def list_campaigns():
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT c.*, u.full_name AS owner_name,
                           a.actual_spend, a.actual_leads, a.actual_deals,
                           a.updated_at AS actuals_updated
                    FROM marketing_campaigns c
                    JOIN users u ON u.id = c.user_id
                    LEFT JOIN marketing_actuals a ON a.campaign_id = c.id
                    ORDER BY c.created_at DESC
                """)
                rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return _json(rows)
    except Exception as e:
        log.error(f"list_campaigns: {e}")
        return _json({"error": str(e)}, 500)


# ─── Get single campaign ───────────────────────────────────────────────────────

@marketing_bp.route("/campaigns/<int:cid>", methods=["GET"])
@role_required("marketing", "manager", "admin")
def get_campaign(cid):
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT c.*, a.actual_spend, a.actual_leads, a.actual_qualified_leads,
                           a.actual_meetings, a.actual_follow_ups, a.actual_deals,
                           a.updated_at AS actuals_updated
                    FROM marketing_campaigns c
                    LEFT JOIN marketing_actuals a ON a.campaign_id = c.id
                    WHERE c.id = %s
                """, (cid,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return _json({"error_code": "not_found", "error": "not_found"}, 404)
        return _json(dict(row))
    except Exception as e:
        return _json({"error": str(e)}, 500)


# ─── Create campaign ───────────────────────────────────────────────────────────

@marketing_bp.route("/campaigns", methods=["POST"])
@role_required("marketing", "manager", "admin")
def create_campaign():
    data = request.get_json() or {}
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO marketing_campaigns
                        (user_id, campaign_name, avg_unit_price, commission_input,
                         commission_type, tax_rate, expected_close_rate, campaign_budget,
                         recommended_scenario, month, notes,
                         start_date, end_date, review_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (
                    session["user_id"],
                    data.get("campaign_name", "").strip(),
                    float(data.get("avg_unit_price") or 0),
                    float(data.get("commission_input") or 0),
                    data.get("commission_type", "percentage"),
                    float(data.get("tax_rate") or 19) / 100,
                    float(data.get("expected_close_rate") or 0) / 100,
                    float(data.get("campaign_budget") or 0),
                    data.get("recommended_scenario", "balanced"),
                    data.get("month") or None,
                    data.get("notes") or None,
                    _parse_date(data.get("start_date")),
                    _parse_date(data.get("end_date")),
                    _parse_date(data.get("review_date")),
                ))
                cid = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        log.info(f"✅ Campaign created: id={cid}")
        return _json({"id": cid, "ok": True}, 201)
    except Exception as e:
        log.error(f"create_campaign: {e}")
        return _json({"error": str(e)}, 500)


# ─── Update campaign inputs ────────────────────────────────────────────────────

@marketing_bp.route("/campaigns/<int:cid>", methods=["PUT"])
@role_required("marketing", "manager", "admin")
def update_campaign(cid):
    data = request.get_json() or {}
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM marketing_campaigns WHERE id = %s", (cid,))
                if not cur.fetchone():
                    return _json({"error_code": "not_found", "error": "not_found"}, 404)

                cur.execute("""
                    UPDATE marketing_campaigns SET

                        campaign_name        = %s,
                        avg_unit_price       = %s,
                        commission_input     = %s,
                        commission_type      = %s,
                        tax_rate             = %s,
                        expected_close_rate  = %s,
                        campaign_budget      = %s,
                        recommended_scenario = %s,
                        month                = %s,
                        notes                = %s,
                        start_date           = %s,
                        end_date             = %s,
                        review_date          = %s,
                        updated_at           = NOW()
                    WHERE id = %s
                """, (
                    data.get("campaign_name", "").strip(),
                    float(data.get("avg_unit_price") or 0),
                    float(data.get("commission_input") or 0),
                    data.get("commission_type", "percentage"),
                    float(data.get("tax_rate") or 19) / 100,
                    float(data.get("expected_close_rate") or 0) / 100,
                    float(data.get("campaign_budget") or 0),
                    data.get("recommended_scenario", "balanced"),
                    data.get("month") or None,
                    data.get("notes") or None,
                    _parse_date(data.get("start_date")),
                    _parse_date(data.get("end_date")),
                    _parse_date(data.get("review_date")),
                    cid,
                ))
            conn.commit()
        finally:
            conn.close()
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)


# ─── Save actuals ──────────────────────────────────────────────────────────────

@marketing_bp.route("/campaigns/<int:cid>/actuals", methods=["PUT"])
@role_required("marketing", "manager", "admin")
def save_actuals(cid):
    data = request.get_json() or {}
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO marketing_actuals
                        (campaign_id, actual_spend, actual_leads, actual_qualified_leads,
                         actual_meetings, actual_follow_ups, actual_deals)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (campaign_id) DO UPDATE SET
                        actual_spend             = EXCLUDED.actual_spend,
                        actual_leads             = EXCLUDED.actual_leads,
                        actual_qualified_leads   = EXCLUDED.actual_qualified_leads,
                        actual_meetings          = EXCLUDED.actual_meetings,
                        actual_follow_ups        = EXCLUDED.actual_follow_ups,
                        actual_deals             = EXCLUDED.actual_deals,
                        updated_at               = NOW()
                """, (
                    cid,
                    float(data.get("actual_spend") or 0),
                    int(data.get("actual_leads") or 0),
                    int(data.get("actual_qualified_leads") or 0),
                    int(data.get("actual_meetings") or 0),
                    int(data.get("actual_follow_ups") or 0),
                    int(data.get("actual_deals") or 0),
                ))
            conn.commit()
        finally:
            conn.close()
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)


# ─── Computed dashboard (Section 05) ──────────────────────────────────────────

def _check_campaign_access(cur, cid):
    """Returns (row, err_response) — caller raises err_response if non-None."""
    cur.execute("SELECT id, user_id FROM marketing_campaigns WHERE id = %s", (cid,))
    row = cur.fetchone()
    if not row:
        return None, _json({"error_code": "not_found", "error": "not_found"}, 404)
    return row, None


@marketing_bp.route("/campaigns/<int:cid>/dashboard", methods=["GET"])
@role_required("marketing", "manager", "admin")
def campaign_dashboard(cid):
    """
    Compute the full marketing dashboard payload for one campaign — single
    round-trip for the frontend. Bundles inputs, cumulative actuals, and
    period rows; runs them through app.marketing_logic.compute_dashboard.
    """
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                _, err = _check_campaign_access(cur, cid)
                if err is not None:
                    return err
                cur.execute("SELECT * FROM marketing_campaigns WHERE id = %s", (cid,))
                campaign = dict(cur.fetchone())
                cur.execute("SELECT * FROM marketing_actuals WHERE campaign_id = %s", (cid,))
                a = cur.fetchone()
                actuals = dict(a) if a else {}
                cur.execute("""
                    SELECT period_kind, period_index, period_label,
                           period_start, period_end,
                           spend, leads, qualified_leads, meetings, follow_ups, deals, notes
                    FROM marketing_period_actuals
                    WHERE campaign_id = %s
                    ORDER BY period_kind, period_index
                """, (cid,))
                period_rows = [PeriodRow(**dict(r)) for r in cur.fetchall()]
        finally:
            conn.close()

        # campaign[tax_rate / expected_close_rate] are stored as decimals already.
        inputs = {
            "campaign_name":       campaign.get("campaign_name"),
            "avg_unit_price":      campaign.get("avg_unit_price"),
            "commission_input":    campaign.get("commission_input"),
            "commission_type":     campaign.get("commission_type"),
            "tax_rate":            campaign.get("tax_rate"),
            "expected_close_rate": campaign.get("expected_close_rate"),
            "campaign_budget":     campaign.get("campaign_budget"),
            "start_date":          campaign.get("start_date"),
            "end_date":            campaign.get("end_date"),
            "review_date":         campaign.get("review_date"),
            "recommended_scenario": campaign.get("recommended_scenario"),
        }
        # Strip the actuals-table-only fields the engine doesn't expect.
        actuals_clean = {k: actuals.get(k) for k in (
            "actual_spend", "actual_leads", "actual_qualified_leads",
            "actual_meetings", "actual_follow_ups", "actual_deals",
        ) if k in actuals}

        payload = compute_dashboard(inputs, actuals_clean, period_rows)
        payload["campaign_id"] = cid
        return _json(payload)
    except Exception as e:
        log.error(f"campaign_dashboard {cid}: {e}")
        return _json({"error_code": "server", "error": str(e)}, 500)


# ─── Period actuals CRUD ──────────────────────────────────────────────────────

VALID_PERIOD_KINDS = {"daily", "5_day", "weekly", "monthly"}


@marketing_bp.route("/campaigns/<int:cid>/periods", methods=["GET"])
@role_required("marketing", "manager", "admin")
def list_periods(cid):
    """Return all period rows for one campaign, optionally filtered by kind."""
    kind = (request.args.get("kind") or "").strip().lower() or None
    if kind and kind not in VALID_PERIOD_KINDS:
        return _json({"error_code": "invalid_period_kind", "error": "invalid"}, 400)
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                _, err = _check_campaign_access(cur, cid)
                if err is not None:
                    return err
                if kind:
                    cur.execute("""
                        SELECT * FROM marketing_period_actuals
                        WHERE campaign_id = %s AND period_kind = %s
                        ORDER BY period_index
                    """, (cid, kind))
                else:
                    cur.execute("""
                        SELECT * FROM marketing_period_actuals
                        WHERE campaign_id = %s
                        ORDER BY period_kind, period_index
                    """, (cid,))
                rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return _json(rows)
    except Exception as e:
        log.error(f"list_periods {cid}: {e}")
        return _json({"error_code": "server", "error": str(e)}, 500)


@marketing_bp.route("/campaigns/<int:cid>/periods", methods=["POST"])
@role_required("marketing", "manager", "admin")
def upsert_period(cid):
    """
    Insert or update one period row. Body:
      { period_kind, period_index, period_label,
        period_start, period_end,
        spend, leads, qualified_leads, meetings, follow_ups, deals, notes }
    Uniqueness on (campaign_id, period_kind, period_index) so a re-POST with
    the same index updates rather than duplicates.
    """
    data = request.get_json() or {}
    kind = (data.get("period_kind") or "").strip().lower()
    if kind not in VALID_PERIOD_KINDS:
        return _json({"error_code": "invalid_period_kind", "error": "invalid"}, 400)
    try:
        idx = int(data.get("period_index"))
    except (TypeError, ValueError):
        return _json({"error_code": "invalid_period_index", "error": "invalid"}, 400)
    label = (data.get("period_label") or "").strip()
    if not label:
        return _json({"error_code": "required_fields_missing", "error": "label"}, 400)

    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                _, err = _check_campaign_access(cur, cid)
                if err is not None:
                    return err
                cur.execute("""
                    INSERT INTO marketing_period_actuals
                        (campaign_id, period_kind, period_index, period_label,
                         period_start, period_end,
                         spend, leads, qualified_leads, meetings, follow_ups, deals, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (campaign_id, period_kind, period_index) DO UPDATE SET
                        period_label    = EXCLUDED.period_label,
                        period_start    = EXCLUDED.period_start,
                        period_end      = EXCLUDED.period_end,
                        spend           = EXCLUDED.spend,
                        leads           = EXCLUDED.leads,
                        qualified_leads = EXCLUDED.qualified_leads,
                        meetings        = EXCLUDED.meetings,
                        follow_ups      = EXCLUDED.follow_ups,
                        deals           = EXCLUDED.deals,
                        notes           = EXCLUDED.notes,
                        updated_at      = NOW()
                """, (
                    cid, kind, idx, label,
                    _parse_date(data.get("period_start")),
                    _parse_date(data.get("period_end")),
                    float(data.get("spend") or 0),
                    int(data.get("leads") or 0),
                    int(data.get("qualified_leads") or 0),
                    int(data.get("meetings") or 0),
                    int(data.get("follow_ups") or 0),
                    int(data.get("deals") or 0),
                    data.get("notes") or None,
                ))
            conn.commit()
        finally:
            conn.close()
        return _json({"ok": True})
    except Exception as e:
        log.error(f"upsert_period {cid}: {e}")
        return _json({"error_code": "server", "error": str(e)}, 500)


@marketing_bp.route("/campaigns/<int:cid>/periods/<kind>/<int:idx>", methods=["DELETE"])
@role_required("marketing", "manager", "admin")
def delete_period(cid, kind, idx):
    if kind not in VALID_PERIOD_KINDS:
        return _json({"error_code": "invalid_period_kind", "error": "invalid"}, 400)
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                _, err = _check_campaign_access(cur, cid)
                if err is not None:
                    return err
                cur.execute("""
                    DELETE FROM marketing_period_actuals
                    WHERE campaign_id = %s AND period_kind = %s AND period_index = %s
                """, (cid, kind, idx))
            conn.commit()
        finally:
            conn.close()
        return _json({"ok": True})
    except Exception as e:
        log.error(f"delete_period {cid}/{kind}/{idx}: {e}")
        return _json({"error_code": "server", "error": str(e)}, 500)


# ─── Delete campaign ───────────────────────────────────────────────────────────

@marketing_bp.route("/campaigns/<int:cid>", methods=["DELETE"])
@role_required("marketing", "manager", "admin")
def delete_campaign(cid):
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM marketing_campaigns WHERE id = %s", (cid,))
                if not cur.fetchone():
                    return _json({"error_code": "not_found", "error": "not_found"}, 404)
                cur.execute("DELETE FROM marketing_campaigns WHERE id = %s", (cid,))
            conn.commit()
        finally:
            conn.close()
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)
