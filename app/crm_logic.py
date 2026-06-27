"""
CRM Report ingestion — single source of truth for normalization & matching.

This module is to the CRM upload pipeline what app/kpi_logic.py is to the
monthly KPI flow: every helper that the parser, the background processor,
the blueprint, and (future) KPI recalculators need lives here.

Phase 1a deliberately scopes this to NORMALIZATION + MATCHING + HASHING.
Recalc functions (campaign/sales KPIs, manager intervention) land in P1b.
Do not add side-effecting recalcs here in this phase.
"""
import hashlib
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)


# ─── Stage mapping ──────────────────────────────────────────────────────
#
# Source-of-truth table for "raw CRM stage string" → "internal token". KPIs
# and Manager-Intervention rules operate on the TOKEN, never on the raw
# string. Lookup at runtime is three-tier (see normalize_stage):
#
#   1. stage_mappings WHERE campaign_id = <this campaign>     (override)
#   2. stage_mappings WHERE campaign_id IS NULL               (admin global)
#   3. DEFAULT_STAGE_MAP                                      (this dict)
#   4. None → bubbles up as an "unmatched stage" warning
#
# Keys are compared after lower() + strip(). New tokens added here must
# also be handled wherever stage logic branches (intervention rules, etc.).
DEFAULT_STAGE_MAP = {
    "no answer":           "NO_ANSWER",
    "1st call no answer":  "NO_ANSWER",
    "no response":         "NO_ANSWER",
    "following":           "FOLLOWING",
    "follow up":           "FOLLOWING",
    "followup":            "FOLLOWING",
    "meeting":             "MEETING",
    "zoom meeting":        "MEETING",
    "meeting done":        "MEETING",
    "cancellation":        "CANCELLATION",
    "cancelled":           "CANCELLATION",
    "canceled":            "CANCELLATION",
    "interested":          "INTERESTED",
    "request":             "REQUEST",
}


# ─── Mobile normalization ───────────────────────────────────────────────

_MOBILE_STRIP_CHARS = re.compile(r"[\s\-\.\(\)\+]")


def normalize_mobile(raw) -> Optional[str]:
    """Coerce a CRM mobile cell to canonical (E.164-ish, no +) digits.

    Rules — tuned for the dominant Egyptian (and occasional Gulf) numbering
    we see in practice. Strict: we'd rather drop a row than collide on a
    mobile that's actually ambiguous.

      "01012345678"        → "201012345678"   (EG, 11 digits starting "01")
      "+20 100 123 4567"   → "201001234567"
      "00201012345678"     → "201012345678"
      "+971569116811"      → "971569116811"   (foreign code untouched)
      ""                   → None
      "abcd"               → None
      "1234567"            → None             (too short)
    """
    if raw is None:
        return None
    # openpyxl returns int/float when Excel formats the cell as a number —
    # 01012345678 round-trips as 1012345678 which then loses the leading 0.
    # Force-format anything numeric back to a digit string before stripping.
    if isinstance(raw, float):
        if raw != raw:  # NaN
            return None
        raw = f"{raw:.0f}"
    elif isinstance(raw, int):
        raw = str(raw)
    s = str(raw).strip()
    if not s:
        return None

    # Strip formatting (spaces, dashes, dots, parens, plus). Do this BEFORE
    # the "00" prefix check so "+00 …" inputs aren't ambiguous.
    s = _MOBILE_STRIP_CHARS.sub("", s)

    # International "00" prefix → drop, country code remains.
    if s.startswith("00"):
        s = s[2:]

    if not s.isdigit():
        return None

    # 11-digit "01..." → Egyptian local; promote to "20" country code.
    # 10-digit "1..."  → Egyptian without the leading 0; same rule, prepend 20.
    if len(s) == 11 and s.startswith("01"):
        s = "20" + s[1:]
    elif len(s) == 10 and s.startswith("1"):
        s = "20" + s

    if len(s) < 8:
        return None
    return s


# ─── Sales-name normalization & matching ────────────────────────────────

_WS_COLLAPSE = re.compile(r"\s+")


def normalize_sales_name(raw) -> str:
    """Trim, lowercase, collapse repeated whitespace to a single space.

    "  Mahmoud   Amr " → "mahmoud amr"
    """
    if raw is None:
        return ""
    return _WS_COLLAPSE.sub(" ", str(raw).strip().lower())


# Allowed roles for CRM sales-rep matching. Team leaders are included since
# they can show up as the responsible rep in mixed-team campaigns; managers
# and admins are not — a "sale" attributed to those roles would skew KPIs.
_SALES_USER_ROLES = ("sales", "team_leader")


def match_sales_user(raw_name, campaign_id: int, conn) -> Optional[int]:
    """Resolve a CRM `Sales Rep` cell to a `users.id`.

    Lookup order:
      1. sales_rep_mappings WHERE campaign_id = <this campaign>   (override)
      2. sales_rep_mappings WHERE campaign_id IS NULL             (global)
      3. users.full_name match (normalized: lower + trimmed + ws-collapsed)
         filtered to role ∈ {sales, team_leader}
      4. None → row is still ingested but flagged as unmatched
    """
    norm = normalize_sales_name(raw_name)
    if not norm:
        return None

    with conn.cursor() as cur:
        # 1 + 2 — explicit mappings, per-campaign first, then global. Using
        # ORDER BY (campaign_id IS NULL) ASC puts non-null first, which gives
        # the per-campaign row precedence when both exist.
        cur.execute(
            """
            SELECT sales_user_id
            FROM sales_rep_mappings
            WHERE LOWER(TRIM(raw_name)) = %s
              AND (campaign_id = %s OR campaign_id IS NULL)
            ORDER BY (campaign_id IS NULL) ASC
            LIMIT 1
            """,
            (norm, campaign_id),
        )
        row = cur.fetchone()
        if row:
            return row[0]

        # 3 — fuzzy match against users.full_name with the same normalization.
        cur.execute(
            """
            SELECT id
            FROM users
            WHERE REGEXP_REPLACE(LOWER(TRIM(full_name)), '\\s+', ' ', 'g') = %s
              AND role = ANY(%s)
              AND active = TRUE
            ORDER BY id ASC
            LIMIT 1
            """,
            (norm, list(_SALES_USER_ROLES)),
        )
        row = cur.fetchone()
        if row:
            return row[0]

    return None


