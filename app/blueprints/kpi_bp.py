"""
KPI blueprint — entry CRUD, reports, breakdowns
"""
import json
import logging
import psycopg2.extras
from decimal import Decimal
from datetime import datetime, date, timedelta
from flask import Blueprint, request, session, Response

# Half-open interval helper for timestamp filters: [from 00:00:00, to+1 00:00:00)
# captures the full last day without subsecond fudging.
_ONE_DAY = timedelta(days=1)
from app.database import get_conn
from app.auth import login_required, role_required, rate_limit
from app.kpi_logic import (
    KPI_CONFIG, SALES_FIELDS, DATAENTRY_FIELDS, compute_score,
    TL_KPI_CONFIG, TL_AUTO_FIELDS, TL_MANUAL_FIELDS, compute_tl_score,
)
from app.util.audit import audit_query
from app.util.date_range import parse_range, InvalidRangeError

# Soft cap on response size for range-aware endpoints. Above this, callers
# get 413 with a clear error code. TODO: paginate when consistently exceeding 5K.
_RANGE_ROW_CAP = 10_000


def _range_where(pr, *, alias="e", ts_field="dataentry"):
    """
    Translate a ParsedRange into a (sql_fragment, params) pair for filtering
    kpi_entries. Three paths matching commit 4's report() endpoint:
      - calendar-month-aligned → e.month = %s (existing fastpath)
      - sub-month → e.<ts_col> half-open interval [from, to+1)
      - multi-month aligned → e.month BETWEEN 'YYYY-MM' AND 'YYYY-MM'
    """
    a = alias
    if pr.month_str:
        return (f" AND {a}.month = %s ", [pr.month_str])
    if pr.is_sub_month:
        ts_col = "dataentry_submitted_at" if ts_field == "dataentry" else "sales_submitted_at"
        return (
            f" AND {a}.{ts_col} >= %s AND {a}.{ts_col} < %s ",
            [pr.from_date, pr.to_date + _ONE_DAY],
        )
    return (
        f" AND {a}.month BETWEEN %s AND %s ",
        [
            f"{pr.from_date.year:04d}-{pr.from_date.month:02d}",
            f"{pr.to_date.year:04d}-{pr.to_date.month:02d}",
        ],
    )

log = logging.getLogger(__name__)
kpi_bp = Blueprint("kpi", __name__, url_prefix="/api/kpi")


# ─── JSON helpers ──────────────────────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, Decimal):
        v = float(obj)
        return None if v != v else v
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def _json(data, status=200):
    return Response(
        json.dumps(data, default=_json_default, allow_nan=False, ensure_ascii=False),
        status=status,
        mimetype="application/json"
    )


def _entry_to_dict(row):
    return dict(row)


def _recompute_and_save(conn, entry_id):
    """Recompute score for an entry and persist it. Returns (total, rating)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM kpi_entries WHERE id = %s", (entry_id,))
        entry = cur.fetchone()
        if not entry:
            return None, None
        total, rating, _ = compute_score(dict(entry))
        cur.execute("""
            UPDATE kpi_entries SET total_score = %s, rating = %s, updated_at = NOW()
            WHERE id = %s
        """, (total, rating, entry_id))
    return total, rating


# ─── Config (KPI weights/targets for frontend) ─────────────────────────────────

@kpi_bp.route("/config", methods=["GET"])
@login_required
def get_config():
    return _json({
        "kpis": KPI_CONFIG,
        "sales_fields": SALES_FIELDS,
        "dataentry_fields": DATAENTRY_FIELDS,
        "tl_kpis": TL_KPI_CONFIG,
        "tl_auto_fields": TL_AUTO_FIELDS,
        "tl_manual_fields": TL_MANUAL_FIELDS,
    })


# ─── Months list ───────────────────────────────────────────────────────────────

@kpi_bp.route("/months", methods=["GET"])
@login_required
def list_months():
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT month FROM kpi_entries ORDER BY month DESC")
                months = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        return _json(months)
    except Exception as e:
        log.error(f"months error: {e}")
        return _json({"error": str(e)}, 500)


# ─── Submit Sales numeric KPI (DataEntry / Admin / Manager) ───────────────────
# Sales role no longer enters its own data — DataEntry enters on their behalf.

@kpi_bp.route("/submit/sales", methods=["POST"])
@role_required("dataentry", "admin", "manager")
def submit_sales():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    month = data.get("month")

    if not user_id or not month:
        return _json({"error_code": "required_fields_missing", "error": "required"}, 400)

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    INSERT INTO kpi_entries (user_id, month,
                        fresh_leads, calls, meetings, crm_pct, deals,
                        reports, reservations, followup_pct, attendance_pct,
                        sales_submitted_at)
                    VALUES (%(user_id)s, %(month)s,
                        %(fresh_leads)s, %(calls)s, %(meetings)s, %(crm_pct)s, %(deals)s,
                        %(reports)s, %(reservations)s, %(followup_pct)s, %(attendance_pct)s,
                        NOW())
                    ON CONFLICT (user_id, month) DO UPDATE SET
                        fresh_leads = EXCLUDED.fresh_leads,
                        calls = EXCLUDED.calls,
                        meetings = EXCLUDED.meetings,
                        crm_pct = EXCLUDED.crm_pct,
                        deals = EXCLUDED.deals,
                        reports = EXCLUDED.reports,
                        reservations = EXCLUDED.reservations,
                        followup_pct = EXCLUDED.followup_pct,
                        attendance_pct = EXCLUDED.attendance_pct,
                        sales_submitted_at = NOW(),
                        updated_at = NOW()
                    RETURNING id
                """, {
                    "user_id": user_id, "month": month,
                    "fresh_leads": int(data.get("fresh_leads") or 0),
                    "calls": int(data.get("calls") or 0),
                    "meetings": int(data.get("meetings") or 0),
                    "crm_pct": float(data.get("crm_pct") or 0),
                    "deals": int(data.get("deals") or 0),
                    "reports": int(data.get("reports") or 0),
                    "reservations": int(data.get("reservations") or 0),
                    "followup_pct": float(data.get("followup_pct") or 0),
                    "attendance_pct": float(data.get("attendance_pct") or 0),
                })
                entry_id = cur.fetchone()["id"]
                total, rating = _recompute_and_save(conn, entry_id)
            conn.commit()
        finally:
            conn.close()
        log.info(f"✅ Sales submit: user={user_id} month={month} score={total}")
        return _json({"ok": True, "total_score": total, "rating": rating})
    except Exception as e:
        log.error(f"Sales submit error: {e}")
        return _json({"error": str(e)}, 500)


