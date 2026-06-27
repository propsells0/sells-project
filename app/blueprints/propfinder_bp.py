"""
PropFinder blueprint — real estate units API
Gracefully handles missing `units` table (returns empty data instead of crashing).
"""
import json
import logging
import threading
import psycopg2
import psycopg2.extras
from decimal import Decimal
from flask import Blueprint, jsonify, request, Response, session
from app.database import get_conn, table_exists
from app.auth import error_response, login_required, role_required
from config import Config

UNITS_LIMIT = 1000  # Hard cap on /api/units result set; frontend shows a notice when truncated.

log = logging.getLogger(__name__)
propfinder_bp = Blueprint("propfinder", __name__, url_prefix="/api")


def _json_serial(obj):
    if isinstance(obj, Decimal):
        val = float(obj)
        if val != val:
            return None
        return val
    raise TypeError(f"Type {type(obj)} not serializable")


def _json_response(data, status=200):
    return Response(
        json.dumps(data, default=_json_serial, allow_nan=False),
        status=status,
        mimetype="application/json"
    )


def _get_sync_status():
    """Lazy import: sync_status dict exists only if sync_service has been imported"""
    try:
        from app.sync_service import sync_status
        return sync_status
    except Exception:
        return {
            "running": False, "last_run": None,
            "last_result": "Sync disabled", "error": None
        }


@propfinder_bp.route("/health")
def health():
    return _json_response({
        "status": "ok",
        "sync_enabled": not Config.DISABLE_SYNC,
        "sync": _get_sync_status(),
    })


def _strip_arg(name):
    """Read a query arg, trimmed; return None for empty/whitespace."""
    v = request.args.get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


@propfinder_bp.route("/units")
@login_required
def get_units():
    # Sales role is restricted to AVAILABLE (unsold) units only.
    available_only = session.get("role") == "sales"

    # Server-side filters (Round 3, Fix #5). Search/price/area/sort still run
    # client-side since they're either fuzzy or low-cardinality.
    f_city     = _strip_arg("city")
    f_dev      = _strip_arg("dev")
    f_compound = _strip_arg("compound")
    f_type     = _strip_arg("type")
    f_bedrooms = _strip_arg("bedrooms")

    try:
        conn = get_conn()
        try:
            if not table_exists(conn, "units"):
                return _json_response({"units": [], "total": 0, "truncated": False})

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                where = []
                params = []
                if available_only:
                    where.append("COALESCE(is_sold, false) = false")
                if f_city:
                    where.append("city_name = %s"); params.append(f_city)
                if f_dev:
                    where.append("developer_name = %s"); params.append(f_dev)
                if f_compound:
                    where.append("compound_name = %s"); params.append(f_compound)
                if f_type:
                    where.append("unit_type = %s"); params.append(f_type)
                if f_bedrooms:
                    # bedrooms in DB may be int or text; compare via text cast.
                    where.append("CAST(bedrooms AS TEXT) = %s"); params.append(f_bedrooms)

                where_sql = (" WHERE " + " AND ".join(where)) if where else ""

                cur.execute("SELECT COUNT(*) AS c FROM units" + where_sql, params)
                total = int(cur.fetchone()["c"])

                query = """
                    SELECT
                        city_name, compound_name, compound_id,
                        developer_name, developer_id,
                        phase_name, phase_id, unit_type,
                        bedrooms,
                        NULLIF(CAST(built_up_area_sqm AS FLOAT), 'NaN') AS built_up_area_sqm,
                        NULLIF(CAST(total_price_egp AS FLOAT), 'NaN') AS total_price_egp,
                        NULLIF(CAST(price_per_sqm_egp AS FLOAT), 'NaN') AS price_per_sqm_egp,
                        NULLIF(CAST(cash_price_from_egp AS FLOAT), 'NaN') AS cash_price_from_egp,
                        NULLIF(CAST(cash_price_to_egp AS FLOAT), 'NaN') AS cash_price_to_egp,
                        delivery_from_months, delivery_to_months,
                        payment_plan, payment_plans, maintenance, club_fees,
                        parking_fees, finishing_type,
                        NULLIF(CAST(cash_discount_percent AS FLOAT), 'NaN') AS cash_discount_percent,
                        city_id, detail_id, outdoor_area, status, sub_type,
                        NULLIF(CAST(total_price_to_egp AS FLOAT), 'NaN') AS total_price_to_egp,
                        type_id,
                        COALESCE(is_sold, false) AS is_sold
                    FROM units
                """ + where_sql + " ORDER BY detail_id ASC LIMIT %s"
                cur.execute(query, params + [UNITS_LIMIT])
                rows = cur.fetchall()
        finally:
            conn.close()

        cleaned = []
        for row in rows:
            d = dict(row)
            for k, v in d.items():
                if isinstance(v, float) and v != v:
                    d[k] = None
            cleaned.append(d)
        return _json_response({
            "units": cleaned,
            "total": total,
            "truncated": total > UNITS_LIMIT,
            "limit": UNITS_LIMIT,
        })
    except Exception as e:
        log.error(f"Error fetching units: {e}")
        return _json_response({"error_code": "server", "error": "server"}, 500)