# ─── Stage normalization ────────────────────────────────────────────────

def normalize_stage(raw, campaign_id: Optional[int] = None, conn=None) -> Optional[str]:
    """Map a raw stage cell to the internal token, or None if unmatched.

    `conn` is optional — if not provided we skip the DB-backed mappings and
    fall straight to DEFAULT_STAGE_MAP. The parser passes `conn` so admin
    overrides work; unit tests that only exercise the default behavior can
    omit it.
    """
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None

    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT normalized_stage
                FROM stage_mappings
                WHERE LOWER(TRIM(raw_stage)) = %s
                  AND (campaign_id = %s OR campaign_id IS NULL)
                ORDER BY (campaign_id IS NULL) ASC
                LIMIT 1
                """,
                (key, campaign_id),
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0]

    return DEFAULT_STAGE_MAP.get(key)


# ─── Event hashing for dedup ────────────────────────────────────────────

def compute_event_hash(
    campaign_id: int,
    mobile: str,
    follow_date,
    raw_sales_rep,
    normalized_stage,
    comment,
) -> str:
    """SHA-256 of the natural-key tuple that identifies a CRM activity.

    Used as a UNIQUE constraint on lead_events. The same sheet re-uploaded
    produces the same hashes → ON CONFLICT DO NOTHING handles dedup.

    NOTE on raw_sales_rep: we hash the RAW name (not the resolved user_id)
    so that two events differing only by which rep wrote them stay distinct
    even if both names match the same user. follow_date is rendered as ISO
    so DST/microsecond noise doesn't cause spurious mismatches.
    """
    if follow_date is None:
        date_part = ""
    else:
        try:
            date_part = follow_date.isoformat(sep=" ", timespec="seconds")
        except (AttributeError, TypeError):
            date_part = str(follow_date)
    payload = "|".join([
        str(campaign_id),
        mobile or "",
        date_part,
        (raw_sales_rep or "").strip(),
        normalized_stage or "",
        (comment or "").strip(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ═══ Manager Intervention rules ═════════════════════════════════════════
# The token vocabulary used by the recalc and by every consumer that
# branches on intervention state. Keep these as constants — never inline
# the strings — so renames stay refactor-safe.

TRIGGER_NO_ANSWER_AFTER_FOLLOWING = "NO_ANSWER_AFTER_FOLLOWING"
TRIGGER_NO_ANSWER_AFTER_MEETING   = "NO_ANSWER_AFTER_MEETING"

PRIORITY_HIGH   = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"

# Positive stages, in escalation order. Manager Intervention is ONLY raised
# when latest stage is NO_ANSWER AND at least one of these appeared earlier.
# MEETING outranks FOLLOWING — if both appear, the priority is HIGH.
POSITIVE_STAGES = {"FOLLOWING", "MEETING"}

# Status workflow for manager_intervention_flags. The recalc only ever
# writes 'OPEN'; the inbox PATCH endpoint moves rows to REVIEWED/CLOSED.
STATUS_OPEN     = "OPEN"
STATUS_REVIEWED = "REVIEWED"
STATUS_CLOSED   = "CLOSED"


# ═══ Recalc — Campaign KPIs ═════════════════════════════════════════════
#
# Rule (from the spec, do not loosen): the stage_counts buckets count
# LEADS, not events. For each unique (campaign, mobile) we take the lead's
# LATEST event by (follow_date DESC, id DESC) and bucket the lead under
# that event's normalized_stage. Events with NULL normalized_stage are
# excluded — they only show up in the unmatched_stages warnings.

def recalc_campaign_kpis(campaign_id: int, conn) -> dict:
    """Recompute total_leads and stage_counts for the campaign; upsert into
    campaign_kpis. Returns the new row as a dict (used by the overview
    endpoint to avoid an immediate re-read)."""
    with conn.cursor() as cur:
        # DISTINCT ON lets PG hand us the latest qualifying event per lead
        # in a single pass without a window function. Sort key is
        # (follow_date DESC, id DESC) so the tiebreaker for two events at
        # the exact same timestamp is "the one written last wins" — which
        # is how the CRM displays them too.
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
            SELECT normalized_stage, COUNT(*) AS n
            FROM latest
            GROUP BY normalized_stage
            """,
            (campaign_id,),
        )
        stage_counts = {row[0]: row[1] for row in cur.fetchall()}

        # total_leads = unique leads that have at least one valid event.
        # If we counted from leads directly we'd over-count rows whose
        # events all had unmatched stages.
        cur.execute(
            """
            SELECT COUNT(DISTINCT l.id)
            FROM leads l
            JOIN lead_events le ON le.lead_id = l.id
            WHERE l.campaign_id = %s
              AND le.normalized_stage IS NOT NULL
              AND le.is_voided = FALSE
            """,
            (campaign_id,),
        )
        total_leads = cur.fetchone()[0] or 0

        # manager_intervention_count tracks OPEN flags only. Closed/reviewed
        # rows live in the table for the audit trail but don't add to the
        # "needs attention" badge on the overview.
        cur.execute(
            """
            SELECT COUNT(*) FROM manager_intervention_flags
            WHERE campaign_id = %s AND status = %s
            """,
            (campaign_id, STATUS_OPEN),
        )
        intervention_count = cur.fetchone()[0] or 0

        cur.execute(
            """
            SELECT MAX(processed_at) FROM crm_report_uploads
            WHERE campaign_id = %s AND status = 'COMPLETED' AND is_voided = FALSE
            """,
            (campaign_id,),
        )
        last_upload_at = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO campaign_kpis (
                campaign_id, total_leads, stage_counts,
                manager_intervention_count, last_upload_at, updated_at
            )
            VALUES (%s, %s, %s::jsonb, %s, %s, NOW())
            ON CONFLICT (campaign_id) DO UPDATE SET
                total_leads                = EXCLUDED.total_leads,
                stage_counts               = EXCLUDED.stage_counts,
                manager_intervention_count = EXCLUDED.manager_intervention_count,
                last_upload_at             = EXCLUDED.last_upload_at,
                updated_at                 = NOW()
            """,
            (
                campaign_id,
                total_leads,
                _json_dumps_safe(stage_counts),
                intervention_count,
                last_upload_at,
            ),
        )
    conn.commit()
    return {
        "campaign_id": campaign_id,
        "total_leads": total_leads,
        "stage_counts": stage_counts,
        "manager_intervention_count": intervention_count,
        "last_upload_at": last_upload_at,
    }


# ═══ Recalc — Manager Intervention ══════════════════════════════════════
#
# Spec rules (these are the only triggers, do not add to them):
#
#   latest stage = NO_ANSWER
#     AND timeline contains MEETING earlier   → AFTER_MEETING   (HIGH)
#     ELSE timeline contains FOLLOWING earlier → AFTER_FOLLOWING (MEDIUM)
#     ELSE                                     → no flag
#
# Anything else (latest = NO_ANSWER from first contact, latest = MEETING,
# latest = CANCELLATION, rep change only, ...) → the recalc DELETEs any
# stale flag for that lead.
#
# Existing flag preservation:
#   - status = REVIEWED/CLOSED + trigger unchanged → only update descriptive
#     fields; leave status alone (the manager already touched it).
#   - status = REVIEWED/CLOSED + trigger changed → reset to OPEN and clear
#     reviewed_by/reviewed_at — the situation has materially shifted, so
#     the previous decision shouldn't carry forward.

def recalc_manager_intervention(campaign_id: int, conn) -> int:
    """Recompute the manager_intervention_flags rows for this campaign.
    Returns the number of OPEN flags after recalc."""
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Pull every event for the campaign in one query, ordered so the
        # Python pass can iterate per-lead with a simple groupby pattern.
        # We need ALL events (not just the latest) so we can scan for a
        # prior MEETING / FOLLOWING.
        cur.execute(
            """
            SELECT le.id, le.lead_id, le.normalized_stage, le.follow_date,
                   le.comment, le.sales_user_id
            FROM lead_events le
            JOIN leads l ON l.id = le.lead_id
            WHERE l.campaign_id = %s
              AND le.normalized_stage IS NOT NULL
              AND le.is_voided = FALSE
            ORDER BY le.lead_id, le.follow_date ASC NULLS LAST, le.id ASC
            """,
            (campaign_id,),
        )
        all_events = cur.fetchall()

        # Group events by lead_id. dict-of-lists is fine — sheets in the
        # tens of thousands of events still fit comfortably.
        by_lead: dict = {}
        for ev in all_events:
            by_lead.setdefault(ev["lead_id"], []).append(ev)

        # Existing flags so we can do preservation logic without a per-lead
        # SELECT inside the loop.
        cur.execute(
            """
            SELECT lead_id, trigger_type, status
            FROM manager_intervention_flags
            WHERE campaign_id = %s
            """,
            (campaign_id,),
        )
        existing_flags = {row["lead_id"]: row for row in cur.fetchall()}

        keep_lead_ids: set = set()

        for lead_id, events in by_lead.items():
            verdict = _classify_lead_intervention(events)
            if verdict is None:
                continue
            keep_lead_ids.add(lead_id)

            trigger = verdict["trigger"]
            existing = existing_flags.get(lead_id)

            if existing and existing["status"] in (STATUS_REVIEWED, STATUS_CLOSED):
                if existing["trigger_type"] == trigger:
                    # Same trigger, manager already actioned it — update
                    # descriptive fields only. Don't reset status.
                    cur.execute(
                        """
                        UPDATE manager_intervention_flags SET
                            current_stage            = %s,
                            previous_positive_stage  = %s,
                            priority                 = %s,
                            last_positive_stage_date = %s,
                            last_no_answer_date      = %s,
                            last_comment             = %s,
                            sales_user_id            = %s,
                            updated_at               = NOW()
                        WHERE lead_id = %s
                        """,
                        (
                            verdict["current_stage"],
                            verdict["previous_positive_stage"],
                            verdict["priority"],
                            verdict["last_positive_stage_date"],
                            verdict["last_no_answer_date"],
                            verdict["last_comment"],
                            verdict["sales_user_id"],
                            lead_id,
                        ),
                    )
                    continue
                # Trigger flipped (e.g. AFTER_FOLLOWING → AFTER_MEETING).
                # Material change → reopen so the manager re-evaluates.
                # Falls through to the upsert below with status=OPEN.

            cur.execute(
                """
                INSERT INTO manager_intervention_flags (
                    lead_id, campaign_id, sales_user_id, trigger_type,
                    current_stage, previous_positive_stage, priority,
                    last_positive_stage_date, last_no_answer_date,
                    last_comment, status, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (lead_id) DO UPDATE SET
                    campaign_id              = EXCLUDED.campaign_id,
                    sales_user_id            = EXCLUDED.sales_user_id,
                    trigger_type             = EXCLUDED.trigger_type,
                    current_stage            = EXCLUDED.current_stage,
                    previous_positive_stage  = EXCLUDED.previous_positive_stage,
                    priority                 = EXCLUDED.priority,
                    last_positive_stage_date = EXCLUDED.last_positive_stage_date,
                    last_no_answer_date      = EXCLUDED.last_no_answer_date,
                    last_comment             = EXCLUDED.last_comment,
                    status                   = %s,
                    reviewed_by              = NULL,
                    reviewed_at              = NULL,
                    updated_at               = NOW()
                """,
                (
                    lead_id, campaign_id, verdict["sales_user_id"], trigger,
                    verdict["current_stage"], verdict["previous_positive_stage"],
                    verdict["priority"],
                    verdict["last_positive_stage_date"], verdict["last_no_answer_date"],
                    verdict["last_comment"],
                    STATUS_OPEN,
                    # Second STATUS_OPEN — the DO UPDATE branch needs it
                    # via the literal, not EXCLUDED.status (which would
                    # mirror the INSERT value but read less clearly).
                    STATUS_OPEN,
                ),
            )

        # DELETE flags for leads in this campaign that no longer qualify.
        # We narrow by campaign_id so we never touch other campaigns' rows.
        if keep_lead_ids:
            cur.execute(
                """
                DELETE FROM manager_intervention_flags
                WHERE campaign_id = %s AND lead_id <> ALL(%s)
                """,
                (campaign_id, list(keep_lead_ids)),
            )
        else:
            cur.execute(
                "DELETE FROM manager_intervention_flags WHERE campaign_id = %s",
                (campaign_id,),
            )

        cur.execute(
            "SELECT COUNT(*) AS n FROM manager_intervention_flags "
            "WHERE campaign_id = %s AND status = %s",
            (campaign_id, STATUS_OPEN),
        )
        open_count = cur.fetchone()["n"]

    conn.commit()
    return open_count


def _classify_lead_intervention(events):
    """Apply the trigger rules to one lead's ordered events. Returns the
    flag fields if the lead qualifies, else None.

    Events come in ASC by (follow_date, id) so events[-1] is the latest.
    """
    if not events:
        return None
    latest = events[-1]
    if latest["normalized_stage"] != "NO_ANSWER":
        return None

    # Scan history before the latest for any positive stage. We walk in
    # reverse so the FIRST hit is the MOST RECENT positive stage — that
    # date goes into last_positive_stage_date.
    previous = events[:-1]
    last_meeting = None
    last_following = None
    for ev in reversed(previous):
        stage = ev["normalized_stage"]
        if stage == "MEETING" and last_meeting is None:
            last_meeting = ev
        elif stage == "FOLLOWING" and last_following is None:
            last_following = ev
        if last_meeting is not None and last_following is not None:
            break

    if last_meeting is not None:
        positive = last_meeting
        positive_stage = "MEETING"
        trigger = TRIGGER_NO_ANSWER_AFTER_MEETING
        priority = PRIORITY_HIGH
    elif last_following is not None:
        positive = last_following
        positive_stage = "FOLLOWING"
        trigger = TRIGGER_NO_ANSWER_AFTER_FOLLOWING
        priority = PRIORITY_MEDIUM
    else:
        return None

    return {
        "trigger": trigger,
        "priority": priority,
        "current_stage": "NO_ANSWER",
        "previous_positive_stage": positive_stage,
        "last_positive_stage_date": positive["follow_date"],
        "last_no_answer_date": latest["follow_date"],
        "last_comment": latest["comment"],
        "sales_user_id": latest["sales_user_id"],
    }


# ═══ Recalc — Assignments (Fresh vs Rotation) ═══════════════════════════
#
# An "assignment" is a half-open time window during which one sales rep
# owned a lead. We rebuild them from scratch on every recalc — there's no
# UPDATE path. Reasons:
#   - Idempotency: re-uploading the same events MUST produce the same
#     assignments, even if the previous upload was partial.
#   - Simplicity: incremental "diff" rebuild has way more edge cases than
#     it's worth (mid-stream rep insert, event delete via void, ...).
#
# The pure function _assignments_from_events takes an ASC-sorted event
# list and returns the assignment dicts WITHOUT touching the DB — it's the
# only piece we unit-test. rebuild_lead_assignments wraps it with the
# DELETE + INSERT bookkeeping.

ASSIGNMENT_TYPE_FRESH    = "FRESH"
ASSIGNMENT_TYPE_ROTATION = "ROTATION"


def _rep_key(ev):
    """Build a comparable key for "is this the same rep as the previous event?".

    Both matched → compare by sales_user_id.
    Both unmatched → compare by normalized raw name.
    Mixed (one has a user_id, the other doesn't) → never equal.
    Both fully blank (no user, no name) → returns None, signalling the
    caller to SKIP this event (don't break the current assignment).
    """
    uid = ev.get("sales_user_id")
    raw = ev.get("raw_sales_rep_name")
    if uid is not None:
        # ("u", 7) — distinct namespace from ("r", "name") so a future raw
        # name that happens to equal a user_id integer can't collide.
        return ("u", uid)
    norm = normalize_sales_name(raw or "")
    if not norm:
        return None
    return ("r", norm)


def _assignments_from_events(events):
    """Pure function: ordered events → ordered list of assignment dicts.

    Each event dict needs: follow_date, sales_user_id, raw_sales_rep_name.
    Events without any rep info are passed through (they don't open or close
    assignments). The first assignment is always FRESH; every subsequent
    rep change is ROTATION, even if it's a rep returning after someone else.

    Returns: [{sales_user_id, raw_sales_rep_name, assignment_type,
               started_at, ended_at}, ...]
    """
    assignments = []
    current = None
    current_key = None

    for ev in events:
        key = _rep_key(ev)
        if key is None:
            # No rep info — skip without breaking the current assignment.
            continue
        if current is None:
            # First rep-bearing event opens the FRESH window.
            current = {
                "sales_user_id": ev.get("sales_user_id"),
                "raw_sales_rep_name": ev.get("raw_sales_rep_name"),
                "assignment_type": ASSIGNMENT_TYPE_FRESH,
                "started_at": ev["follow_date"],
                "ended_at": None,
            }
            current_key = key
            assignments.append(current)
            continue
        if key == current_key:
            # Same rep continuing — nothing to record.
            continue
        # Rep flipped → close current at THIS event's date (half-open
        # boundary: [started_at, ended_at)) and open ROTATION for the new
        # rep.
        current["ended_at"] = ev["follow_date"]
        current = {
            "sales_user_id": ev.get("sales_user_id"),
            "raw_sales_rep_name": ev.get("raw_sales_rep_name"),
            "assignment_type": ASSIGNMENT_TYPE_ROTATION,
            "started_at": ev["follow_date"],
            "ended_at": None,
        }
        current_key = key
        assignments.append(current)

    return assignments


def rebuild_lead_assignments(lead_id: int, conn) -> int:
    """Recompute all assignments for one lead. Returns count written."""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, follow_date, sales_user_id, raw_sales_rep_name, campaign_id
            FROM lead_events
            WHERE lead_id = %s
              AND is_voided = FALSE
              AND follow_date IS NOT NULL
            ORDER BY follow_date ASC, id ASC
            """,
            (lead_id,),
        )
        events = cur.fetchall()
        if not events:
            cur.execute("DELETE FROM lead_assignments WHERE lead_id = %s", (lead_id,))
            return 0

        campaign_id = events[0]["campaign_id"]
        assignments = _assignments_from_events(events)

        # Wipe-and-write keeps the logic obvious; the alternative (diff &
        # patch) is fiddly and the event-counts we're walking are small.
        cur.execute("DELETE FROM lead_assignments WHERE lead_id = %s", (lead_id,))
        if not assignments:
            return 0

        rows = [
            (
                lead_id, campaign_id,
                a["sales_user_id"], a["raw_sales_rep_name"], a["assignment_type"],
                a["started_at"], a["ended_at"],
            )
            for a in assignments
        ]
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO lead_assignments
                (lead_id, campaign_id, sales_user_id, raw_sales_rep_name,
                 assignment_type, started_at, ended_at)
            VALUES %s
            """,
            rows,
        )
    return len(assignments)


def rebuild_assignments_for_campaign(campaign_id: int, conn,
                                     affected_lead_ids=None) -> int:
    """Rebuild assignments for every lead in the campaign (or only the
    provided subset). Returns total assignments written."""
    with conn.cursor() as cur:
        if affected_lead_ids is None:
            cur.execute("SELECT id FROM leads WHERE campaign_id = %s", (campaign_id,))
            lead_ids = [r[0] for r in cur.fetchall()]
        else:
            lead_ids = list(affected_lead_ids)

    total = 0
    for lid in lead_ids:
        total += rebuild_lead_assignments(lid, conn)
    conn.commit()
    return total


# ═══ Recalc — Sales KPIs ════════════════════════════════════════════════
#
# For each rep with at least one assignment in the campaign:
#   fresh_leads_count    = distinct leads where rep has a FRESH assignment
#   rotation_leads_count = distinct leads where rep has any ROTATION assignment
#   fresh_outcomes       = histogram of the LATEST event's normalized_stage,
#                          counted across each FRESH assignment window
#   rotation_outcomes    = same but for ROTATION windows
#
# "Latest event in window" is per-assignment, not per-lead. If a rep had
# the same lead twice (FRESH, then again as ROTATION later), both windows
# contribute their own latest-stage to the rep's totals. The window is
# half-open [started_at, ended_at) — ended_at=NULL means open-ended.

def recalc_sales_kpis(campaign_id: int, conn) -> int:
    """Upsert one sales_kpis row per matched rep in the campaign; delete
    rows for reps who no longer have any assignments. Returns rep count."""
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # The window-join + DISTINCT ON(assignment) gives us "latest valid
        # event per assignment" in a single pass — much cheaper than a
        # Python loop with per-assignment queries.
        cur.execute(
            """
            WITH window_latest AS (
                SELECT DISTINCT ON (a.id)
                       a.id            AS assignment_id,
                       a.lead_id,
                       a.sales_user_id,
                       a.assignment_type,
                       le.normalized_stage
                FROM lead_assignments a
                JOIN lead_events le ON le.lead_id = a.lead_id
                WHERE a.campaign_id = %s
                  AND a.sales_user_id IS NOT NULL
                  AND le.is_voided = FALSE
                  AND le.normalized_stage IS NOT NULL
                  AND le.follow_date >= a.started_at
                  AND (a.ended_at IS NULL OR le.follow_date < a.ended_at)
                ORDER BY a.id, le.follow_date DESC NULLS LAST, le.id DESC
            )
            SELECT sales_user_id, assignment_type, normalized_stage, COUNT(*) AS n
            FROM window_latest
            GROUP BY sales_user_id, assignment_type, normalized_stage
            """,
            (campaign_id,),
        )
        outcome_rows = cur.fetchall()

        # Lead counts come from the assignments table directly — a rep can
        # own a lead without that lead having a valid event in the window
        # yet, and we still want to count them.
        cur.execute(
            """
            SELECT sales_user_id, assignment_type, COUNT(DISTINCT lead_id) AS n
            FROM lead_assignments
            WHERE campaign_id = %s AND sales_user_id IS NOT NULL
            GROUP BY sales_user_id, assignment_type
            """,
            (campaign_id,),
        )
        count_rows = cur.fetchall()

    # Pivot: user_id → {fresh_count, rotation_count, fresh_outcomes,
    #                   rotation_outcomes}.
    per_user: dict = {}
    for r in count_rows:
        uid = r["sales_user_id"]
        bucket = per_user.setdefault(uid, {
            "fresh_leads_count": 0,
            "rotation_leads_count": 0,
            "fresh_outcomes": {},
            "rotation_outcomes": {},
        })
        if r["assignment_type"] == ASSIGNMENT_TYPE_FRESH:
            bucket["fresh_leads_count"] = r["n"]
        else:
            bucket["rotation_leads_count"] = r["n"]

    for r in outcome_rows:
        uid = r["sales_user_id"]
        bucket = per_user.setdefault(uid, {
            "fresh_leads_count": 0,
            "rotation_leads_count": 0,
            "fresh_outcomes": {},
            "rotation_outcomes": {},
        })
        target = ("fresh_outcomes" if r["assignment_type"] == ASSIGNMENT_TYPE_FRESH
                  else "rotation_outcomes")
        bucket[target][r["normalized_stage"]] = r["n"]

    with conn.cursor() as cur:
        if per_user:
            payload = [
                (
                    campaign_id, uid,
                    b["fresh_leads_count"], b["rotation_leads_count"],
                    _json_dumps_safe(b["fresh_outcomes"]),
                    _json_dumps_safe(b["rotation_outcomes"]),
                )
                for uid, b in per_user.items()
            ]
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO sales_kpis
                    (campaign_id, sales_user_id, fresh_leads_count,
                     rotation_leads_count, fresh_outcomes, rotation_outcomes)
                VALUES %s
                ON CONFLICT (campaign_id, sales_user_id) DO UPDATE SET
                    fresh_leads_count    = EXCLUDED.fresh_leads_count,
                    rotation_leads_count = EXCLUDED.rotation_leads_count,
                    fresh_outcomes       = EXCLUDED.fresh_outcomes,
                    rotation_outcomes    = EXCLUDED.rotation_outcomes,
                    updated_at           = NOW()
                """,
                payload,
                template="(%s, %s, %s, %s, %s::jsonb, %s::jsonb)",
            )
            cur.execute(
                """
                DELETE FROM sales_kpis
                WHERE campaign_id = %s
                  AND sales_user_id <> ALL(%s)
                """,
                (campaign_id, list(per_user.keys())),
            )
        else:
            cur.execute(
                "DELETE FROM sales_kpis WHERE campaign_id = %s",
                (campaign_id,),
            )
    conn.commit()
    return len(per_user)


# ═══ Lead Timeline enrichment (P3) ══════════════════════════════════════
#
# enrich_timeline_events is a pure function the timeline endpoint uses to
# compute per-event `risk` and `is_transfer` flags from the same ordered
# event list the lead's events came in as. The DB doesn't store these —
# they're a function of the surrounding history, so we recompute on read.
#
# Risk tokens map (frontend resolves to localized labels):
#   "red"    — NO_ANSWER after MEETING was seen earlier (urgent)
#   "orange" — NO_ANSWER after FOLLOWING was seen earlier (follow-up)
#   "yellow" — NO_ANSWER from first contact / repeated, no positive earlier
#   None     — non-NO_ANSWER event, no risk to surface

RISK_RED    = "red"
RISK_ORANGE = "orange"
RISK_YELLOW = "yellow"

# Maps trigger token → frontend i18n key for the recommendation copy. The
# server returns the trigger; the client looks the localized line up so
# the API stays language-agnostic.
RECOMMENDATION_KEYS = {
    TRIGGER_NO_ANSWER_AFTER_MEETING:   "crm.intervention.recommendation.meeting",
    TRIGGER_NO_ANSWER_AFTER_FOLLOWING: "crm.intervention.recommendation.following",
}


def _events_have_same_rep(a, b):
    """True iff event `a` and event `b` are attributed to the same rep.
    Matches the assignment-builder rep_key rules (P2)."""
    if a is None or b is None:
        return False
    a_uid, b_uid = a.get("sales_user_id"), b.get("sales_user_id")
    if a_uid is not None and b_uid is not None:
        return a_uid == b_uid
    if a_uid is None and b_uid is None:
        an = normalize_sales_name(a.get("raw_sales_rep_name") or "")
        bn = normalize_sales_name(b.get("raw_sales_rep_name") or "")
        # Both fully blank → don't claim "same rep" — caller treats as None.
        if not an or not bn:
            return False
        return an == bn
    # Mixed (one matched, one not) → different reps.
    return False


def enrich_timeline_events(events):
    """Compute `risk` + `is_transfer` for each event in an ASC-ordered list.

    Each input event needs at minimum: normalized_stage, sales_user_id,
    raw_sales_rep_name. The function returns a NEW list of shallow copies
    with `risk` and `is_transfer` added — never mutates the input.

    Walking once is enough: we keep two cheap booleans (seen_following /
    seen_meeting) and update them AFTER computing the current event's
    risk, so a NO_ANSWER doesn't trigger off its own positive context.
    The `is_transfer` flag compares against the previous rep-bearing
    event (events with no rep at all are skipped, mirroring the
    assignment builder).
    """
    out = []
    seen_following = False
    seen_meeting   = False
    prev_with_rep  = None  # last event that actually had a rep attribution

    for ev in events:
        stage = ev.get("normalized_stage")

        if stage == "NO_ANSWER":
            if seen_meeting:
                risk = RISK_RED
            elif seen_following:
                risk = RISK_ORANGE
            else:
                risk = RISK_YELLOW
        else:
            risk = None

        # is_transfer: rep flipped from the previous rep-bearing event.
        # First rep-bearing event is never a transfer (no previous rep).
        has_rep = (ev.get("sales_user_id") is not None
                   or bool((ev.get("raw_sales_rep_name") or "").strip()))
        if has_rep and prev_with_rep is not None:
            is_transfer = not _events_have_same_rep(prev_with_rep, ev)
        else:
            is_transfer = False

        new_ev = dict(ev)
        new_ev["risk"] = risk
        new_ev["is_transfer"] = is_transfer
        out.append(new_ev)

        # Update lookahead trackers AFTER risk computation so a positive
        # stage doesn't self-trigger if it ever became NO_ANSWER inline.
        if stage == "MEETING":
            seen_meeting = True
        elif stage == "FOLLOWING":
            seen_following = True
        if has_rep:
            prev_with_rep = ev

    return out


def build_lead_timeline(lead_id: int, conn) -> dict:
    """Assemble the per-lead timeline payload the page renders.

    Pulls: lead row, campaign name, ordered events with assignment_type
    joined from lead_assignments (via the window join), and any active
    intervention flag. Computes risk + is_transfer in Python via
    enrich_timeline_events.

    Returns None if the lead doesn't exist; the endpoint maps that to 404.
    """
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT l.id AS lead_id, l.client_name, l.mobile,
                   l.campaign_id, c.campaign_name
            FROM leads l
            JOIN marketing_campaigns c ON c.id = l.campaign_id
            WHERE l.id = %s
            """,
            (lead_id,),
        )
        lead = cur.fetchone()
        if not lead:
            return None

        # Events + assignment_type via the half-open window join. NULL
        # assignment_type means the event landed outside any window —
        # in practice this is the no-rep ghost rows the parser kept.
        cur.execute(
            """
            SELECT e.id AS event_id, e.follow_date, e.raw_stage, e.normalized_stage,
                   e.comment, e.sales_user_id, e.raw_sales_rep_name,
                   u.full_name AS sales_rep_name, u.avatar_url,
                   a.assignment_type
            FROM lead_events e
            LEFT JOIN users u ON u.id = e.sales_user_id
            LEFT JOIN lead_assignments a
                   ON a.lead_id = e.lead_id
                  AND e.follow_date >= a.started_at
                  AND (a.ended_at IS NULL OR e.follow_date < a.ended_at)
            WHERE e.lead_id = %s AND e.is_voided = FALSE
            ORDER BY e.follow_date ASC NULLS LAST, e.id ASC
            """,
            (lead_id,),
        )
        events = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT m.id, m.trigger_type, m.priority, m.status,
                   m.previous_positive_stage, m.last_positive_stage_date,
                   m.last_no_answer_date, m.last_comment,
                   m.reviewed_by, m.reviewed_at,
                   ru.full_name AS reviewed_by_name
            FROM manager_intervention_flags m
            LEFT JOIN users ru ON ru.id = m.reviewed_by
            WHERE m.lead_id = %s
            """,
            (lead_id,),
        )
        flag = cur.fetchone()

    enriched = enrich_timeline_events(events)

    # latest_stage + current_sales_rep come from the last event with a
    # resolvable stage / rep. Events with NULL stage are skipped for the
    # "latest stage" but kept in the timeline so the user sees the raw row.
    latest_stage = None
    current_rep_id = None
    current_rep_name = None
    for ev in reversed(events):
        if latest_stage is None and ev.get("normalized_stage"):
            latest_stage = ev["normalized_stage"]
        if current_rep_id is None and ev.get("sales_user_id") is not None:
            current_rep_id = ev["sales_user_id"]
            current_rep_name = ev.get("sales_rep_name")
        if latest_stage and current_rep_id is not None:
            break

    intervention = None
    if flag:
        intervention = {
            "id": flag["id"],
            "trigger": flag["trigger_type"],
            "priority": flag["priority"],
            "status": flag["status"],
            "previous_positive_stage": flag["previous_positive_stage"],
            "last_positive_stage_date": (
                flag["last_positive_stage_date"].isoformat()
                if flag["last_positive_stage_date"] else None
            ),
            "last_no_answer_date": (
                flag["last_no_answer_date"].isoformat()
                if flag["last_no_answer_date"] else None
            ),
            "last_comment": flag["last_comment"],
            "recommendation_key": RECOMMENDATION_KEYS.get(flag["trigger_type"]),
            "reviewed_by": flag["reviewed_by"],
            "reviewed_by_name": flag["reviewed_by_name"],
            "reviewed_at": (
                flag["reviewed_at"].isoformat() if flag["reviewed_at"] else None
            ),
        }

    timeline_out = []
    for ev in enriched:
        timeline_out.append({
            "event_id": ev["event_id"],
            "date": ev["follow_date"].isoformat() if ev.get("follow_date") else None,
            "sales_rep": ev.get("sales_rep_name") or ev.get("raw_sales_rep_name"),
            "sales_user_id": ev.get("sales_user_id"),
            "avatar_url": ev.get("avatar_url"),
            "assignment_type": ev.get("assignment_type"),
            "raw_stage": ev.get("raw_stage"),
            "stage": ev.get("normalized_stage"),
            "comment": ev.get("comment"),
            "is_transfer": ev["is_transfer"],
            "risk": ev["risk"],
        })

    return {
        "lead_id": lead["lead_id"],
        "client_name": lead["client_name"],
        "mobile": lead["mobile"],
        "campaign_id": lead["campaign_id"],
        "campaign_name": lead["campaign_name"],
        "current_sales_rep": current_rep_name,
        "current_sales_user_id": current_rep_id,
        "latest_stage": latest_stage,
        "intervention": intervention,
        "timeline": timeline_out,
    }


