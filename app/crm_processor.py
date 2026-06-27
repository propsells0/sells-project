"""
Background worker for CRM-report uploads.

The HTTP endpoint inserts a `crm_report_uploads` row with status=PENDING
and hands the bytes + upload_id to `process_upload()`, which runs on a
daemon thread and walks the rows into `leads` + `lead_events`. KPI
recalculation lands in a follow-up phase — for now this is just ingest +
dedup + status reporting.

We keep a daemon thread (not a process queue) for the same reason
`app/sync_service.py` does: the production deployment runs
`gunicorn --workers 1` so a single in-proc worker is sufficient and
removes operational moving parts. If the worker count ever bumps up we
revisit and move to RQ/Celery.
"""
import io
import json
import logging
import threading
import traceback

from app.crm_logic import compute_event_hash, recalc_after_upload
from app.crm_parser import parse_crm_excel
from app.database import get_conn

log = logging.getLogger(__name__)


def start_processing_thread(upload_id: int, file_bytes: bytes, campaign_id: int) -> None:
    """Spawn the daemon worker. Returns immediately so the HTTP handler can
    200 the client right away."""
    t = threading.Thread(
        target=_process_upload_safe,
        args=(upload_id, file_bytes, campaign_id),
        name=f"crm-upload-{upload_id}",
        daemon=True,
    )
    t.start()


def _process_upload_safe(upload_id: int, file_bytes: bytes, campaign_id: int) -> None:
    """Top-level wrapper — every exception path lands the upload row in a
    terminal state (COMPLETED or FAILED) so the polling client never sees
    it hung in PROCESSING forever."""
    try:
        _process_upload(upload_id, file_bytes, campaign_id)
    except Exception as exc:
        log.error("CRM upload %s crashed: %s\n%s", upload_id, exc, traceback.format_exc())
        _mark_failed(upload_id, str(exc))