@propfinder_bp.route("/units/facets")
@login_required
def get_units_facets():
    """DISTINCT values for filter dropdowns. Sales role sees only AVAILABLE units."""
    available_only = session.get("role") == "sales"
    try:
        conn = get_conn()
        try:
            if not table_exists(conn, "units"):
                return _json_response({
                    "cities": [], "developers": [], "compounds": [],
                    "phases": [], "types": [], "bedrooms": [], "finishings": [],
                })

            scope = " WHERE COALESCE(is_sold, false) = false " if available_only else ""

            def _distinct(col, cast_text=False):
                expr = f"CAST({col} AS TEXT)" if cast_text else col
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT DISTINCT {expr} FROM units{scope} "
                        f"AND {col} IS NOT NULL AND {expr} <> '' AND {expr} <> '0'"
                        if scope else
                        f"SELECT DISTINCT {expr} FROM units "
                        f"WHERE {col} IS NOT NULL AND {expr} <> '' AND {expr} <> '0'"
                    )
                    vals = [r[0] for r in cur.fetchall() if r[0] is not None]
                # Mixed int/string — sort numerically when possible.
                def _key(v):
                    try: return (0, float(v))
                    except (TypeError, ValueError): return (1, str(v))
                return sorted(vals, key=_key)

            facets = {
                "cities":     _distinct("city_name"),
                "developers": _distinct("developer_name"),
                "compounds":  _distinct("compound_name"),
                "phases":     _distinct("phase_name"),
                "types":      _distinct("unit_type"),
                "bedrooms":   _distinct("bedrooms", cast_text=True),
                "finishings": _distinct("finishing_type", cast_text=True),
            }
        finally:
            conn.close()
        return _json_response(facets)
    except Exception as e:
        log.error(f"Error fetching unit facets: {e}")
        return _json_response({"error_code": "server", "error": "server"}, 500)


@propfinder_bp.route("/stats")
@login_required
def get_stats():
    try:
        conn = get_conn()
        try:
            if not table_exists(conn, "units"):
                return _json_response({
                    "total": 0, "sold": 0, "compounds": 0,
                    "avg_price": None, "min_price": None, "max_price": None
                })

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) AS total,
                        COUNT(CASE WHEN is_sold = true OR status = 0 THEN 1 END) AS sold,
                        AVG(CAST(total_price_egp AS FLOAT)) AS avg_price,
                        MIN(CAST(total_price_egp AS FLOAT)) AS min_price,
                        MAX(CAST(total_price_egp AS FLOAT)) AS max_price,
                        COUNT(DISTINCT compound_name) AS compounds
                    FROM units
                """)
                stats = dict(cur.fetchone())
        finally:
            conn.close()
        return _json_response(stats)
    except Exception as e:
        log.error(f"Stats error: {e}")
        return _json_response({"error": str(e)}, 500)


@propfinder_bp.route("/sync/status")
@login_required
def sync_status_route():
    return _json_response({
        "enabled": not Config.DISABLE_SYNC,
        **_get_sync_status()
    })


@propfinder_bp.route("/sync/trigger", methods=["POST"])
@role_required("admin", "manager", "marketing")
def trigger_sync():
    if Config.DISABLE_SYNC:
        return _json_response({"error_code": "forbidden", "error": "sync_disabled"}, 400)
    try:
        from app.sync_service import run_sync, sync_status
    except Exception as e:
        return _json_response({"error_code": "server", "error": "sync_unavailable"}, 500)
    if sync_status["running"]:
        return _json_response({"ok": False, "running": True}, 409)
    t = threading.Thread(target=run_sync, daemon=True)
    t.start()
    return _json_response({"ok": True})


@propfinder_bp.route("/reset-sold", methods=["POST"])
@role_required("admin")
def reset_sold():
    try:
        conn = get_conn()
        try:
            if not table_exists(conn, "units"):
                return _json_response({"error_code": "not_found", "error": "not_found"}, 404)
            with conn.cursor() as cur:
                cur.execute("UPDATE units SET is_sold = FALSE, sold_at = NULL")
                affected = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return _json_response({"ok": True, "affected": affected})
    except Exception as e:
        return _json_response({"error_code": "server", "error": "server"}, 500)
