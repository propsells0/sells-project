"""
Finance blueprint — converts KPI data into financial projections
(revenue from deals, commissions, etc).
NO actual income/expense management — purely derived from KPI numbers.
"""
import json
import logging
import psycopg2.extras
from decimal import Decimal
from datetime import datetime, date
from flask import Blueprint, request, session, Response
from datetime import timedelta
from app.database import get_conn
from app.auth import role_required, rate_limit
from app.kpi_logic import compute_financials, FINANCIAL_DEFAULTS
from app.util.audit import audit_query
from app.util.date_range import parse_range, InvalidRangeError

_ONE_DAY = timedelta(days=1)

log = logging.getLogger(__name__)
finance_bp = Blueprint("finance", __name__, url_prefix="/api/finance")


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


@finance_bp.route("/report", methods=["GET"])
@role_required("admin", "manager")
@rate_limit("kpi_range_query", limit=30, window=60)
@audit_query
def report():
    """
    Finance projection for all sales reps within the resolved range, plus
    aggregated totals. Filtering follows the standard date-range contract
    (see app.util.date_range.parse_range): from/to/preset/legacy month.

    Sub-month ranges allowed — they filter rows by dataentry_submitted_at,
    but each row's revenue still covers a full month. The frontend surfaces
    a contextual warning banner so users don't misread submission-date
    filtering as daily revenue.

    Other params:
      - avg_deal_value_egp, commission_rate, avg_reservation_value_egp,
        reservation_commission_rate  (override defaults)
    """
    try:
        pr = parse_range(request.args)
    except InvalidRangeError as e:
        return _json({"error_code": e.code, "error": e.code}, 400)

    # Allow overriding financial settings via query params
    settings = dict(FINANCIAL_DEFAULTS)
    for key in FINANCIAL_DEFAULTS:
        if request.args.get(key):
            try:
                settings[key] = float(request.args.get(key))
            except ValueError:
                pass

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                q = """
                    SELECT e.*, u.full_name AS user_name, u.username
                    FROM kpi_entries e
                    JOIN users u ON u.id = e.user_id
                    WHERE u.active = true
                """
                params = []
                if pr.month_str:
                    q += " AND e.month = %s"
                    params.append(pr.month_str)
                elif pr.is_sub_month:
                    # Sub-month: filter by submission timestamp on the chosen
                    # column (idx_kpi_user_dataentry_submitted picks this up).
                    q += " AND e.dataentry_submitted_at >= %s AND e.dataentry_submitted_at < %s"
                    params.append(pr.from_date)
                    params.append(pr.to_date + _ONE_DAY)
                else:
                    # Multi-month aligned range.
                    q += " AND e.month BETWEEN %s AND %s"
                    params.append(f"{pr.from_date.year:04d}-{pr.from_date.month:02d}")
                    params.append(f"{pr.to_date.year:04d}-{pr.to_date.month:02d}")
                q += " ORDER BY u.full_name"
                cur.execute(q, params)
                rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        breakdown = []
        total_revenue = 0.0
        total_commission = 0.0
        total_deals = 0
        total_reservations = 0

        for row in rows:
            fin = compute_financials(row, settings)
            breakdown.append({
                "user_id": row["user_id"],
                "user_name": row["user_name"],
                "month": row["month"],
                "total_score": float(row.get("total_score") or 0),
                "rating": row.get("rating"),
                **fin,
            })
            total_revenue += fin["total_revenue"]
            total_commission += fin["total_commission"]
            total_deals += fin["deals_count"]
            total_reservations += fin["reservations_count"]

        return _json({
            # `month` kept for backwards compat: set when range is exactly one
            # calendar month (matches the legacy ?month= response shape).
            "month": pr.month_str,
            "range": pr.to_dict(),
            "settings": settings,
            "totals": {
                "total_revenue": round(total_revenue, 2),
                "total_commission": round(total_commission, 2),
                "total_deals": total_deals,
                "total_reservations": total_reservations,
                "sales_count": len(breakdown),
                "avg_commission_per_sales": round(total_commission / len(breakdown), 2) if breakdown else 0,
            },
            "breakdown": breakdown,
        })
    except Exception as e:
        log.error(f"Finance report error: {e}")
        return _json({"error": str(e)}, 500)


@finance_bp.route("/trend", methods=["GET"])
@role_required("admin", "manager")
def trend():
    """Monthly revenue and commission trend over time."""
    months_back = int(request.args.get("months_back", 6))
    settings = FINANCIAL_DEFAULTS

    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT month,
                           SUM(COALESCE(deals, 0)) AS total_deals,
                           SUM(COALESCE(reservations, 0)) AS total_reservations
                    FROM kpi_entries e
                    JOIN users u ON u.id = e.user_id
                    WHERE u.active = true
                    GROUP BY month
                    ORDER BY month DESC
                    LIMIT %s
                """, (months_back,))
                rows = list(reversed([dict(r) for r in cur.fetchall()]))
        finally:
            conn.close()

        result = []
        for row in rows:
            deals = float(row["total_deals"] or 0)
            reservations = float(row["total_reservations"] or 0)
            deal_rev = deals * settings["avg_deal_value_egp"]
            res_rev = reservations * settings["avg_reservation_value_egp"]
            deal_comm = deal_rev * settings["commission_rate"]
            res_comm = res_rev * settings["reservation_commission_rate"]
            result.append({
                "month": row["month"],
                "deals": int(deals),
                "reservations": int(reservations),
                "revenue": round(deal_rev + res_rev, 2),
                "commission": round(deal_comm + res_comm, 2),
            })

        return _json(result)
    except Exception as e:
        log.error(f"Finance trend error: {e}")
        return _json({"error": str(e)}, 500)


@finance_bp.route("/settings", methods=["GET"])
@role_required("admin", "manager")
def get_settings():
    return _json(FINANCIAL_DEFAULTS)