def _process_upload(upload_id: int, file_bytes: bytes, campaign_id: int) -> None:
    # Move PENDING → PROCESSING so the status endpoint reflects work in
    # flight. A separate connection from the parse/insert connection isn't
    # needed — we commit between phases.
    _set_status(upload_id, "PROCESSING")

    conn = None
    try:
        conn = get_conn()

        # ── Parse ────────────────────────────────────────────────────────
        # The parser uses the same connection to look up admin-defined
        # stage/sales-rep mappings. parse_crm_excel itself is read-only
        # against the DB; we don't commit anything until after parsing.
        parse_result = parse_crm_excel(
            io.BytesIO(file_bytes), campaign_id=campaign_id, conn=conn
        )
        rows = parse_result["rows"]
        warnings = list(parse_result["warnings"])
        unmatched_reps = list(parse_result["unmatched_sales_reps"])
        unmatched_stages = list(parse_result["unmatched_stages"])
        total_rows_in_sheet = parse_result["total_rows_in_sheet"]

        new_events = 0
        duplicate_events = 0
        leads_touched: set = set()

        # ── Ingest row by row ────────────────────────────────────────────
        # Each row is its own transaction (SAVEPOINT) so one malformed row
        # can't poison the upload. The unique constraint on event_hash is
        # the dedup mechanism — re-uploading the same sheet bumps
        # duplicate_events, not new_events.
        for row in rows:
            try:
                with conn.cursor() as cur:
                    # 1. Get-or-create the lead.
                    lead_id = _upsert_lead(cur, campaign_id, row)
                    leads_touched.add(lead_id)

                    # 2. Compute event_hash and try to insert.
                    event_hash = compute_event_hash(
                        campaign_id=campaign_id,
                        mobile=row["mobile"],
                        follow_date=row["follow_date"],
                        raw_sales_rep=row["raw_sales_rep_name"],
                        normalized_stage=row["normalized_stage"],
                        comment=row["comment"],
                    )

                    cur.execute(
                        """
                        INSERT INTO lead_events (
                            lead_id, campaign_id, sales_user_id,
                            raw_sales_rep_name, raw_stage, normalized_stage,
                            follow_date, comment,
                            source_upload_id, source_row_number, event_hash
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (event_hash) DO NOTHING
                        RETURNING id
                        """,
                        (
                            lead_id,
                            campaign_id,
                            row["sales_user_id"],
                            row["raw_sales_rep_name"],
                            row["raw_stage"],
                            row["normalized_stage"],
                            row["follow_date"],
                            row["comment"],
                            upload_id,
                            row["row_number"],
                            event_hash,
                        ),
                    )
                    inserted = cur.fetchone()
                    if inserted:
                        new_events += 1
                    else:
                        duplicate_events += 1
                conn.commit()
            except Exception as row_exc:
                # Rollback the bad row's work and keep going. The Postgres
                # connection enters an aborted state on error, so the
                # rollback is mandatory before the next row's INSERT.
                conn.rollback()
                warnings.append(
                    f"Row {row.get('row_number')}: skipped — {type(row_exc).__name__}: {row_exc}"
                )
                log.warning("CRM upload %s, row %s skipped: %s",
                            upload_id, row.get("row_number"), row_exc)

        # ── Recalc KPIs + Manager Intervention ──────────────────────────
        # Runs before the COMPLETED flip so the overview endpoint never
        # serves stale aggregates between "events landed" and "rollups
        # caught up". Either recalc raising propagates to the outer
        # exception handler, which marks the upload FAILED — better than
        # leaving the user with a green "completed" toast on a sheet that
        # corrupted the dashboards.
        try:
            recalc_after_upload(campaign_id, conn)
        except Exception as recalc_exc:
            warnings.append(
                f"Recalc failed after ingest: {type(recalc_exc).__name__}: {recalc_exc}"
            )
            log.error("CRM upload %s recalc failed: %s\n%s",
                      upload_id, recalc_exc, traceback.format_exc())
            raise

        # ── Finalize ────────────────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crm_report_uploads SET
                    status              = 'COMPLETED',
                    total_rows          = %s,
                    total_leads         = %s,
                    total_events        = %s,
                    new_events          = %s,
                    duplicate_events    = %s,
                    unmatched_sales_reps= %s::jsonb,
                    unmatched_stages    = %s::jsonb,
                    warnings            = %s::jsonb,
                    processed_at        = NOW()
                WHERE id = %s
                """,
                (
                    total_rows_in_sheet,
                    len(leads_touched),
                    new_events + duplicate_events,
                    new_events,
                    duplicate_events,
                    json.dumps(unmatched_reps),
                    json.dumps(unmatched_stages),
                    json.dumps(warnings),
                    upload_id,
                ),
            )
        conn.commit()
        log.info(
            "✅ CRM upload %s COMPLETED — leads=%s new=%s dup=%s warnings=%s",
            upload_id, len(leads_touched), new_events, duplicate_events, len(warnings),
        )
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _upsert_lead(cur, campaign_id: int, row: dict) -> int:
    """Find the lead row for (campaign, mobile) or create it. Returns id.

    If the lead already exists with an empty client_name and this row
    carries a non-empty one (continuation row of an earlier upload), we
    backfill the name so future reports/timelines show something useful.
    """
    cur.execute(
        "SELECT id, client_name FROM leads WHERE campaign_id = %s AND mobile = %s",
        (campaign_id, row["mobile"]),
    )
    existing = cur.fetchone()
    if existing:
        lead_id, existing_name = existing
        if (not existing_name) and row["client_name"]:
            cur.execute(
                "UPDATE leads SET client_name = %s, updated_at = NOW() WHERE id = %s",
                (row["client_name"], lead_id),
            )
        return lead_id

    cur.execute(
        """
        INSERT INTO leads (campaign_id, client_name, mobile)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (campaign_id, row["client_name"] or None, row["mobile"]),
    )
    return cur.fetchone()[0]


# ─── Status helpers ─────────────────────────────────────────────────────

def _set_status(upload_id: int, status: str) -> None:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_report_uploads SET status = %s WHERE id = %s",
                (status, upload_id),
            )
        conn.commit()
    except Exception as e:
        log.error("Failed to mark upload %s as %s: %s", upload_id, status, e)
    finally:
        if conn is not None:
            conn.close()


def _mark_failed(upload_id: int, message: str) -> None:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE crm_report_uploads SET
                    status        = 'FAILED',
                    error_message = %s,
                    processed_at  = NOW()
                WHERE id = %s
                """,
                (message[:2000] if message else "unknown error", upload_id),
            )
        conn.commit()
    except Exception as e:
        log.error("Could not even mark upload %s FAILED: %s", upload_id, e)
    finally:
        if conn is not None:
            conn.close()