# ─── DataEntry / Manager submits full KPI entry (numeric + pass/fail) ─────────
# Since Sales no longer self-submit, DataEntry fills BOTH the numeric performance
# fields AND the pass/fail evaluation in a single shot.

_NUMERIC_FIELDS = (
    "fresh_leads", "calls", "meetings", "crm_pct", "deals",
    "reports", "reservations", "followup_pct", "attendance_pct",
)
_EVAL_FIELDS = ("attitude", "presentation", "behaviour", "appearance", "hr_roles")


def _coerce(data, key, cast, default=0):
    v = data.get(key)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


@kpi_bp.route("/submit/evaluation", methods=["POST"])
@role_required("dataentry", "manager", "admin")
def submit_evaluation():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    month = data.get("month")
    if not user_id or not month:
        return _json({"error_code": "required_fields_missing", "error": "required"}, 400)

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Upsert on (user_id, month). If the caller passed the numeric
                # fields, write them AND flip sales_submitted_at so the entry
                # counts as "delivered"; if only eval fields were sent, leave
                # the numeric ones untouched.
                has_numeric = any(k in data for k in _NUMERIC_FIELDS)

                params = {
                    "user_id": user_id,
                    "month": month,
                    "notes": data.get("notes") or None,
                    "dataentry_by": session["user_id"],
                    # Numeric (only used if has_numeric)
                    "fresh_leads": _coerce(data, "fresh_leads", int),
                    "calls":       _coerce(data, "calls", int),
                    "meetings":    _coerce(data, "meetings", int),
                    "crm_pct":     _coerce(data, "crm_pct", float),
                    "deals":       _coerce(data, "deals", int),
                    "reports":     _coerce(data, "reports", int),
                    "reservations":_coerce(data, "reservations", int),
                    "followup_pct":_coerce(data, "followup_pct", float),
                    "attendance_pct": _coerce(data, "attendance_pct", float),
                    # Pass/fail
                    "attitude":     _coerce(data, "attitude", int),
                    "presentation": _coerce(data, "presentation", int),
                    "behaviour":    _coerce(data, "behaviour", int),
                    "appearance":   _coerce(data, "appearance", int),
                    "hr_roles":     _coerce(data, "hr_roles", int),
                }

                if has_numeric:
                    cur.execute("""
                        INSERT INTO kpi_entries (user_id, month,
                            fresh_leads, calls, meetings, crm_pct, deals,
                            reports, reservations, followup_pct, attendance_pct,
                            attitude, presentation, behaviour, appearance, hr_roles,
                            notes, dataentry_by,
                            sales_submitted_at, dataentry_submitted_at)
                        VALUES (%(user_id)s, %(month)s,
                            %(fresh_leads)s, %(calls)s, %(meetings)s, %(crm_pct)s, %(deals)s,
                            %(reports)s, %(reservations)s, %(followup_pct)s, %(attendance_pct)s,
                            %(attitude)s, %(presentation)s, %(behaviour)s, %(appearance)s, %(hr_roles)s,
                            %(notes)s, %(dataentry_by)s,
                            NOW(), NOW())
                        ON CONFLICT (user_id, month) DO UPDATE SET
                            fresh_leads = EXCLUDED.fresh_leads,
                            calls = EXCLUDED.calls,
                            meetings = EXCLUDED.meetings,
                            crm_pct = EXCLUDED.crm_pct,
                            deals = EXCLUDED.deals,
                            reports = EXCLUDED.reports,
                            reservations = EXCLUDED.reservations,
                            followup_pct = EXCLUDED.followup_pct,
                            attendance_pct = EXCLUDED.attendance_pct,
                            attitude = EXCLUDED.attitude,
                            presentation = EXCLUDED.presentation,
                            behaviour = EXCLUDED.behaviour,
                            appearance = EXCLUDED.appearance,
                            hr_roles = EXCLUDED.hr_roles,
                            notes = EXCLUDED.notes,
                            dataentry_by = EXCLUDED.dataentry_by,
                            sales_submitted_at = NOW(),
                            dataentry_submitted_at = NOW(),
                            updated_at = NOW()
                        RETURNING id
                    """, params)
                else:
                    # Eval-only update — keep numeric fields untouched, create
                    # the row if it doesn't exist yet.
                    cur.execute("""
                        INSERT INTO kpi_entries (user_id, month,
                            attitude, presentation, behaviour, appearance, hr_roles,
                            notes, dataentry_by, dataentry_submitted_at)
                        VALUES (%(user_id)s, %(month)s,
                            %(attitude)s, %(presentation)s, %(behaviour)s, %(appearance)s, %(hr_roles)s,
                            %(notes)s, %(dataentry_by)s, NOW())
                        ON CONFLICT (user_id, month) DO UPDATE SET
                            attitude = EXCLUDED.attitude,
                            presentation = EXCLUDED.presentation,
                            behaviour = EXCLUDED.behaviour,
                            appearance = EXCLUDED.appearance,
                            hr_roles = EXCLUDED.hr_roles,
                            notes = EXCLUDED.notes,
                            dataentry_by = EXCLUDED.dataentry_by,
                            dataentry_submitted_at = NOW(),
                            updated_at = NOW()
                        RETURNING id
                    """, params)

                entry_id = cur.fetchone()["id"]
                total, rating = _recompute_and_save(conn, entry_id)
            conn.commit()
        finally:
            conn.close()
        log.info(f"✅ Evaluation submit: user={user_id} month={month} score={total}")
        return _json({"ok": True, "total_score": total, "rating": rating})
    except Exception as e:
        log.error(f"Evaluation submit error: {e}")
        return _json({"error_code": "server", "error": "server"}, 500)


