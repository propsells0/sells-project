"""
CRM Report ingestion endpoints.

P1a — upload + status polling. P1b — campaign overview + per-campaign
intervention listing. P2 — sales KPI rollup. P3 (this file's latest
additions) — leads listing, lead timeline, cross-campaign intervention
inbox, status PATCH, and the open-count badge feed.
"""
import base64
import json
import logging

import psycopg2.extras
from flask import Blueprint, jsonify, request, session

from app.auth import (
    csrf_protect,
    error_response,
    login_required,
    role_required,
)
from datetime import date, datetime, timedelta

from app.crm_logic import (
    DEFAULT_STAGE_MAP,
    apply_sales_rep_mapping_change,
    apply_stage_mapping_change,
    build_lead_timeline,
    normalize_sales_name,
    recalc_after_upload,
    _response_rate_pct,
)
from app.crm_processor import start_processing_thread
from app.database import get_conn

log = logging.getLogger(__name__)
crm_bp = Blueprint("crm", __name__, url_prefix="/api/crm")


# Upload cap. 10 MB is roughly ~40k rows of typical CRM exports; anything
# larger is almost certainly a multi-month export that should be split
# upstream. The cap also doubles as a cheap DoS guard since the file goes
# straight into a worker thread's memory.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _campaign_exists(conn, campaign_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM marketing_campaigns WHERE id = %s",
            (campaign_id,),
        )
        return cur.fetchone() is not None


# ─── POST upload ────────────────────────────────────────────────────────