# ═══ Aggregator ═════════════════════════════════════════════════════════

def recalc_after_upload(campaign_id: int, conn) -> dict:
    """Called by the background upload thread after lead_events are written.

    Pipeline order (P2):
      1. rebuild_assignments_for_campaign — Fresh/Rotation windows live here
         and feed (3) below.
      2. recalc_campaign_kpis — total_leads + stage_counts. NOTE this also
         writes manager_intervention_count, but the value will be stale
         since intervention runs in step (4). We resync it at the end.
      3. recalc_sales_kpis — per-rep rollups, needs assignments from (1).
      4. recalc_manager_intervention — the strict-rule flag set; returns
         the new OPEN count which we mirror into campaign_kpis to fix the
         staleness from step (2).
    """
    rebuild_assignments_for_campaign(campaign_id, conn)
    kpis = recalc_campaign_kpis(campaign_id, conn)
    rep_count = recalc_sales_kpis(campaign_id, conn)
    open_count = recalc_manager_intervention(campaign_id, conn)

    # Sync intervention count into campaign_kpis. One-row UPDATE so we
    # never touch total_leads / stage_counts here.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE campaign_kpis
            SET manager_intervention_count = %s,
                updated_at = NOW()
            WHERE campaign_id = %s
            """,
            (open_count, campaign_id),
        )
    conn.commit()

    kpis["manager_intervention_count"] = open_count
    return {
        "intervention_open": open_count,
        "sales_reps_with_kpis": rep_count,
        "kpis": kpis,
    }


# ─── Local helpers ──────────────────────────────────────────────────────

def _json_dumps_safe(obj) -> str:
    """Stable JSON for jsonb columns — sorted keys, ASCII-safe defaults.
    Defined inline so the module's only third-party-ish import stays
    psycopg2 (used elsewhere)."""
    import json
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


# ═══ Reporting helpers (P4) ═════════════════════════════════════════════

def _response_rate_pct(total: int, no_answer: int) -> float:
    """Percent of attempts that landed on something other than NO_ANSWER.

    `total` is the count of events (or unique leads, depending on caller)
    — both use the same shape. We round to one decimal so the value
    survives a JSON round-trip without ".0000000004"-style drift.

    Edge case: total=0 → 0.0 (not NaN — caller renders "—" if it wants
    to distinguish "no data" from "0%"). negatives clamp to 0 since they
    can't happen in well-formed input but a stray bug shouldn't produce
    a 700% rate either.
    """
    if total is None or total <= 0:
        return 0.0
    answered = max(0, total - max(0, no_answer or 0))
    return round((answered / total) * 100.0, 1)


# ═══ Retroactive mapping application (P4) ═══════════════════════════════
#
# When an admin adds or removes a stage/sales-rep mapping, the historical
# events in lead_events keep the value they were ingested with. These
# helpers re-derive that value against the current mappings table and
# UPDATE the affected rows in place, returning the campaign_ids that
# need a full recalc afterwards.
#
# Design notes:
#   - The helpers don't know whether they were called for an ADD or a
#     DELETE. They just re-derive — which is correct in both directions
#     because normalize_stage / match_sales_user read the live mappings
#     table.
#   - For stage: we update normalized_stage. is_voided rows are skipped.
#   - For sales-rep: we update sales_user_id. Skipping is_voided too.
#   - Callers are responsible for committing the mapping row change
#     BEFORE calling these (so the live re-derivation sees the new
#     state) and for running recalc_after_upload per returned campaign.


def apply_stage_mapping_change(raw_stage: str, campaign_id_scope, conn) -> set:
    """Re-derive normalized_stage for every event whose raw_stage matches.

    `campaign_id_scope`:
      - integer → only events in that campaign
      - None    → global change; updates events across every campaign
                  (per-campaign mappings still win in the re-derivation
                  because normalize_stage uses the proper lookup order)

    Returns a set of affected campaign_ids. Empty set means no events
    matched — caller can skip the recalc loop entirely.
    """
    if raw_stage is None:
        return set()
    raw_key = str(raw_stage).strip().lower()
    if not raw_key:
        return set()

    affected: set = set()
    with conn.cursor() as cur:
        # SELECT first so we can recompute per-event in Python — the
        # mapping lookup is 3-tier (per-campaign → global → default) and
        # encoding that in a single UPDATE statement would be brittle.
        if campaign_id_scope is not None:
            cur.execute(
                """
                SELECT id, campaign_id, raw_stage
                FROM lead_events
                WHERE LOWER(TRIM(raw_stage)) = %s
                  AND campaign_id = %s
                  AND is_voided = FALSE
                """,
                (raw_key, campaign_id_scope),
            )
        else:
            cur.execute(
                """
                SELECT id, campaign_id, raw_stage
                FROM lead_events
                WHERE LOWER(TRIM(raw_stage)) = %s
                  AND is_voided = FALSE
                """,
                (raw_key,),
            )
        rows = cur.fetchall()

    for event_id, campaign_id, original_raw in rows:
        new_stage = normalize_stage(original_raw, campaign_id=campaign_id, conn=conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE lead_events SET normalized_stage = %s WHERE id = %s",
                (new_stage, event_id),
            )
        affected.add(campaign_id)

    conn.commit()
    return affected


def apply_sales_rep_mapping_change(raw_name: str, campaign_id_scope, conn) -> set:
    """Re-derive sales_user_id for every event whose raw_sales_rep_name
    normalizes to this raw_name. Mirrors apply_stage_mapping_change but
    keyed on the normalized name.

    Scope: an integer campaign_id (per-campaign change) or None (global).

    Returns the set of affected campaign_ids. Whether those campaigns'
    KPIs/intervention/assignments actually change depends on the recalc
    chain the caller runs afterwards.
    """
    if raw_name is None:
        return set()
    norm = normalize_sales_name(raw_name)
    if not norm:
        return set()

    affected: set = set()
    with conn.cursor() as cur:
        # Match events by NORMALIZED raw_name, not exact string — keeps
        # "Rana Hany" and "rana  hany" together (same as the parser).
        if campaign_id_scope is not None:
            cur.execute(
                """
                SELECT id, campaign_id, raw_sales_rep_name
                FROM lead_events
                WHERE REGEXP_REPLACE(LOWER(TRIM(raw_sales_rep_name)), '\\s+', ' ', 'g') = %s
                  AND raw_sales_rep_name IS NOT NULL
                  AND raw_sales_rep_name <> ''
                  AND campaign_id = %s
                  AND is_voided = FALSE
                """,
                (norm, campaign_id_scope),
            )
        else:
            cur.execute(
                """
                SELECT id, campaign_id, raw_sales_rep_name
                FROM lead_events
                WHERE REGEXP_REPLACE(LOWER(TRIM(raw_sales_rep_name)), '\\s+', ' ', 'g') = %s
                  AND raw_sales_rep_name IS NOT NULL
                  AND raw_sales_rep_name <> ''
                  AND is_voided = FALSE
                """,
                (norm,),
            )
        rows = cur.fetchall()

    for event_id, campaign_id, original_raw in rows:
        new_user_id = match_sales_user(original_raw, campaign_id, conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE lead_events SET sales_user_id = %s WHERE id = %s",
                (new_user_id, event_id),
            )
        affected.add(campaign_id)

    conn.commit()
    return affected