# ─── Get a single entry ────────────────────────────────────────────────────────

@kpi_bp.route("/entry/<int:user_id>/<month>", methods=["GET"])
@login_required
def get_entry(user_id, month):
    # Sales role has no access to KPI entries
    if session.get("role") == "sales":
        return _json({"error_code": "forbidden", "error": "forbidden"}, 403)

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT e.*, u.full_name AS user_name, u.username, u.avatar_url
                    FROM kpi_entries e
                    JOIN users u ON u.id = e.user_id
                    WHERE e.user_id = %s AND e.month = %s
                """, (user_id, month))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return _json(None)
        entry = dict(row)
        _, _, breakdown = compute_score(entry)
        entry["breakdown"] = breakdown
        return _json(entry)
    except Exception as e:
        log.error(f"get_entry error: {e}")
        return _json({"error": str(e)}, 500)


# ─── Delete entry ──────────────────────────────────────────────────────────────

@kpi_bp.route("/entry/<int:entry_id>", methods=["DELETE"])
@role_required("admin", "manager")
def delete_entry(entry_id):
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kpi_entries WHERE id = %s", (entry_id,))
                if cur.rowcount == 0:
                    return _json({"error_code": "not_found", "error": "not_found"}, 404)
            conn.commit()
        finally:
            conn.close()
        return _json({"ok": True})
    except Exception as e:
        return _json({"error": str(e)}, 500)


# ─── Report: all entries with filters ──────────────────────────────────────────

@kpi_bp.route("/report", methods=["GET"])
@login_required
@rate_limit("kpi_range_query", limit=30, window=60)
@audit_query
def report():
    """
    KPI report rows.

    Filtering (resolution order — see app.util.date_range.parse_range):
      ?from=YYYY-MM-DD&to=YYYY-MM-DD     explicit range
      ?preset=this_month|last_7|...      named preset
      ?month=YYYY-MM                     legacy compat (single calendar month)
      (default)                          this_month

    When the resolved range is exactly one calendar month, we keep the existing
    e.month = %s fastpath. When it's sub-month, we filter by submission
    timestamp (?ts_field=dataentry|sales, default 'dataentry') because the
    monthly grain has no day-level data — see CLAUDE.md "monthly grain" note.

    Multi-month ranges return one row per (user, month) from the join. The
    dashboard leaderboard wants ONE row per user (latest month wins) so the
    same person doesn't appear three times for January / February / March.
    Pass `?dedupe=user` to enable that collapse — front-end pages that need
    the per-month rows for trend math leave it off.

    Other params:
      ?user_id=N           filter to one rep
      ?detail=1            include compute_score breakdown
    """
    user_id_filter = request.args.get("user_id")
    want_detail = request.args.get("detail") in ("1", "true", "yes")
    dedupe_mode = (request.args.get("dedupe") or "").lower()
    ts_field = request.args.get("ts_field", "dataentry").lower()
    if ts_field not in ("dataentry", "sales"):
        ts_field = "dataentry"

    if session.get("role") == "sales":
        return _json({"error_code": "forbidden", "error": "forbidden"}, 403)

    try:
        pr = parse_range(request.args)
    except InvalidRangeError as e:
        return _json({"error_code": e.code, "error": e.code}, 400)

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                q = """
                    SELECT e.*, u.full_name AS user_name, u.username, u.avatar_url
                    FROM kpi_entries e
                    JOIN users u ON u.id = e.user_id
                    WHERE u.active = true
                """
                where, params = _range_where(pr, alias="e", ts_field=ts_field)
                q += where
                if user_id_filter:
                    q += " AND e.user_id = %s"
                    params.append(int(user_id_filter))
                q += " ORDER BY e.total_score DESC, u.full_name"
                cur.execute(q, params)
                rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        # TODO: paginate when consistently exceeding 5K rows.
        if len(rows) > _RANGE_ROW_CAP:
            return _json({
                "error_code": "range_too_large",
                "error": "range_too_large",
                "row_count": len(rows),
                "cap": _RANGE_ROW_CAP,
            }, 413)

        # Collapse to one-row-per-user (latest month) when the caller asks
        # for it. Done after the row-cap guard so the cap still reflects DB
        # work, not post-processing size.
        if dedupe_mode == "user":
            by_uid = {}
            for r in rows:
                uid = r.get("user_id")
                m_new = r.get("month") or ""
                prev = by_uid.get(uid)
                if not prev or (prev.get("month") or "") < m_new:
                    by_uid[uid] = r
            rows = list(by_uid.values())
            rows.sort(key=lambda r: (-(r.get("total_score") or 0), r.get("user_name") or ""))

        if want_detail:
            for row in rows:
                _, _, breakdown = compute_score(row)
                row["breakdown"] = breakdown
        return _json(rows)
    except Exception as e:
        log.error(f"report error: {e}")
        return _json({"error": str(e)}, 500)


# ─── Summary for a month ───────────────────────────────────────────────────────

@kpi_bp.route("/summary", methods=["GET"])
@role_required("manager", "admin", "dataentry")
@rate_limit("kpi_range_query", limit=30, window=60)
@audit_query
def summary():
    """Aggregate scalars across the resolved range. See parse_range docs."""
    try:
        pr = parse_range(request.args)
    except InvalidRangeError as e:
        return _json({"error_code": e.code, "error": e.code}, 400)
    where, where_params = _range_where(pr, alias="e")

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                q = (
                    """
                    SELECT
                        COUNT(*) AS total_entries,
                        AVG(total_score) AS avg_score,
                        MAX(total_score) AS max_score,
                        MIN(total_score) AS min_score,
                        COUNT(CASE WHEN total_score < 55 THEN 1 END) AS below_55,
                        COUNT(CASE WHEN sales_submitted_at IS NOT NULL THEN 1 END) AS sales_done,
                        COUNT(CASE WHEN dataentry_submitted_at IS NOT NULL THEN 1 END) AS dataentry_done
                    FROM kpi_entries e
                    JOIN users u ON u.id = e.user_id
                    WHERE u.active = true
                    """
                    + where
                )
                cur.execute(q, where_params)
                s = dict(cur.fetchone())

                # Top performer across the range
                q2 = (
                    """
                    SELECT u.full_name, e.total_score, e.rating
                    FROM kpi_entries e JOIN users u ON u.id = e.user_id
                    WHERE u.active = true
                    """
                    + where
                    + " ORDER BY e.total_score DESC LIMIT 1"
                )
                cur.execute(q2, where_params)
                top = cur.fetchone()
                s["top"] = dict(top) if top else None
        finally:
            conn.close()
        return _json(s)
    except Exception as e:
        log.error(f"summary error: {e}")
        return _json({"error": str(e)}, 500)


# ─── Sales Manager submits TL manual evaluation ────────────────────────────────

@kpi_bp.route("/submit/tl-evaluation", methods=["POST"])
@role_required("manager", "admin")
def submit_tl_evaluation():
    data = request.get_json() or {}
    tl_user_id = data.get("user_id")
    month = data.get("month")
    if not tl_user_id or not month:
        return _json({"error_code": "required_fields_missing", "error": "required"}, 400)

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    INSERT INTO kpi_entries (user_id, month)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, month) DO NOTHING
                """, (tl_user_id, month))

                cur.execute("""
                    UPDATE kpi_entries SET
                        crm_pct          = %(crm_pct)s,
                        reports          = %(reports)s,
                        clients_pipeline = %(clients_pipeline)s,
                        attitude         = %(attitude)s,
                        presentation     = %(presentation)s,
                        behaviour        = %(behaviour)s,
                        appearance       = %(appearance)s,
                        attendance_pct   = %(attendance_pct)s,
                        hr_roles         = %(hr_roles)s,
                        notes            = %(notes)s,
                        dataentry_by     = %(dataentry_by)s,
                        dataentry_submitted_at = NOW(),
                        updated_at       = NOW()
                    WHERE user_id = %(user_id)s AND month = %(month)s
                    RETURNING id
                """, {
                    "user_id":          tl_user_id,
                    "month":            month,
                    "crm_pct":          float(data.get("crm_pct") or 0),
                    "reports":          int(data.get("reports") or 0),
                    "clients_pipeline": float(data.get("clients_pipeline") or 0),
                    "attitude":         int(data.get("attitude") or 0),
                    "presentation":     int(data.get("presentation") or 0),
                    "behaviour":        int(data.get("behaviour") or 0),
                    "appearance":       int(data.get("appearance") or 0),
                    "attendance_pct":   float(data.get("attendance_pct") or 0),
                    "hr_roles":         int(data.get("hr_roles") or 0),
                    "notes":            data.get("notes") or None,
                    "dataentry_by":     session["user_id"],
                })
            conn.commit()
        finally:
            conn.close()
        log.info(f"✅ TL evaluation submit: tl={tl_user_id} month={month}")
        return _json({"ok": True})
    except Exception as e:
        log.error(f"TL evaluation submit error: {e}")
        return _json({"error": str(e)}, 500)