@crm_bp.route("/campaigns/<int:campaign_id>/upload", methods=["POST"])
@login_required
@role_required("admin", "manager", "marketing")
@csrf_protect
def upload_crm_report(campaign_id: int):
    # Validate the multipart payload before we even consider opening the file.
    if "file" not in request.files:
        return error_response("required_fields_missing", 400)

    f = request.files["file"]
    if not f or not f.filename:
        return error_response("required_fields_missing", 400)

    # openpyxl reads .xlsx; we don't support .xls (old binary format) or
    # .csv on this endpoint to keep the parser focused.
    if not f.filename.lower().endswith(".xlsx"):
        return error_response("invalid_input", 400)

    # Slurp the bytes once. werkzeug streams from a SpooledTemporaryFile;
    # by reading it into memory we (a) decouple parsing from the HTTP
    # request lifecycle (the thread keeps working after we return) and
    # (b) get an unambiguous size check.
    blob = f.read()
    if not blob:
        return error_response("invalid_input", 400)
    if len(blob) > _MAX_UPLOAD_BYTES:
        # 413 Payload Too Large is the strict-correct status; the frontend
        # already handles range_too_large the same way, so reuse the
        # surface here. We pick a specific code so the toast localizes.
        return error_response("range_too_large", 413)

    conn = None
    try:
        conn = get_conn()
        if not _campaign_exists(conn, campaign_id):
            return error_response("not_found", 404)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crm_report_uploads (
                    campaign_id, file_name, uploaded_by, status
                )
                VALUES (%s, %s, %s, 'PENDING')
                RETURNING id
                """,
                (campaign_id, f.filename[:255], session.get("user_id")),
            )
            upload_id = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        log.error("CRM upload insert failed: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()

    # Hand off to the daemon thread. start_processing_thread is fire-and-
    # forget; status/errors land on the upload row via the thread itself.
    start_processing_thread(upload_id, blob, campaign_id)

    return jsonify({
        "ok": True,
        "upload_id": upload_id,
        "status": "PROCESSING",
        "message": "Upload received, processing in background",
    }), 202


# ─── GET status ─────────────────────────────────────────────────────────

@crm_bp.route("/uploads/<int:upload_id>/status", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def upload_status(upload_id: int):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, campaign_id, status,
                       total_rows, total_leads, total_events,
                       new_events, duplicate_events,
                       unmatched_sales_reps, unmatched_stages, warnings,
                       error_message, processed_at
                FROM crm_report_uploads
                WHERE id = %s
                """,
                (upload_id,),
            )
            row = cur.fetchone()
        if not row:
            return error_response("not_found", 404)
        return jsonify({
            "upload_id": row["id"],
            "campaign_id": row["campaign_id"],
            "status": row["status"],
            "total_rows": row["total_rows"],
            "total_leads": row["total_leads"],
            "total_events": row["total_events"],
            "new_events": row["new_events"],
            "duplicate_events": row["duplicate_events"],
            "unmatched_sales_reps": row["unmatched_sales_reps"] or [],
            "unmatched_stages": row["unmatched_stages"] or [],
            "warnings": row["warnings"] or [],
            "error_message": row["error_message"],
            "processed_at": row["processed_at"].isoformat() if row["processed_at"] else None,
        })
    except Exception as e:
        log.error("upload_status %s: %s", upload_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET campaign overview ──────────────────────────────────────────────

@crm_bp.route("/campaigns/<int:campaign_id>/overview", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def campaign_overview(campaign_id: int):
    """Snapshot view rendered on the per-campaign page.

    Reads from campaign_kpis (rolled up after each upload) plus a small
    intervention breakdown for the HIGH/MEDIUM badge counts. The "last
    upload summary" is pulled from crm_report_uploads — useful for the
    "Last uploaded by X · N new events" line on the page header.

    Stage labels are NOT localized here — the frontend resolves
    `crm.stages.<TOKEN>` keys so the same payload renders in either
    language without a server round-trip.
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, campaign_name FROM marketing_campaigns WHERE id = %s",
                (campaign_id,),
            )
            camp = cur.fetchone()
            if not camp:
                return error_response("not_found", 404)

            # Read the KPI rollup if it exists; new campaigns with no
            # uploads yet just get zeros.
            cur.execute(
                """
                SELECT total_leads, stage_counts,
                       manager_intervention_count, last_upload_at, updated_at
                FROM campaign_kpis WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            kpi_row = cur.fetchone()

            cur.execute(
                """
                SELECT priority, COUNT(*) AS n
                FROM manager_intervention_flags
                WHERE campaign_id = %s AND status = 'OPEN'
                GROUP BY priority
                """,
                (campaign_id,),
            )
            breakdown = {"HIGH": 0, "MEDIUM": 0}
            for r in cur.fetchall():
                if r["priority"] in breakdown:
                    breakdown[r["priority"]] = r["n"]

            # Most recent COMPLETED upload; surfaces "by whom, new vs dup".
            cur.execute(
                """
                SELECT u.id, u.processed_at, u.total_events, u.new_events,
                       u.duplicate_events, u.uploaded_by,
                       usr.full_name AS uploaded_by_name
                FROM crm_report_uploads u
                LEFT JOIN users usr ON usr.id = u.uploaded_by
                WHERE u.campaign_id = %s
                  AND u.status = 'COMPLETED'
                  AND u.is_voided = FALSE
                ORDER BY u.processed_at DESC NULLS LAST
                LIMIT 1
                """,
                (campaign_id,),
            )
            last_upload = cur.fetchone()

        stage_counts = (kpi_row["stage_counts"] if kpi_row else None) or {}
        return jsonify({
            "campaign_id": camp["id"],
            "campaign_name": camp["campaign_name"],
            "total_leads": (kpi_row["total_leads"] if kpi_row else 0),
            "stage_counts": stage_counts,
            "manager_intervention_count": (
                kpi_row["manager_intervention_count"] if kpi_row else 0
            ),
            "intervention_breakdown": breakdown,
            "last_upload_at": (
                kpi_row["last_upload_at"].isoformat()
                if kpi_row and kpi_row["last_upload_at"] else None
            ),
            "updated_at": (
                kpi_row["updated_at"].isoformat()
                if kpi_row and kpi_row["updated_at"] else None
            ),
            "last_upload_summary": ({
                "upload_id": last_upload["id"],
                "uploaded_at": (
                    last_upload["processed_at"].isoformat()
                    if last_upload["processed_at"] else None
                ),
                "uploaded_by": last_upload["uploaded_by"],
                "uploaded_by_name": last_upload["uploaded_by_name"],
                "total_events": last_upload["total_events"],
                "new_events": last_upload["new_events"],
                "duplicate_events": last_upload["duplicate_events"],
            } if last_upload else None),
        })
    except Exception as e:
        log.error("campaign_overview %s: %s", campaign_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET intervention list for a campaign ───────────────────────────────

_VALID_INTERVENTION_STATUSES = {"OPEN", "REVIEWED", "CLOSED"}
_VALID_INTERVENTION_PRIORITIES = {"HIGH", "MEDIUM"}


@crm_bp.route("/campaigns/<int:campaign_id>/intervention", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def campaign_intervention(campaign_id: int):
    """Manager-intervention inbox for a single campaign.

    Filters:
      status   default OPEN — accept OPEN | REVIEWED | CLOSED | all
      priority default all  — accept HIGH | MEDIUM | all

    Order: priority (HIGH → MEDIUM), then last_no_answer_date DESC so the
    most recently-broken-down conversations bubble up. Caps at 200 rows —
    pagination ships in P3 once we have a real inbox page.
    """
    status_arg = (request.args.get("status") or "OPEN").upper()
    priority_arg = (request.args.get("priority") or "all").upper()

    if status_arg != "ALL" and status_arg not in _VALID_INTERVENTION_STATUSES:
        return error_response("invalid_input", 400)
    if priority_arg != "ALL" and priority_arg not in _VALID_INTERVENTION_PRIORITIES:
        return error_response("invalid_input", 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM marketing_campaigns WHERE id = %s",
                (campaign_id,),
            )
            if not cur.fetchone():
                return error_response("not_found", 404)

        clauses = ["m.campaign_id = %s"]
        params = [campaign_id]
        if status_arg != "ALL":
            clauses.append("m.status = %s")
            params.append(status_arg)
        if priority_arg != "ALL":
            clauses.append("m.priority = %s")
            params.append(priority_arg)
        where_sql = " AND ".join(clauses)

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT m.id, m.lead_id, m.campaign_id, m.sales_user_id,
                       m.trigger_type, m.current_stage, m.previous_positive_stage,
                       m.priority, m.last_positive_stage_date, m.last_no_answer_date,
                       m.last_comment, m.status, m.created_at, m.updated_at,
                       l.client_name, l.mobile,
                       usr.full_name AS current_sales_rep_name
                FROM manager_intervention_flags m
                JOIN leads l ON l.id = m.lead_id
                LEFT JOIN users usr ON usr.id = m.sales_user_id
                WHERE {where_sql}
                ORDER BY
                  CASE m.priority WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                  m.last_no_answer_date DESC NULLS LAST,
                  m.id DESC
                LIMIT 200
                """,
                params,
            )
            rows = cur.fetchall()

        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "lead_id": r["lead_id"],
                "campaign_id": r["campaign_id"],
                "client_name": r["client_name"],
                "mobile": r["mobile"],
                "current_sales_rep_id": r["sales_user_id"],
                "current_sales_rep_name": r["current_sales_rep_name"],
                "trigger_type": r["trigger_type"],
                "current_stage": r["current_stage"],
                "previous_positive_stage": r["previous_positive_stage"],
                "priority": r["priority"],
                "last_positive_stage_date": (
                    r["last_positive_stage_date"].isoformat()
                    if r["last_positive_stage_date"] else None
                ),
                "last_no_answer_date": (
                    r["last_no_answer_date"].isoformat()
                    if r["last_no_answer_date"] else None
                ),
                "last_comment": r["last_comment"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            })
        return jsonify(out)
    except Exception as e:
        log.error("campaign_intervention %s: %s", campaign_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET per-rep Fresh vs Rotation rollup ───────────────────────────────

@crm_bp.route("/campaigns/<int:campaign_id>/sales-kpis", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def campaign_sales_kpis(campaign_id: int):
    """Per-sales-rep view of Fresh + Rotation lead counts and stage
    outcomes for the campaign. Pre-rolled-up in sales_kpis after every
    upload; reads pull plain rows.

    Unmatched reps (sales_user_id IS NULL) DO NOT appear in the rep list —
    they show up in `unmatched_reps_summary` so the admin can pick them
    out and map them in the CRM settings page (P4).
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM marketing_campaigns WHERE id = %s",
                (campaign_id,),
            )
            if not cur.fetchone():
                return error_response("not_found", 404)

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT s.sales_user_id, s.fresh_leads_count, s.rotation_leads_count,
                       s.fresh_outcomes, s.rotation_outcomes, s.updated_at,
                       u.full_name AS sales_name, u.avatar_url
                FROM sales_kpis s
                JOIN users u ON u.id = s.sales_user_id
                WHERE s.campaign_id = %s
                ORDER BY s.fresh_leads_count DESC,
                         s.rotation_leads_count DESC,
                         u.full_name ASC
                """,
                (campaign_id,),
            )
            reps = cur.fetchall()

            # Unmatched reps with at least one event. Aggregated by the
            # raw (pre-normalization) name so the admin sees what was
            # written in the sheet, not a sanitized form.
            cur.execute(
                """
                SELECT raw_sales_rep_name AS raw, COUNT(*) AS n
                FROM lead_events
                WHERE campaign_id = %s
                  AND sales_user_id IS NULL
                  AND raw_sales_rep_name IS NOT NULL
                  AND raw_sales_rep_name <> ''
                  AND is_voided = FALSE
                GROUP BY raw_sales_rep_name
                ORDER BY n DESC, raw_sales_rep_name ASC
                """,
                (campaign_id,),
            )
            unmatched_rows = cur.fetchall()

        out_reps = []
        for r in reps:
            out_reps.append({
                "sales_user_id": r["sales_user_id"],
                "sales_name": r["sales_name"],
                "avatar_url": r["avatar_url"],
                "fresh_leads_count": r["fresh_leads_count"],
                "rotation_leads_count": r["rotation_leads_count"],
                "fresh_outcomes": r["fresh_outcomes"] or {},
                "rotation_outcomes": r["rotation_outcomes"] or {},
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            })

        return jsonify({
            "campaign_id": campaign_id,
            "reps": out_reps,
            "unmatched_reps_summary": {r["raw"]: r["n"] for r in unmatched_rows},
        })
    except Exception as e:
        log.error("campaign_sales_kpis %s: %s", campaign_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET leads listing for a campaign (P3) ──────────────────────────────

_LEADS_MAX_LIMIT = 200
_LEADS_DEFAULT_LIMIT = 50


def _decode_leads_cursor(raw):
    """Cursor is base64(json({ts:iso, id:int})). Returns (datetime|None, id|None)
    or (None, None) if missing/invalid. Bad cursor degrades to "no cursor" —
    surfacing a 400 here makes deep-link sharing brittle without buying us
    much."""
    if not raw:
        return None, None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii") + b"=="))
        from datetime import datetime as _dt
        ts = _dt.fromisoformat(payload["ts"]) if payload.get("ts") else None
        return ts, int(payload["id"])
    except Exception:
        return None, None


def _encode_leads_cursor(ts, lid):
    payload = {"ts": ts.isoformat() if ts else None, "id": lid}
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")


@crm_bp.route("/campaigns/<int:campaign_id>/leads", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def campaign_leads(campaign_id: int):
    """Paginated leads listing for the campaign tab.

    Each row is one lead with its derived state (latest stage, current
    rep, event count, last event date, intervention flag). The latest
    event is found via a LATERAL subquery — cheaper than a window over
    the full lead_events table because the (lead_id, follow_date) index
    drives the lookup to a single row per lead.

    Cursor pagination keyed on (last_event_at DESC NULLS LAST, lead_id DESC)
    so the order is stable when two leads share a timestamp.
    """
    stage_filter = (request.args.get("stage") or "").strip().upper() or None
    rep_arg = request.args.get("sales_user_id")
    sales_user_id = None
    if rep_arg not in (None, "", "all"):
        try:
            sales_user_id = int(rep_arg)
        except ValueError:
            return error_response("invalid_input", 400)

    intervention_arg = (request.args.get("has_intervention") or "").strip().lower()
    if intervention_arg in ("true", "1", "yes"):
        intervention_filter = True
    elif intervention_arg in ("false", "0", "no"):
        intervention_filter = False
    else:
        intervention_filter = None

    search = (request.args.get("search") or "").strip()
    try:
        limit = int(request.args.get("limit") or _LEADS_DEFAULT_LIMIT)
    except ValueError:
        return error_response("invalid_input", 400)
    limit = max(1, min(limit, _LEADS_MAX_LIMIT))

    cursor_ts, cursor_id = _decode_leads_cursor(request.args.get("cursor"))

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM marketing_campaigns WHERE id = %s",
                (campaign_id,),
            )
            if not cur.fetchone():
                return error_response("not_found", 404)

        # Build the WHERE clauses dynamically. Use parameter placeholders
        # only — concatenating user input would be a SQL-injection foot-gun.
        clauses = ["l.campaign_id = %s"]
        params = [campaign_id]
        if stage_filter:
            clauses.append("latest.normalized_stage = %s")
            params.append(stage_filter)
        if sales_user_id is not None:
            clauses.append("latest.sales_user_id = %s")
            params.append(sales_user_id)
        if intervention_filter is True:
            clauses.append("mi.id IS NOT NULL AND mi.status = 'OPEN'")
        elif intervention_filter is False:
            clauses.append("(mi.id IS NULL OR mi.status <> 'OPEN')")
        if search:
            clauses.append("(l.client_name ILIKE %s OR l.mobile ILIKE %s)")
            like = f"%{search}%"
            params.extend([like, like])
        if cursor_ts is not None and cursor_id is not None:
            # (last_event_at, lead_id) < (cursor) — strictly less than the
            # cursor for "next page". NULL last_event_at sorts last and is
            # the trailing edge of the dataset.
            clauses.append(
                "(latest.follow_date < %s "
                "OR (latest.follow_date IS NOT DISTINCT FROM %s AND l.id < %s))"
            )
            params.extend([cursor_ts, cursor_ts, cursor_id])

        where_sql = " AND ".join(clauses)

        # +1 trick: ask for one extra row so we can detect whether more
        # results exist without a second COUNT query.
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT l.id AS lead_id, l.client_name, l.mobile,
                       latest.normalized_stage AS latest_stage,
                       latest.sales_user_id    AS current_sales_user_id,
                       latest.follow_date      AS last_event_at,
                       counts.event_count,
                       u.full_name             AS current_sales_rep,
                       mi.priority             AS intervention_priority,
                       (mi.id IS NOT NULL AND mi.status = 'OPEN') AS has_intervention
                FROM leads l
                LEFT JOIN LATERAL (
                    SELECT normalized_stage, sales_user_id, follow_date
                    FROM lead_events
                    WHERE lead_id = l.id AND is_voided = FALSE
                    ORDER BY follow_date DESC NULLS LAST, id DESC
                    LIMIT 1
                ) latest ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS event_count
                    FROM lead_events
                    WHERE lead_id = l.id AND is_voided = FALSE
                ) counts ON TRUE
                LEFT JOIN users u ON u.id = latest.sales_user_id
                LEFT JOIN manager_intervention_flags mi ON mi.lead_id = l.id
                WHERE {where_sql}
                ORDER BY latest.follow_date DESC NULLS LAST, l.id DESC
                LIMIT %s
                """,
                params + [limit + 1],
            )
            rows = cur.fetchall()

            # Total count under the same filters, but without the cursor
            # clause — pagination shouldn't change the total. Re-build
            # the WHERE here so the count reflects ONLY the filters.
            count_clauses = clauses[:]
            count_params = params[:]
            if cursor_ts is not None:
                # Drop the cursor's 3 trailing params and its clause.
                count_clauses = clauses[:-1]
                count_params = params[:-3]
            count_where = " AND ".join(count_clauses)
            cur.execute(
                f"""
                SELECT COUNT(*) AS n FROM leads l
                LEFT JOIN LATERAL (
                    SELECT normalized_stage, sales_user_id, follow_date
                    FROM lead_events
                    WHERE lead_id = l.id AND is_voided = FALSE
                    ORDER BY follow_date DESC NULLS LAST, id DESC
                    LIMIT 1
                ) latest ON TRUE
                LEFT JOIN manager_intervention_flags mi ON mi.lead_id = l.id
                WHERE {count_where}
                """,
                count_params,
            )
            total_count = cur.fetchone()["n"]

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = None
        if has_more and page_rows:
            tail = page_rows[-1]
            next_cursor = _encode_leads_cursor(tail["last_event_at"], tail["lead_id"])

        leads_out = []
        for r in page_rows:
            leads_out.append({
                "lead_id": r["lead_id"],
                "client_name": r["client_name"],
                "mobile": r["mobile"],
                "latest_stage": r["latest_stage"],
                "current_sales_rep": r["current_sales_rep"],
                "current_sales_user_id": r["current_sales_user_id"],
                "event_count": r["event_count"] or 0,
                "last_event_at": (
                    r["last_event_at"].isoformat() if r["last_event_at"] else None
                ),
                "has_intervention": bool(r["has_intervention"]),
                "intervention_priority": (
                    r["intervention_priority"] if r["has_intervention"] else None
                ),
            })

        return jsonify({
            "leads": leads_out,
            "next_cursor": next_cursor,
            "total_count": total_count,
        })
    except Exception as e:
        log.error("campaign_leads %s: %s", campaign_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET lead timeline (P3) ─────────────────────────────────────────────

@crm_bp.route("/leads/<int:lead_id>/timeline", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def lead_timeline(lead_id: int):
    """Full timeline for one lead. Heavy lifting (rep-key transfer
    detection, risk classification) lives in crm_logic.build_lead_timeline
    so the same logic is exercised by the smoke tests."""
    conn = None
    try:
        conn = get_conn()
        payload = build_lead_timeline(lead_id, conn)
        if payload is None:
            return error_response("not_found", 404)
        return jsonify(payload)
    except Exception as e:
        log.error("lead_timeline %s: %s", lead_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET cross-campaign intervention inbox (P3) ─────────────────────────

@crm_bp.route("/intervention", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def intervention_inbox():
    """Same row shape as /campaigns/<id>/intervention but across every
    campaign. Adds campaign_name to each row so the inbox can identify
    which campaign the flag belongs to. Order matches the per-campaign
    list: HIGH first, then most-recently-broken-down."""
    status_arg = (request.args.get("status") or "OPEN").upper()
    priority_arg = (request.args.get("priority") or "all").upper()
    campaign_arg = request.args.get("campaign_id")

    if status_arg != "ALL" and status_arg not in _VALID_INTERVENTION_STATUSES:
        return error_response("invalid_input", 400)
    if priority_arg != "ALL" and priority_arg not in _VALID_INTERVENTION_PRIORITIES:
        return error_response("invalid_input", 400)

    campaign_filter = None
    if campaign_arg not in (None, "", "all"):
        try:
            campaign_filter = int(campaign_arg)
        except ValueError:
            return error_response("invalid_input", 400)

    clauses = []
    params = []
    if status_arg != "ALL":
        clauses.append("m.status = %s")
        params.append(status_arg)
    if priority_arg != "ALL":
        clauses.append("m.priority = %s")
        params.append(priority_arg)
    if campaign_filter is not None:
        clauses.append("m.campaign_id = %s")
        params.append(campaign_filter)
    where_sql = " AND ".join(clauses) if clauses else "TRUE"

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT m.id, m.lead_id, m.campaign_id, m.sales_user_id,
                       m.trigger_type, m.current_stage, m.previous_positive_stage,
                       m.priority, m.last_positive_stage_date, m.last_no_answer_date,
                       m.last_comment, m.status, m.created_at, m.updated_at,
                       m.reviewed_by, m.reviewed_at,
                       l.client_name, l.mobile,
                       c.campaign_name,
                       usr.full_name AS current_sales_rep_name,
                       ru.full_name  AS reviewed_by_name
                FROM manager_intervention_flags m
                JOIN leads l                 ON l.id = m.lead_id
                JOIN marketing_campaigns c   ON c.id = m.campaign_id
                LEFT JOIN users usr ON usr.id = m.sales_user_id
                LEFT JOIN users ru  ON ru.id  = m.reviewed_by
                WHERE {where_sql}
                ORDER BY
                  CASE m.priority WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                  m.last_no_answer_date DESC NULLS LAST,
                  m.id DESC
                LIMIT 200
                """,
                params,
            )
            rows = cur.fetchall()

            # Side stats — count by status/priority across the visible
            # scope (ignoring the current filter, so the bar values don't
            # collapse when the user narrows down).
            cur.execute(
                """
                SELECT priority, COUNT(*) AS n
                FROM manager_intervention_flags
                WHERE status = 'OPEN'
                GROUP BY priority
                """
            )
            open_breakdown = {"HIGH": 0, "MEDIUM": 0}
            for r in cur.fetchall():
                if r["priority"] in open_breakdown:
                    open_breakdown[r["priority"]] = r["n"]

            cur.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE status = 'OPEN')     AS open_total,
                  COUNT(*) FILTER (WHERE status = 'REVIEWED'
                                   AND reviewed_at >= NOW() - INTERVAL '7 days') AS reviewed_7d,
                  COUNT(*) FILTER (WHERE status = 'CLOSED'
                                   AND reviewed_at >= NOW() - INTERVAL '7 days') AS closed_7d
                FROM manager_intervention_flags
                """
            )
            counts = cur.fetchone()

        return jsonify({
            "rows": [_intervention_row_to_json(r) for r in rows],
            "stats": {
                "open_total": counts["open_total"] or 0,
                "open_high": open_breakdown["HIGH"],
                "open_medium": open_breakdown["MEDIUM"],
                "reviewed_7d": counts["reviewed_7d"] or 0,
                "closed_7d": counts["closed_7d"] or 0,
            },
        })
    except Exception as e:
        log.error("intervention_inbox: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


def _intervention_row_to_json(r):
    """Shared row shape between /campaigns/<id>/intervention and /intervention."""
    return {
        "id": r["id"],
        "lead_id": r["lead_id"],
        "campaign_id": r["campaign_id"],
        "campaign_name": r.get("campaign_name"),
        "client_name": r["client_name"],
        "mobile": r["mobile"],
        "current_sales_rep_id": r["sales_user_id"],
        "current_sales_rep_name": r["current_sales_rep_name"],
        "trigger_type": r["trigger_type"],
        "current_stage": r["current_stage"],
        "previous_positive_stage": r["previous_positive_stage"],
        "priority": r["priority"],
        "last_positive_stage_date": (
            r["last_positive_stage_date"].isoformat()
            if r["last_positive_stage_date"] else None
        ),
        "last_no_answer_date": (
            r["last_no_answer_date"].isoformat()
            if r["last_no_answer_date"] else None
        ),
        "last_comment": r["last_comment"],
        "status": r["status"],
        "reviewed_by": r.get("reviewed_by"),
        "reviewed_by_name": r.get("reviewed_by_name"),
        "reviewed_at": (
            r["reviewed_at"].isoformat() if r.get("reviewed_at") else None
        ),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


# ─── PATCH intervention flag status (P3) ────────────────────────────────

@crm_bp.route("/intervention/<int:flag_id>", methods=["PATCH"])
@login_required
@role_required("admin", "manager")  # marketing can READ but not action.
@csrf_protect
def update_intervention(flag_id: int):
    """Move the flag through OPEN → REVIEWED → CLOSED (or back to OPEN).
    Records who actioned it + when. The recalc on next upload will only
    touch this row's description fields — preservation rule from P1b."""
    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or "").strip().upper()
    if new_status not in _VALID_INTERVENTION_STATUSES:
        return error_response("invalid_input", 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # If the new status is OPEN, clear the reviewer fields — the
            # row is "back in queue" and shouldn't keep a stale signature
            # of who last touched it.
            if new_status == "OPEN":
                cur.execute(
                    """
                    UPDATE manager_intervention_flags
                    SET status = %s, reviewed_by = NULL, reviewed_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, lead_id, campaign_id, status
                    """,
                    (new_status, flag_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE manager_intervention_flags
                    SET status = %s, reviewed_by = %s, reviewed_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, lead_id, campaign_id, status
                    """,
                    (new_status, session["user_id"], flag_id),
                )
            row = cur.fetchone()
            if not row:
                return error_response("not_found", 404)

            # Keep campaign_kpis.manager_intervention_count in sync —
            # we just bumped a row in or out of the OPEN bucket.
            cur.execute(
                """
                UPDATE campaign_kpis SET
                  manager_intervention_count = (
                    SELECT COUNT(*) FROM manager_intervention_flags
                    WHERE campaign_id = %s AND status = 'OPEN'
                  ),
                  updated_at = NOW()
                WHERE campaign_id = %s
                """,
                (row["campaign_id"], row["campaign_id"]),
            )
        conn.commit()
        return jsonify({"ok": True, "id": row["id"], "status": row["status"]})
    except Exception as e:
        log.error("update_intervention %s: %s", flag_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET open-count for the sidebar badge (P3) ──────────────────────────

@crm_bp.route("/intervention/open-count", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def intervention_open_count():
    """Tiny endpoint behind the sidebar badge. Pulled on every page load
    via common.js, so the SQL stays a single GROUP BY against the indexed
    (priority, status) — no joins. Returns {open_count, high_priority}.
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT priority, COUNT(*) FROM manager_intervention_flags
                WHERE status = 'OPEN'
                GROUP BY priority
                """
            )
            high = 0
            medium = 0
            for priority, n in cur.fetchall():
                if priority == "HIGH":
                    high = n
                elif priority == "MEDIUM":
                    medium = n
        return jsonify({
            "open_count": high + medium,
            "high_priority": high,
            "medium_priority": medium,
        })
    except Exception as e:
        log.error("intervention_open_count: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET daily activity (P4) ────────────────────────────────────────────

_DAILY_DEFAULT_DAYS = 30


def _parse_iso_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s).strip())
    except (ValueError, TypeError):
        return None


@crm_bp.route("/campaigns/<int:campaign_id>/daily-activity", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def campaign_daily_activity(campaign_id: int):
    """Per-day event aggregation. Counts are based on event ROWS (not
    unique leads) — same day can have multiple attempts on the same
    lead and we want to see them all. NULL-stage events are excluded
    (they're tracked as warnings via the unmatched-suggestions feed).

    Default range: last 30 days. The frontend's two date inputs map to
    `from` and `to` (inclusive on both ends).
    """
    to_d = _parse_iso_date(request.args.get("to")) or date.today()
    from_d = _parse_iso_date(request.args.get("from"))
    if from_d is None:
        from_d = to_d - timedelta(days=_DAILY_DEFAULT_DAYS - 1)
    if from_d > to_d:
        return error_response("range_inverted", 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM marketing_campaigns WHERE id = %s",
                (campaign_id,),
            )
            if not cur.fetchone():
                return error_response("not_found", 404)

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # FILTER (...) lets one pass produce all six per-stage buckets;
            # cheaper than a CASE-based pivot for the volumes we expect.
            cur.execute(
                """
                SELECT DATE(follow_date) AS day,
                       COUNT(*) AS total_attempts,
                       COUNT(*) FILTER (WHERE normalized_stage = 'NO_ANSWER')    AS no_answer_count,
                       COUNT(*) FILTER (WHERE normalized_stage = 'FOLLOWING')    AS following_count,
                       COUNT(*) FILTER (WHERE normalized_stage = 'MEETING')      AS meeting_count,
                       COUNT(*) FILTER (WHERE normalized_stage = 'CANCELLATION') AS cancellation_count,
                       COUNT(*) FILTER (WHERE normalized_stage = 'INTERESTED')   AS interested_count,
                       COUNT(*) FILTER (WHERE normalized_stage = 'REQUEST')      AS request_count
                FROM lead_events
                WHERE campaign_id = %s
                  AND normalized_stage IS NOT NULL
                  AND is_voided = FALSE
                  AND follow_date >= %s::date
                  AND follow_date <  (%s::date + INTERVAL '1 day')
                GROUP BY DATE(follow_date)
                ORDER BY DATE(follow_date) ASC
                """,
                (campaign_id, from_d, to_d),
            )
            rows = cur.fetchall()

        days = []
        total_attempts = 0
        total_answered = 0
        for r in rows:
            total = r["total_attempts"] or 0
            no_ans = r["no_answer_count"] or 0
            answered = total - no_ans
            days.append({
                "date": r["day"].isoformat(),
                "total_attempts": total,
                "no_answer_count": no_ans,
                "following_count": r["following_count"] or 0,
                "meeting_count": r["meeting_count"] or 0,
                "cancellation_count": r["cancellation_count"] or 0,
                "interested_count": r["interested_count"] or 0,
                "request_count": r["request_count"] or 0,
                "answered_count": answered,
                "response_rate_pct": _response_rate_pct(total, no_ans),
            })
            total_attempts += total
            total_answered += answered

        return jsonify({
            "campaign_id": campaign_id,
            "from": from_d.isoformat(),
            "to": to_d.isoformat(),
            "days": days,
            "totals": {
                "total_attempts": total_attempts,
                "answered_count": total_answered,
                "response_rate_pct": _response_rate_pct(total_attempts, total_attempts - total_answered),
            },
        })
    except Exception as e:
        log.error("campaign_daily_activity %s: %s", campaign_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET marketing report (unique-lead aggregation) (P4) ────────────────

@crm_bp.route("/campaigns/<int:campaign_id>/marketing-report", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def campaign_marketing_report(campaign_id: int):
    """Unique-lead rollup — same DISTINCT ON pattern as campaign_kpis so
    the two views never disagree on which stage a lead "is in". Computes
    rates against total_unique_leads (skipping leads whose every event
    was NULL-stage)."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM marketing_campaigns WHERE id = %s",
                (campaign_id,),
            )
            if not cur.fetchone():
                return error_response("not_found", 404)

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (le.lead_id)
                           le.lead_id, le.normalized_stage
                    FROM lead_events le
                    JOIN leads l ON l.id = le.lead_id
                    WHERE l.campaign_id = %s
                      AND le.normalized_stage IS NOT NULL
                      AND le.is_voided = FALSE
                    ORDER BY le.lead_id, le.follow_date DESC NULLS LAST, le.id DESC
                )
                SELECT normalized_stage, COUNT(*) AS n FROM latest
                GROUP BY normalized_stage
                """,
                (campaign_id,),
            )
            buckets = {r["normalized_stage"]: r["n"] for r in cur.fetchall()}

            cur.execute(
                """
                SELECT COUNT(*) FILTER (WHERE status = 'OPEN') AS open_intervention
                FROM manager_intervention_flags
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            intervention = cur.fetchone()
            open_intervention = (intervention["open_intervention"] or 0) if intervention else 0

        # Every stage key surfaces — the front-end always has a key to
        # read from even when the count is zero.
        breakdown = {tok: buckets.get(tok, 0) for tok in (
            "NO_ANSWER", "FOLLOWING", "MEETING",
            "CANCELLATION", "INTERESTED", "REQUEST",
        )}
        total_unique = sum(breakdown.values())
        no_answer = breakdown["NO_ANSWER"]
        answered = total_unique - no_answer

        no_answer_rate = round(((no_answer / total_unique) * 100.0), 1) if total_unique > 0 else 0.0
        intervention_rate = round(((open_intervention / total_unique) * 100.0), 1) if total_unique > 0 else 0.0

        return jsonify({
            "campaign_id": campaign_id,
            "total_unique_leads": total_unique,
            "answered_unique_leads": answered,
            "no_answer_unique_leads": no_answer,
            "stage_breakdown_unique": breakdown,
            "response_rate_pct": _response_rate_pct(total_unique, no_answer),
            "no_answer_rate_pct": no_answer_rate,
            "intervention_rate_pct": intervention_rate,
            "open_intervention_count": open_intervention,
        })
    except Exception as e:
        log.error("campaign_marketing_report %s: %s", campaign_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── Admin CRM mappings — CRUD + suggestions (P4) ───────────────────────

# Canonical stage tokens — admin can only map to one of these. Keeping
# the set tight prevents typos from creating a stage the rest of the
# pipeline doesn't know how to handle.
_CANONICAL_STAGE_TOKENS = frozenset(set(DEFAULT_STAGE_MAP.values()))


@crm_bp.route("/stage-mappings", methods=["GET"])
@login_required
@role_required("admin")
def list_stage_mappings():
    """List every stage mapping, joined with the campaign name where the
    mapping is per-campaign (NULL campaign_id = global). Ordered by
    scope first (globals on top) then most-recently-created."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT s.id, s.campaign_id, c.campaign_name,
                       s.raw_stage, s.normalized_stage,
                       s.created_by, u.full_name AS created_by_name,
                       s.created_at
                FROM stage_mappings s
                LEFT JOIN marketing_campaigns c ON c.id = s.campaign_id
                LEFT JOIN users u                ON u.id = s.created_by
                ORDER BY (s.campaign_id IS NULL) DESC, s.created_at DESC
                """,
            )
            rows = cur.fetchall()
        return jsonify([{
            "id": r["id"],
            "campaign_id": r["campaign_id"],
            "campaign_name": r["campaign_name"],
            "raw_stage": r["raw_stage"],
            "normalized_stage": r["normalized_stage"],
            "created_by": r["created_by"],
            "created_by_name": r["created_by_name"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in rows])
    except Exception as e:
        log.error("list_stage_mappings: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


@crm_bp.route("/stage-mappings", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def create_stage_mapping():
    """Add a stage mapping AND retroactively re-derive every matching
    event's normalized_stage. Affected campaigns get a full recalc so
    KPIs and intervention reflect the new mapping immediately."""
    data = request.get_json(silent=True) or {}
    raw_stage = (data.get("raw_stage") or "").strip()
    normalized = (data.get("normalized_stage") or "").strip().upper()
    campaign_raw = data.get("campaign_id")

    if not raw_stage:
        return error_response("required_fields_missing", 400)
    if normalized not in _CANONICAL_STAGE_TOKENS:
        return error_response("invalid_input", 400)
    campaign_id = None
    if campaign_raw not in (None, "", "all"):
        try:
            campaign_id = int(campaign_raw)
        except (ValueError, TypeError):
            return error_response("invalid_input", 400)

    conn = None
    try:
        conn = get_conn()
        if campaign_id is not None:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM marketing_campaigns WHERE id = %s", (campaign_id,))
                if not cur.fetchone():
                    return error_response("not_found", 404)

        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO stage_mappings
                        (campaign_id, raw_stage, normalized_stage, created_by)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (campaign_id, raw_stage, normalized, session.get("user_id")),
                )
                new_id = cur.fetchone()[0]
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                return error_response("invalid_input", 409)
        conn.commit()

        # Retroactive recalc — the mapping is committed, so the
        # normalize_stage lookup will see it.
        affected = apply_stage_mapping_change(raw_stage, campaign_id, conn)
        for cid in affected:
            recalc_after_upload(cid, conn)
        log.info("Stage mapping %s created (raw=%r → %s, scope=%s); recalculated %d campaign(s)",
                 new_id, raw_stage, normalized, campaign_id or "GLOBAL", len(affected))
        return jsonify({
            "ok": True, "id": new_id,
            "affected_campaigns": sorted(list(affected)),
        }), 201
    except Exception as e:
        log.error("create_stage_mapping: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


@crm_bp.route("/stage-mappings/<int:mapping_id>", methods=["DELETE"])
@login_required
@role_required("admin")
@csrf_protect
def delete_stage_mapping(mapping_id: int):
    """Delete the mapping then retroactively re-derive normalized_stage
    for matching events. The same event might be picked up by another
    surviving mapping (per-campaign override or default), in which case
    its value just changes; otherwise it falls back to NULL."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw_stage, campaign_id FROM stage_mappings WHERE id = %s",
                (mapping_id,),
            )
            row = cur.fetchone()
            if not row:
                return error_response("not_found", 404)
            raw_stage, campaign_id = row
            cur.execute("DELETE FROM stage_mappings WHERE id = %s", (mapping_id,))
        conn.commit()

        affected = apply_stage_mapping_change(raw_stage, campaign_id, conn)
        for cid in affected:
            recalc_after_upload(cid, conn)
        log.info("Stage mapping %s deleted; recalculated %d campaign(s)",
                 mapping_id, len(affected))
        return jsonify({
            "ok": True,
            "affected_campaigns": sorted(list(affected)),
        })
    except Exception as e:
        log.error("delete_stage_mapping %s: %s", mapping_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


@crm_bp.route("/sales-rep-mappings", methods=["GET"])
@login_required
@role_required("admin")
def list_sales_rep_mappings():
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT m.id, m.campaign_id, c.campaign_name,
                       m.raw_name, m.sales_user_id,
                       u.full_name AS sales_user_name,
                       m.created_by, cu.full_name AS created_by_name,
                       m.created_at
                FROM sales_rep_mappings m
                LEFT JOIN marketing_campaigns c ON c.id = m.campaign_id
                LEFT JOIN users u                ON u.id = m.sales_user_id
                LEFT JOIN users cu               ON cu.id = m.created_by
                ORDER BY (m.campaign_id IS NULL) DESC, m.created_at DESC
                """,
            )
            rows = cur.fetchall()
        return jsonify([{
            "id": r["id"],
            "campaign_id": r["campaign_id"],
            "campaign_name": r["campaign_name"],
            "raw_name": r["raw_name"],
            "sales_user_id": r["sales_user_id"],
            "sales_user_name": r["sales_user_name"],
            "created_by": r["created_by"],
            "created_by_name": r["created_by_name"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in rows])
    except Exception as e:
        log.error("list_sales_rep_mappings: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


@crm_bp.route("/sales-rep-mappings", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def create_sales_rep_mapping():
    """Map a raw rep-name string to an existing user. Retroactive recalc
    flips the sales_user_id on every event that had this unmatched name
    and runs the full recalc chain — assignments rebuild because the
    rep IDENTITY changed (unmatched raw_name → matched user_id)."""
    data = request.get_json(silent=True) or {}
    raw_name = (data.get("raw_name") or "").strip()
    sales_user_raw = data.get("sales_user_id")
    campaign_raw = data.get("campaign_id")

    if not raw_name:
        return error_response("required_fields_missing", 400)
    try:
        sales_user_id = int(sales_user_raw)
    except (ValueError, TypeError):
        return error_response("invalid_input", 400)
    campaign_id = None
    if campaign_raw not in (None, "", "all"):
        try:
            campaign_id = int(campaign_raw)
        except (ValueError, TypeError):
            return error_response("invalid_input", 400)

    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM users WHERE id = %s AND role IN ('sales','team_leader')",
                (sales_user_id,),
            )
            if not cur.fetchone():
                return error_response("not_found", 404)
            if campaign_id is not None:
                cur.execute("SELECT 1 FROM marketing_campaigns WHERE id = %s", (campaign_id,))
                if not cur.fetchone():
                    return error_response("not_found", 404)

        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO sales_rep_mappings
                        (campaign_id, raw_name, sales_user_id, created_by)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (campaign_id, raw_name, sales_user_id, session.get("user_id")),
                )
                new_id = cur.fetchone()[0]
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                return error_response("invalid_input", 409)
        conn.commit()

        affected = apply_sales_rep_mapping_change(raw_name, campaign_id, conn)
        for cid in affected:
            recalc_after_upload(cid, conn)
        log.info("Sales-rep mapping %s created (raw=%r → user=%s, scope=%s); recalculated %d campaign(s)",
                 new_id, raw_name, sales_user_id, campaign_id or "GLOBAL", len(affected))
        return jsonify({
            "ok": True, "id": new_id,
            "affected_campaigns": sorted(list(affected)),
        }), 201
    except Exception as e:
        log.error("create_sales_rep_mapping: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


@crm_bp.route("/sales-rep-mappings/<int:mapping_id>", methods=["DELETE"])
@login_required
@role_required("admin")
@csrf_protect
def delete_sales_rep_mapping(mapping_id: int):
    """Delete the mapping then re-derive sales_user_id for matching
    events. Same idempotency property as the stage delete: the event's
    user_id may flip to another mapping, to a users.full_name fuzzy
    match, or back to NULL."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw_name, campaign_id FROM sales_rep_mappings WHERE id = %s",
                (mapping_id,),
            )
            row = cur.fetchone()
            if not row:
                return error_response("not_found", 404)
            raw_name, campaign_id = row
            cur.execute("DELETE FROM sales_rep_mappings WHERE id = %s", (mapping_id,))
        conn.commit()

        affected = apply_sales_rep_mapping_change(raw_name, campaign_id, conn)
        for cid in affected:
            recalc_after_upload(cid, conn)
        log.info("Sales-rep mapping %s deleted; recalculated %d campaign(s)",
                 mapping_id, len(affected))
        return jsonify({
            "ok": True,
            "affected_campaigns": sorted(list(affected)),
        })
    except Exception as e:
        log.error("delete_sales_rep_mapping %s: %s", mapping_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


@crm_bp.route("/unmatched-suggestions", methods=["GET"])
@login_required
@role_required("admin")
def unmatched_suggestions():
    """Live-state suggestions: aggregate every event currently sitting
    with a NULL normalized_stage or NULL sales_user_id, capped at 50
    rows per kind. Live state beats reading the historical
    crm_report_uploads.warnings because mappings added since those
    uploads may have already resolved some of them."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT TRIM(raw_stage) AS raw_stage, COUNT(*) AS n
                FROM lead_events
                WHERE normalized_stage IS NULL
                  AND raw_stage IS NOT NULL AND TRIM(raw_stage) <> ''
                  AND is_voided = FALSE
                GROUP BY TRIM(raw_stage)
                ORDER BY COUNT(*) DESC, TRIM(raw_stage) ASC
                LIMIT 50
                """,
            )
            stages = [{"raw_stage": r["raw_stage"], "count": r["n"]} for r in cur.fetchall()]

            cur.execute(
                """
                SELECT TRIM(raw_sales_rep_name) AS raw_name, COUNT(*) AS n
                FROM lead_events
                WHERE sales_user_id IS NULL
                  AND raw_sales_rep_name IS NOT NULL AND TRIM(raw_sales_rep_name) <> ''
                  AND is_voided = FALSE
                GROUP BY TRIM(raw_sales_rep_name)
                ORDER BY COUNT(*) DESC, TRIM(raw_sales_rep_name) ASC
                LIMIT 50
                """,
            )
            reps = [{"raw_name": r["raw_name"], "count": r["n"]} for r in cur.fetchall()]

            # Eligible sales users for the dropdown.
            cur.execute(
                """
                SELECT id, full_name, role
                FROM users
                WHERE role IN ('sales','team_leader') AND active = TRUE
                ORDER BY full_name ASC
                """,
            )
            users = [{"id": r["id"], "full_name": r["full_name"], "role": r["role"]} for r in cur.fetchall()]

            cur.execute(
                "SELECT id, campaign_name FROM marketing_campaigns ORDER BY campaign_name ASC"
            )
            campaigns = [{"id": r["id"], "campaign_name": r["campaign_name"]} for r in cur.fetchall()]

        return jsonify({
            "unmatched_stages": stages,
            "unmatched_reps": reps,
            "sales_users": users,
            "campaigns": campaigns,
            "stage_tokens": sorted(_CANONICAL_STAGE_TOKENS),
        })
    except Exception as e:
        log.error("unmatched_suggestions: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET cross-campaign summary (powers the /marketing CRM table) ───────

@crm_bp.route("/campaigns-summary", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def campaigns_summary():
    """List of campaigns with their CRM rollup — feeds the table on
    /marketing under "CRM Reports". Returns every campaign visible to the
    caller (no scoping in marketing_bp either, so we match it).
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT c.id, c.campaign_name, c.created_at,
                       COALESCE(k.total_leads, 0) AS total_leads,
                       COALESCE(k.manager_intervention_count, 0) AS open_intervention,
                       k.last_upload_at,
                       k.stage_counts
                FROM marketing_campaigns c
                LEFT JOIN campaign_kpis k ON k.campaign_id = c.id
                ORDER BY
                    k.last_upload_at DESC NULLS LAST,
                    c.created_at DESC
                """
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "campaign_id": r["id"],
                "campaign_name": r["campaign_name"],
                "total_leads": r["total_leads"],
                "open_intervention": r["open_intervention"],
                "last_upload_at": (
                    r["last_upload_at"].isoformat() if r["last_upload_at"] else None
                ),
                "stage_counts": r["stage_counts"] or {},
            })
        return jsonify(out)
    except Exception as e:
        log.error("campaigns_summary: %s", e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()


# ─── GET recent uploads for a campaign ──────────────────────────────────

@crm_bp.route("/campaigns/<int:campaign_id>/uploads", methods=["GET"])
@login_required
@role_required("admin", "manager", "marketing")
def list_uploads(campaign_id: int):
    conn = None
    try:
        conn = get_conn()
        if not _campaign_exists(conn, campaign_id):
            return error_response("not_found", 404)

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT u.id, u.file_name, u.status,
                       u.total_rows, u.total_leads, u.total_events,
                       u.new_events, u.duplicate_events,
                       u.is_voided, u.error_message,
                       u.created_at, u.processed_at,
                       u.uploaded_by, usr.full_name AS uploaded_by_name
                FROM crm_report_uploads u
                LEFT JOIN users usr ON usr.id = u.uploaded_by
                WHERE u.campaign_id = %s
                ORDER BY u.created_at DESC
                LIMIT 50
                """,
                (campaign_id,),
            )
            rows = cur.fetchall()

        out = []
        for r in rows:
            out.append({
                "upload_id": r["id"],
                "file_name": r["file_name"],
                "status": r["status"],
                "total_rows": r["total_rows"],
                "total_leads": r["total_leads"],
                "total_events": r["total_events"],
                "new_events": r["new_events"],
                "duplicate_events": r["duplicate_events"],
                "is_voided": r["is_voided"],
                "error_message": r["error_message"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
                "uploaded_by": r["uploaded_by"],
                "uploaded_by_name": r["uploaded_by_name"],
            })
        return jsonify(out)
    except Exception as e:
        log.error("list_uploads campaign=%s: %s", campaign_id, e)
        return error_response("server", 500)
    finally:
        if conn is not None:
            conn.close()