# ─── Get computed TL KPI ───────────────────────────────────────────────────────

@kpi_bp.route("/tl-kpi/<int:tl_user_id>/<month>", methods=["GET"])
@login_required
def get_tl_kpi(tl_user_id, month):
    role = session.get("role")
    if role == "sales":
        return _json({"error_code": "forbidden", "error": "forbidden"}, 403)
    if role == "team_leader" and session["user_id"] != tl_user_id:
        return _json({"error_code": "forbidden", "error": "forbidden"}, 403)

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # TL's own entry (manual fields)
                cur.execute("""
                    SELECT e.* FROM kpi_entries e
                    WHERE e.user_id = %s AND e.month = %s
                """, (tl_user_id, month))
                tl_row = cur.fetchone()
                tl_entry = dict(tl_row) if tl_row else {}

                # Find TL's team
                cur.execute("""
                    SELECT id FROM teams WHERE leader_id = %s
                """, (tl_user_id,))
                team_row = cur.fetchone()
                if not team_row:
                    return _json({"error_code": "no_team_assigned", "error": "no_team_assigned"}, 404)
                team_id = team_row["id"]

                # Team size
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM users
                    WHERE team_id = %s AND role = 'sales' AND active = true
                """, (team_id,))
                team_size = cur.fetchone()["cnt"]

                # Submitted team entries for this month — joined to user_name so we can
                # surface top/weakest performer in the response.
                cur.execute("""
                    SELECT e.*, u.full_name AS user_name, u.avatar_url FROM kpi_entries e
                    JOIN users u ON u.id = e.user_id
                    WHERE u.team_id = %s AND u.role = 'sales' AND u.active = true
                    AND e.month = %s
                """, (team_id, month))
                team_entries = [dict(r) for r in cur.fetchall()]

                # Per-rep list for TL-05/TL-06: every active sales rep on this team,
                # with their entry data if it exists (LEFT JOIN — reps with no entry
                # this month still appear so the TL sees their full team).
                cur.execute("""
                    SELECT u.id, u.full_name, u.username, u.avatar_url,
                           e.total_score, e.rating,
                           e.sales_submitted_at, e.dataentry_submitted_at
                    FROM users u
                    LEFT JOIN kpi_entries e
                      ON e.user_id = u.id AND e.month = %s
                    WHERE u.team_id = %s AND u.role = 'sales' AND u.active = true
                    ORDER BY e.total_score DESC NULLS LAST, u.full_name ASC
                """, (month, team_id))
                member_rows = [dict(r) for r in cur.fetchall()]

                # Rank context — this TL's standing among all active TLs for the month.
                cur.execute("""
                    SELECT u.id, u.full_name, e.total_score
                    FROM users u
                    LEFT JOIN kpi_entries e ON e.user_id = u.id AND e.month = %s
                    WHERE u.role = 'team_leader' AND u.active = true
                """, (month,))
                tl_rows = [dict(r) for r in cur.fetchall()]

        finally:
            conn.close()

        total, rating, breakdown = compute_tl_score(tl_entry, team_entries)

        # Team aggregates — computed from team_entries we already loaded.
        scored_members = [
            (m.get("user_name") or "", float(m["total_score"]))
            for m in team_entries
            if m.get("total_score") is not None
        ]
        team_avg_score = (
            sum(s for _, s in scored_members) / len(scored_members)
        ) if scored_members else 0.0
        team_above_55 = sum(1 for _, s in scored_members if s >= 55)
        team_below_55 = sum(1 for _, s in scored_members if s < 55)
        team_top = max(scored_members, key=lambda x: x[1]) if scored_members else None
        team_weakest = min(scored_members, key=lambda x: x[1]) if scored_members else None

        # TL rank — sort all TLs by total_score desc; nulls last.
        tl_scored = [(r["id"], float(r["total_score"])) for r in tl_rows if r["total_score"] is not None]
        tl_scored.sort(key=lambda x: x[1], reverse=True)
        tl_rank = next((i + 1 for i, (uid, _) in enumerate(tl_scored) if uid == tl_user_id), None)
        tl_total = len(tl_rows)
        tl_avg = (sum(s for _, s in tl_scored) / len(tl_scored)) if tl_scored else 0.0

        # Per-rep member list — Section 04, TL-05/TL-06.
        # Status semantics:
        #   evaluated   → dataentry_submitted_at is set (final score locked)
        #   submitted   → sales_submitted_at is set, evaluation pending
        #   pending     → no entry yet for this month
        members = []
        for r in member_rows:
            if r.get("dataentry_submitted_at"):
                status = "evaluated"
            elif r.get("sales_submitted_at"):
                status = "submitted"
            else:
                status = "pending"
            members.append({
                "id":        r["id"],
                "full_name": r["full_name"],
                "username":  r["username"],
                "total_score": float(r["total_score"]) if r.get("total_score") is not None else None,
                "rating":      r.get("rating"),
                "status":      status,
            })

        return _json({
            "tl_entry": tl_entry,
            "team_size": team_size,
            "team_submitted": len(team_entries),
            "total_score": total,
            "rating": rating,
            "breakdown": breakdown,
            # Batch 5 — team-context aggregates
            "team_avg_score":  round(team_avg_score, 1),
            "team_above_55":   team_above_55,
            "team_below_55":   team_below_55,
            "team_top":        ({"name": team_top[0], "score": round(team_top[1], 1)} if team_top else None),
            "team_weakest":    ({"name": team_weakest[0], "score": round(team_weakest[1], 1)} if team_weakest else None),
            # Batch 5 — TL rank context
            "tl_rank":  tl_rank,
            "tl_total": tl_total,
            "tl_avg":   round(tl_avg, 1),
            # Section 04 — per-rep visibility for TL-05/TL-06
            "members":  members,
        })
    except Exception as e:
        log.error(f"get_tl_kpi error: {e}")
        return _json({"error": str(e)}, 500)


# ─── List team leaders (for manager evaluation page) ──────────────────────────

@kpi_bp.route("/team-leaders", methods=["GET"])
@role_required("manager", "admin")
@rate_limit("kpi_range_query", limit=30, window=60)
@audit_query
def list_team_leaders():
    """
    One row per active team leader. Accepts month/range params (incl. sub-month).
    For multi-month / sub-month ranges, DISTINCT ON picks the LATEST entry
    per TL within range — so 'evaluated/pending' means "evaluated at least
    once in this range." Sub-month is allowed at the API layer; the frontend
    surfaces a contextual warning banner so users don't misread the result
    as a new daily evaluation.
    """
    try:
        pr = parse_range(request.args)
    except InvalidRangeError as e:
        return _json({"error_code": e.code, "error": e.code}, 400)
    where, where_params = _range_where(pr, alias="e")

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # DISTINCT ON picks the latest entry within range per TL.
                # When pr is exactly one month, this collapses to the original
                # behavior (zero or one row per TL anyway).
                cur.execute(
                    """
                    SELECT u.id, u.full_name, u.username, u.avatar_url,
                           t.id AS team_id, t.name AS team_name,
                           latest.dataentry_submitted_at, latest.notes
                    FROM users u
                    LEFT JOIN teams t ON t.leader_id = u.id
                    LEFT JOIN LATERAL (
                        SELECT DISTINCT ON (e.user_id)
                               e.dataentry_submitted_at, e.notes, e.month
                        FROM kpi_entries e
                        WHERE e.user_id = u.id
                    """ + where + """
                        ORDER BY e.user_id, e.month DESC
                    ) latest ON true
                    WHERE u.role = 'team_leader' AND u.active = true
                    ORDER BY u.full_name
                    """,
                    where_params,
                )
                rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return _json(rows)
    except Exception as e:
        log.error(f"list_team_leaders error: {e}")
        return _json({"error": str(e)}, 500)


# ─── Per-team performance summary (for Teams Performance section) ─────────────

@kpi_bp.route("/teams-summary", methods=["GET"])
@role_required("admin", "manager", "dataentry")
@rate_limit("kpi_range_query", limit=30, window=60)
@audit_query
def teams_summary():
    """
    Per-team aggregates across the resolved range:
      team_id, team_name, leader_id, leader_name, leader_score, leader_rating,
      leader_evaluated, member_count, members_submitted, members_evaluated,
      avg_member_score, top_performer (name + score), members[],
      total_leads/calls/meetings/deals/reservations.

    Range semantics:
      - leader score/rating: latest entry within range (DISTINCT ON)
      - member totals (calls, deals, etc.): summed across all entries in range
      - avg_member_score: average of all per-(member, month) total_scores
      - sub-month: filter rows by submission timestamp (idx_kpi_user_*_submitted);
        members[] reflects whoever was evaluated/submitted in that window.
        Frontend shows a contextual warning so this isn't misread as new data.
    """
    try:
        pr = parse_range(request.args)
    except InvalidRangeError as e:
        return _json({"error_code": e.code, "error": e.code}, 400)
    where, where_params = _range_where(pr, alias="e")

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # All teams + their leader basic info (range-independent)
                cur.execute("""
                    SELECT t.id   AS team_id,
                           t.name AS team_name,
                           t.leader_id,
                           u.full_name AS leader_name,
                           u.username  AS leader_username
                    FROM teams t
                    LEFT JOIN users u ON u.id = t.leader_id
                    ORDER BY t.name
                """)
                teams = [dict(r) for r in cur.fetchall()]

                # Leader KPI rows within range (latest per leader)
                leader_ids = [t["leader_id"] for t in teams if t["leader_id"]]
                leader_kpi = {}
                if leader_ids:
                    q_leader = (
                        """
                        SELECT DISTINCT ON (e.user_id)
                               e.user_id, e.total_score, e.rating,
                               e.dataentry_submitted_at, e.month
                        FROM kpi_entries e
                        WHERE e.user_id = ANY(%s)
                        """
                        + where
                        + " ORDER BY e.user_id, e.month DESC"
                    )
                    cur.execute(q_leader, [leader_ids] + where_params)
                    for r in cur.fetchall():
                        leader_kpi[int(r["user_id"])] = dict(r)

                # All sales entries within range, joined to their team. For a
                # multi-month range each (user, month) yields one row, which
                # the Python aggregation below sums into team totals.
                # Range filter placed in the LEFT JOIN ON clause (not WHERE)
                # so users with no entry in range still appear with NULLs.
                q_sales = (
                    """
                    SELECT u.id AS user_id, u.full_name, u.team_id, u.avatar_url,
                           e.fresh_leads, e.calls, e.meetings, e.deals,
                           e.reservations, e.crm_pct, e.followup_pct,
                           e.total_score, e.rating, e.month,
                           e.sales_submitted_at, e.dataentry_submitted_at,
                           e.attitude, e.presentation, e.behaviour, e.appearance,
                           e.attendance_pct, e.hr_roles, e.reports
                    FROM users u
                    LEFT JOIN kpi_entries e
                      ON e.user_id = u.id
                    """
                    + where
                    + " WHERE u.role = 'sales' AND u.active = true"
                )
                cur.execute(q_sales, where_params)
                sales_rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        # Group sales by team_id; collect unassigned (team_id IS NULL) separately
        # so the manager view can surface them as a flagged "Unassigned" card
        # rather than silently dropping them from the totals (TL-07).
        by_team = {}
        unassigned_rows = []
        for r in sales_rows:
            tid = r.get("team_id")
            if tid is None:
                unassigned_rows.append(r)
                continue
            by_team.setdefault(int(tid), []).append(r)

        # Each (user, month) lands as its own row from the LEFT JOIN; over a
        # multi-month range that means the same user appears multiple times.
        # Per-rep displays (member list, member_count, submitted/evaluated
        # counters, top performer) need per-user dedupe — pick the latest
        # month so the status reflects what the user did most recently.
        # Range sums (deals/calls/meetings totals) intentionally stay on the
        # raw rows so the totals add up across the whole period.
        def _dedupe_latest(rows):
            by_uid = {}
            for r in rows:
                uid = r["user_id"]
                m_new = r.get("month") or ""
                prev = by_uid.get(uid)
                if not prev or (prev.get("month") or "") < m_new:
                    by_uid[uid] = r
            return list(by_uid.values())

        out = []
        for t in teams:
            tid = int(t["team_id"])
            members_all = by_team.get(tid, [])         # raw rows for sums
            members_uniq = _dedupe_latest(members_all)  # one row per user, latest

            mcount = len(members_uniq)
            submitted = sum(1 for m in members_uniq if m.get("sales_submitted_at"))
            evaluated = sum(1 for m in members_uniq if m.get("dataentry_submitted_at"))

            scored = [
                (m["full_name"], float(m["total_score"]))
                for m in members_uniq
                if m.get("total_score") is not None
            ]
            avg_member = (sum(s for _, s in scored) / len(scored)) if scored else 0.0
            top = max(scored, key=lambda x: x[1]) if scored else None

            def _sum(field, _rows=members_all):
                return sum(float(m.get(field) or 0) for m in _rows)

            ld = leader_kpi.get(int(t["leader_id"])) if t["leader_id"] else None

            # Per-rep list — same shape as tl-kpi's members so the manager
            # cards and the TL's own page can share rendering.
            member_list = []
            for m in members_uniq:
                if m.get("dataentry_submitted_at"):
                    mstatus = "evaluated"
                elif m.get("sales_submitted_at"):
                    mstatus = "submitted"
                else:
                    mstatus = "pending"
                member_list.append({
                    "id":          m["user_id"],
                    "full_name":   m["full_name"],
                    "avatar_url":  m.get("avatar_url"),
                    "total_score": float(m["total_score"]) if m.get("total_score") is not None else None,
                    "rating":      m.get("rating"),
                    "status":      mstatus,
                })
            # Sort: highest score first, NULLs last; alpha tiebreak.
            member_list.sort(key=lambda x: (
                -(x["total_score"] if x["total_score"] is not None else -1),
                x["full_name"] or "",
            ))

            out.append({
                "team_id":           tid,
                "team_name":         t["team_name"],
                "leader_id":         t["leader_id"],
                "leader_name":       t["leader_name"],
                "leader_score":      float(ld["total_score"]) if ld and ld.get("total_score") is not None else None,
                "leader_rating":     ld["rating"] if ld else None,
                "leader_evaluated":  bool(ld and ld.get("dataentry_submitted_at")),
                "member_count":      mcount,
                "members_submitted": submitted,
                "members_evaluated": evaluated,
                "avg_member_score":  round(avg_member, 1),
                "top_performer":     {"name": top[0], "score": round(top[1], 1)} if top else None,
                "total_leads":        int(_sum("fresh_leads")),
                "total_calls":        int(_sum("calls")),
                "total_meetings":     int(_sum("meetings")),
                "total_deals":        int(_sum("deals")),
                "total_reservations": int(_sum("reservations")),
                "members":            member_list,
            })

        # Rank: highest avg_member_score first, then by member_count
        out.sort(key=lambda x: (-x["avg_member_score"], -x["member_count"]))
        for i, row in enumerate(out, 1):
            row["rank"] = i

        # Unassigned bucket — sales reps with no team. Same dedupe rule as
        # the team buckets above so a rep doesn't appear once per month
        # they have an entry in the range.
        unassigned_uniq = _dedupe_latest(unassigned_rows)
        unassigned_members = []
        for r in unassigned_uniq:
            if r.get("dataentry_submitted_at"):
                status = "evaluated"
            elif r.get("sales_submitted_at"):
                status = "submitted"
            else:
                status = "pending"
            unassigned_members.append({
                "id":          r["user_id"],
                "full_name":   r["full_name"],
                "avatar_url":  r.get("avatar_url"),
                "total_score": float(r["total_score"]) if r.get("total_score") is not None else None,
                "rating":      r.get("rating"),
                "status":      status,
            })
        u_submitted = sum(1 for m in unassigned_uniq if m.get("sales_submitted_at"))
        u_evaluated = sum(1 for m in unassigned_uniq if m.get("dataentry_submitted_at"))

        return _json({
            "teams": out,
            "unassigned": {
                "member_count":      len(unassigned_members),
                "members_submitted": u_submitted,
                "members_evaluated": u_evaluated,
                "members":           unassigned_members,
            },
        })
    except Exception as e:
        log.error(f"teams_summary error: {e}")
        return _json({"error_code": "server", "error": str(e)}, 500)
