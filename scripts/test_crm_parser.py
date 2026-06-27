"""
Smoke tests for the CRM upload pipeline (P1a).

Covers what's runnable WITHOUT touching the real database:
  - normalize_mobile     — every Egyptian/Gulf shape we promised to handle
  - normalize_stage      — DEFAULT_STAGE_MAP path (conn=None)
  - normalize_sales_name — whitespace/case collapsing
  - compute_event_hash   — stable across runs, varies on every component
  - parse_crm_excel      — forward-fill on Client name / Mobile, header
                           aliases, unmatched rep collection, comment
                           passthrough
  - dedup via event_hash — same row twice → same hash

The parser smoke test feeds in a FakeConn so we don't need PostgreSQL
running locally. The blueprint, the background thread, and the live INSERTs
into lead_events live in app/crm_processor.py — those need an actual DB
and are smoke-tested with `curl` once the server is up.

Run: PYTHONIOENCODING=utf-8 DISABLE_SYNC=true python scripts/test_crm_parser.py
"""
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import Workbook  # noqa: E402

from app.crm_logic import (  # noqa: E402
    compute_event_hash,
    normalize_mobile,
    normalize_sales_name,
    normalize_stage,
    _classify_lead_intervention,
    _assignments_from_events,
    _response_rate_pct,
    enrich_timeline_events,
    ASSIGNMENT_TYPE_FRESH,
    ASSIGNMENT_TYPE_ROTATION,
    RISK_RED,
    RISK_ORANGE,
    RISK_YELLOW,
    TRIGGER_NO_ANSWER_AFTER_FOLLOWING,
    TRIGGER_NO_ANSWER_AFTER_MEETING,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
)
from app.crm_parser import parse_crm_excel  # noqa: E402


# ─── Tiny harness ───────────────────────────────────────────────────────

_failures = 0


def _check(name, ok, detail=""):
    global _failures
    if ok:
        print(f"  ok   {name}")
    else:
        _failures += 1
        print(f"  FAIL {name}: {detail}")


# ─── FakeConn — satisfies the bits parse_crm_excel needs ───────────────
#
# normalize_stage and match_sales_user both call `conn.cursor()` as a
# context manager and run a SELECT. We return zero rows for every query
# so the helpers fall back to DEFAULT_STAGE_MAP / users-table-not-found
# (also empty under FakeConn). That's enough to exercise the parser end-
# to-end without standing up Postgres.

class _FakeCursor:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *_args, **_kwargs): return None
    def fetchone(self): return None
    def fetchall(self): return []


class _FakeConn:
    def cursor(self, *a, **kw): return _FakeCursor()


# ─── Mobile normalization ───────────────────────────────────────────────

def test_normalize_mobile():
    print("─── normalize_mobile ───")
    _check("01012345678 → 201012345678", normalize_mobile("01012345678") == "201012345678")
    _check("+20 100 123 4567 → 201001234567",
           normalize_mobile("+20 100 123 4567") == "201001234567")
    _check("00201012345678 → 201012345678",
           normalize_mobile("00201012345678") == "201012345678")
    _check("+971569116811 → 971569116811",
           normalize_mobile("+971569116811") == "971569116811")
    _check("with dashes → digits",
           normalize_mobile("010-1234-5678") == "201012345678")
    _check("with parens → digits",
           normalize_mobile("(010) 1234 5678") == "201012345678")
    _check("10-digit '1...' → prepend 20",
           normalize_mobile("1012345678") == "201012345678")
    _check("None → None", normalize_mobile(None) is None)
    _check("empty → None", normalize_mobile("") is None)
    _check("whitespace → None", normalize_mobile("   ") is None)
    _check("letters → None", normalize_mobile("abcd") is None)
    _check("too short → None", normalize_mobile("1234567") is None)
    # openpyxl returns an int when Excel formatted the cell as a number —
    # the int form must reach the same canonical output.
    _check("int input round-trips",
           normalize_mobile(201012345678) == "201012345678")
    _check("float input round-trips",
           normalize_mobile(201012345678.0) == "201012345678")


# ─── Stage normalization (default map only) ────────────────────────────

def test_normalize_stage():
    print("─── normalize_stage (DEFAULT_STAGE_MAP, conn=None) ───")
    _check("No Answer → NO_ANSWER", normalize_stage("No Answer") == "NO_ANSWER")
    _check("  Following → FOLLOWING (trim)",
           normalize_stage("  Following  ") == "FOLLOWING")
    _check("Zoom Meeting → MEETING", normalize_stage("Zoom Meeting") == "MEETING")
    _check("Meeting Done → MEETING", normalize_stage("Meeting Done") == "MEETING")
    _check("Cancelled → CANCELLATION", normalize_stage("Cancelled") == "CANCELLATION")
    _check("Canceled  → CANCELLATION (US spelling)",
           normalize_stage("Canceled") == "CANCELLATION")
    _check("Interested → INTERESTED", normalize_stage("Interested") == "INTERESTED")
    _check("unknown → None", normalize_stage("Discounted via Zoom") is None)
    _check("None → None", normalize_stage(None) is None)
    _check("empty → None", normalize_stage("") is None)


# ─── Sales name normalization ───────────────────────────────────────────

def test_normalize_sales_name():
    print("─── normalize_sales_name ───")
    _check("trim + lower", normalize_sales_name("  Mahmoud Amr  ") == "mahmoud amr")
    _check("collapse multi spaces",
           normalize_sales_name("Mahmoud   Amr") == "mahmoud amr")
    _check("mixed → consistent",
           normalize_sales_name(" mahmoud   AMR ") == "mahmoud amr")
    _check("None → empty", normalize_sales_name(None) == "")


# ─── Event hashing ──────────────────────────────────────────────────────

def test_compute_event_hash():
    print("─── compute_event_hash ───")
    args = dict(
        campaign_id=12, mobile="201012345678",
        follow_date=datetime(2026, 4, 23, 13, 42, 50),
        raw_sales_rep="Mahmoud Amr", normalized_stage="NO_ANSWER",
        comment="بعتله واتس",
    )
    h1 = compute_event_hash(**args)
    h2 = compute_event_hash(**args)
    _check("deterministic across calls", h1 == h2)
    _check("64-char hex", len(h1) == 64 and all(c in "0123456789abcdef" for c in h1))

    diff = dict(args)
    diff["mobile"] = "201019999999"
    _check("changes with mobile", compute_event_hash(**diff) != h1)
    diff = dict(args)
    diff["follow_date"] = datetime(2026, 4, 24, 13, 42, 50)
    _check("changes with date", compute_event_hash(**diff) != h1)
    diff = dict(args)
    diff["normalized_stage"] = "FOLLOWING"
    _check("changes with stage", compute_event_hash(**diff) != h1)


# ─── Parser end-to-end with FakeConn ────────────────────────────────────

def _build_sample_xlsx() -> bytes:
    """Build an in-memory .xlsx that exercises:
      - header aliases ("Phone" instead of "Mobile", "Notes" instead of "Comment")
      - forward-fill (rows 2 and 3 inherit row 1's client/mobile)
      - mixed stages (one known, one unknown — unknown lands in unmatched_stages)
      - a totally blank row (skipped)
      - a row with mobile-but-no-client (still attached to the client_name in scope)
    """
    wb = Workbook()
    ws = wb.active
    ws.append(["Client name", "Phone", "Stage", "Follow Date", "Sales Rep", "Notes"])

    # Lead 1: 3 events, forward-fill from row 1
    ws.append(["Ahmed Yehia", "01012345678", "Following",
               datetime(2026, 4, 21, 11, 0), "Mahmoud Amr", "first call"])
    ws.append([None, None, "Meeting",
               datetime(2026, 4, 22, 14, 0), "Mahmoud Amr", "zoom done"])
    ws.append([None, None, "No Answer",
               datetime(2026, 4, 23, 12, 0), "Reham Hany", "stopped responding"])

    # Totally blank row — should be skipped
    ws.append([None, None, None, None, None, None])

    # Lead 2: brand new client+mobile in one row
    ws.append(["Sara Ali", "+971569116811", "Interested",
               datetime(2026, 4, 24, 9, 30), "Mahmoud Amr", "wants brochure"])

    # Unknown stage — should land in unmatched_stages
    ws.append([None, None, "Discounted Special",
               datetime(2026, 4, 24, 10, 0), "Mahmoud Amr", "promo offer"])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def test_parser():
    print("─── parse_crm_excel ───")
    xlsx_bytes = _build_sample_xlsx()
    result = parse_crm_excel(io.BytesIO(xlsx_bytes), campaign_id=42, conn=_FakeConn())

    rows = result["rows"]
    _check(f"parsed 5 event rows (got {len(rows)})", len(rows) == 5)

    # Forward-fill
    if len(rows) >= 3:
        first_three = rows[:3]
        all_ahmed = all(r["client_name"] == "Ahmed Yehia" for r in first_three)
        _check("forward-fill carries client_name", all_ahmed,
               detail=str([r["client_name"] for r in first_three]))
        all_same_mobile = all(r["mobile"] == "201012345678" for r in first_three)
        _check("forward-fill carries mobile (normalized)", all_same_mobile,
               detail=str([r["mobile"] for r in first_three]))

    # Stage normalization
    if len(rows) >= 3:
        _check("row 2 stage Following → FOLLOWING",
               rows[0]["normalized_stage"] == "FOLLOWING")
        _check("row 3 stage Meeting → MEETING",
               rows[1]["normalized_stage"] == "MEETING")
        _check("row 4 stage No Answer → NO_ANSWER",
               rows[2]["normalized_stage"] == "NO_ANSWER")

    # Sara is row 4 in `rows` because the totally-blank one was skipped
    if len(rows) >= 5:
        sara = rows[3]
        _check("Sara's mobile normalized",
               sara["mobile"] == "971569116811", detail=sara["mobile"])
        _check("Sara's stage Interested → INTERESTED",
               sara["normalized_stage"] == "INTERESTED")

        unknown = rows[4]
        _check("unknown stage → normalized_stage=None",
               unknown["normalized_stage"] is None)
        # The unknown row still carries the previous client/mobile (forward-fill)
        _check("unknown row inherits Sara's mobile",
               unknown["mobile"] == "971569116811")

    # Unmatched stages / reps
    _check("unmatched_stages contains 'Discounted Special'",
           "Discounted Special" in result["unmatched_stages"],
           detail=str(result["unmatched_stages"]))
    # Without a DB, no rep can resolve to a user → both reps land here
    _check("unmatched_sales_reps includes both reps",
           set(result["unmatched_sales_reps"]) == {"Mahmoud Amr", "Reham Hany"},
           detail=str(result["unmatched_sales_reps"]))

    # Comment passthrough
    if rows:
        _check("comment preserved", rows[0]["comment"] == "first call",
               detail=repr(rows[0].get("comment")))


# ─── Dedup via event_hash ──────────────────────────────────────────────

def test_event_hash_dedup():
    print("─── event_hash dedup (same input → same hash) ───")
    # Two parses of the same bytes should produce identical event_hash sets,
    # which is what the UNIQUE constraint exploits on re-upload.
    xlsx_bytes = _build_sample_xlsx()
    r1 = parse_crm_excel(io.BytesIO(xlsx_bytes), campaign_id=42, conn=_FakeConn())
    r2 = parse_crm_excel(io.BytesIO(xlsx_bytes), campaign_id=42, conn=_FakeConn())

    def hashes(rows):
        return [
            compute_event_hash(
                campaign_id=42,
                mobile=r["mobile"],
                follow_date=r["follow_date"],
                raw_sales_rep=r["raw_sales_rep_name"],
                normalized_stage=r["normalized_stage"],
                comment=r["comment"],
            )
            for r in rows
        ]

    h1, h2 = hashes(r1["rows"]), hashes(r2["rows"])
    _check("same sheet → identical hash sequence", h1 == h2)
    _check("hashes within a sheet are unique",
           len(set(h1)) == len(h1),
           detail=f"got {len(h1)} hashes, {len(set(h1))} unique")


# ─── Manager intervention classifier ───────────────────────────────────
#
# These exercise the pure-function core of recalc_manager_intervention
# without needing a database. The classifier takes an ordered list of event
# dicts (ASC by follow_date) and returns either None (no flag) or the flag
# payload.

def _ev(stage, day, comment="", sales_user_id=7):
    """Build a minimal event dict matching the cursor row shape."""
    return {
        "id": day * 10,
        "lead_id": 1,
        "normalized_stage": stage,
        "follow_date": datetime(2026, 4, day),
        "comment": comment,
        "sales_user_id": sales_user_id,
    }


def test_intervention_classifier():
    print("─── _classify_lead_intervention ───")

    # Empty timeline → no flag
    _check("empty timeline → None",
           _classify_lead_intervention([]) is None)

    # NO_ANSWER from first contact → no flag (spec says no)
    _check("NO_ANSWER on its own → None",
           _classify_lead_intervention([_ev("NO_ANSWER", 1)]) is None)

    # Three NO_ANSWERs in a row, never went positive → no flag (spec says no)
    _check("repeated NO_ANSWER only → None",
           _classify_lead_intervention([
               _ev("NO_ANSWER", 1), _ev("NO_ANSWER", 2), _ev("NO_ANSWER", 3),
           ]) is None)

    # Following → NO_ANSWER → MEDIUM
    verdict = _classify_lead_intervention([
        _ev("FOLLOWING", 1, comment="will think"),
        _ev("NO_ANSWER", 2, comment="silence"),
    ])
    _check("Following → NO_ANSWER raises AFTER_FOLLOWING",
           verdict is not None and verdict["trigger"] == TRIGGER_NO_ANSWER_AFTER_FOLLOWING,
           detail=str(verdict))
    _check("AFTER_FOLLOWING is MEDIUM",
           verdict["priority"] == PRIORITY_MEDIUM)
    _check("previous_positive_stage is FOLLOWING",
           verdict["previous_positive_stage"] == "FOLLOWING")
    _check("last_comment carried from latest NO_ANSWER",
           verdict["last_comment"] == "silence")

    # Meeting → NO_ANSWER → HIGH
    verdict = _classify_lead_intervention([
        _ev("MEETING", 1),
        _ev("NO_ANSWER", 2),
    ])
    _check("Meeting → NO_ANSWER raises AFTER_MEETING",
           verdict is not None and verdict["trigger"] == TRIGGER_NO_ANSWER_AFTER_MEETING)
    _check("AFTER_MEETING is HIGH",
           verdict["priority"] == PRIORITY_HIGH)

    # Following → Meeting → NO_ANSWER → HIGH (meeting outranks following)
    verdict = _classify_lead_intervention([
        _ev("FOLLOWING", 1), _ev("MEETING", 2), _ev("NO_ANSWER", 3),
    ])
    _check("FOLLOWING + MEETING + NO_ANSWER → AFTER_MEETING wins",
           verdict["trigger"] == TRIGGER_NO_ANSWER_AFTER_MEETING,
           detail=verdict["trigger"])
    _check("priority is HIGH when meeting present",
           verdict["priority"] == PRIORITY_HIGH)

    # Last_positive_stage_date is the MOST RECENT positive stage's date
    verdict = _classify_lead_intervention([
        _ev("FOLLOWING", 1), _ev("FOLLOWING", 5), _ev("NO_ANSWER", 6),
    ])
    _check("last_positive_stage_date = most recent matching positive",
           verdict["last_positive_stage_date"].day == 5,
           detail=str(verdict["last_positive_stage_date"]))

    # Latest stage = MEETING → no flag (spec says no — only NO_ANSWER triggers)
    _check("latest=MEETING → None",
           _classify_lead_intervention([
               _ev("NO_ANSWER", 1), _ev("FOLLOWING", 2), _ev("MEETING", 3),
           ]) is None)

    # Latest stage = CANCELLATION → no flag
    _check("latest=CANCELLATION → None",
           _classify_lead_intervention([
               _ev("MEETING", 1), _ev("CANCELLATION", 2),
           ]) is None)

    # Latest stage = INTERESTED → no flag
    _check("latest=INTERESTED → None",
           _classify_lead_intervention([
               _ev("FOLLOWING", 1), _ev("INTERESTED", 2),
           ]) is None)


# ─── Assignment builder (Fresh vs Rotation) ─────────────────────────────
#
# Pure-function tests for _assignments_from_events. The function takes an
# ASC-sorted event list (each dict needs follow_date, sales_user_id,
# raw_sales_rep_name) and returns the ordered assignment dicts.

def _aev(day, user_id=None, raw_name=None):
    """Minimal event tuple matching the live row shape."""
    return {
        "follow_date": datetime(2026, 4, day),
        "sales_user_id": user_id,
        "raw_sales_rep_name": raw_name,
    }


def test_assignments_from_events():
    print("─── _assignments_from_events ───")

    # No events → no assignments
    _check("empty → []", _assignments_from_events([]) == [])

    # Single event with one rep → one FRESH assignment, ended_at=None
    out = _assignments_from_events([_aev(1, user_id=7)])
    _check("single event → 1 FRESH",
           len(out) == 1
           and out[0]["assignment_type"] == ASSIGNMENT_TYPE_FRESH
           and out[0]["ended_at"] is None
           and out[0]["sales_user_id"] == 7,
           detail=str(out))

    # Three events, same rep → still 1 FRESH, ended_at=None
    out = _assignments_from_events([
        _aev(1, user_id=7), _aev(2, user_id=7), _aev(3, user_id=7),
    ])
    _check("same rep streak → 1 FRESH",
           len(out) == 1
           and out[0]["assignment_type"] == ASSIGNMENT_TYPE_FRESH
           and out[0]["ended_at"] is None,
           detail=str(out))

    # Rep A → Rep B: 2 assignments. A is FRESH (closed), B is ROTATION (open).
    out = _assignments_from_events([
        _aev(1, user_id=7), _aev(2, user_id=7), _aev(3, user_id=9),
    ])
    _check("A→B → FRESH(A) + ROTATION(B)",
           len(out) == 2
           and out[0]["assignment_type"] == ASSIGNMENT_TYPE_FRESH
           and out[0]["sales_user_id"] == 7
           and out[0]["ended_at"].day == 3
           and out[1]["assignment_type"] == ASSIGNMENT_TYPE_ROTATION
           and out[1]["sales_user_id"] == 9
           and out[1]["ended_at"] is None,
           detail=str(out))

    # Rep returning later (A → B → A) → 3 assignments, the returning A is
    # ROTATION (NOT another FRESH).
    out = _assignments_from_events([
        _aev(1, user_id=7), _aev(2, user_id=9), _aev(3, user_id=7),
    ])
    _check("A→B→A → 3 assignments, returning A is ROTATION",
           len(out) == 3
           and out[0]["assignment_type"] == ASSIGNMENT_TYPE_FRESH
           and out[1]["assignment_type"] == ASSIGNMENT_TYPE_ROTATION
           and out[2]["assignment_type"] == ASSIGNMENT_TYPE_ROTATION
           and out[2]["sales_user_id"] == 7,
           detail=str(out))

    # Both reps unmatched (no sales_user_id, raw names differ) → still works
    out = _assignments_from_events([
        _aev(1, raw_name="Rana Hany"),
        _aev(2, raw_name="rana  hany"),  # normalizes to same
        _aev(3, raw_name="Yara K"),
    ])
    _check("unmatched: normalized-name compare collapses 'rana  hany'",
           len(out) == 2
           and out[0]["assignment_type"] == ASSIGNMENT_TYPE_FRESH
           and out[1]["sales_user_id"] is None
           and out[1]["raw_sales_rep_name"] == "Yara K",
           detail=str(out))

    # Mixed: matched user_id vs unmatched raw name → never equal (rotation)
    out = _assignments_from_events([
        _aev(1, user_id=7),
        _aev(2, raw_name="Rana Hany"),
    ])
    _check("matched vs unmatched → ROTATION",
           len(out) == 2
           and out[1]["assignment_type"] == ASSIGNMENT_TYPE_ROTATION
           and out[1]["sales_user_id"] is None,
           detail=str(out))

    # Events with no rep info at all → SKIPPED, don't break current assignment
    out = _assignments_from_events([
        _aev(1, user_id=7),
        _aev(2, user_id=None, raw_name=None),  # ghost row, skip
        _aev(3, user_id=7),
    ])
    _check("ghost (no rep) row doesn't break the streak",
           len(out) == 1
           and out[0]["sales_user_id"] == 7
           and out[0]["assignment_type"] == ASSIGNMENT_TYPE_FRESH,
           detail=str(out))

    # Ghost row before any real rep → first real event still opens FRESH
    out = _assignments_from_events([
        _aev(1, user_id=None, raw_name=None),
        _aev(2, user_id=7),
    ])
    _check("leading ghost row doesn't suppress FRESH",
           len(out) == 1 and out[0]["assignment_type"] == ASSIGNMENT_TYPE_FRESH,
           detail=str(out))

    # Half-open interval: assignment N's ended_at == assignment N+1's started_at
    out = _assignments_from_events([
        _aev(1, user_id=7), _aev(5, user_id=9),
    ])
    _check("half-open: ended_at == next started_at",
           out[0]["ended_at"] == out[1]["started_at"]
           and out[0]["ended_at"].day == 5,
           detail=str(out))


# ─── Timeline enrichment (risk + is_transfer) ───────────────────────────
#
# Pure-function tests for enrich_timeline_events. Input events need at
# minimum follow_date, normalized_stage, sales_user_id, raw_sales_rep_name.
# The function returns a NEW list of shallow copies with `risk` and
# `is_transfer` added — original list isn't mutated.

def _tev(day, stage, user_id=None, raw_name=None):
    """Build a minimal event dict matching the cursor row shape."""
    return {
        "event_id": day * 10,
        "follow_date": datetime(2026, 4, day),
        "normalized_stage": stage,
        "sales_user_id": user_id,
        "raw_sales_rep_name": raw_name,
    }


def test_enrich_timeline_events():
    print("─── enrich_timeline_events ───")

    # Empty list → empty list, no mutation
    _check("empty → []", enrich_timeline_events([]) == [])

    # Single NO_ANSWER, no positives earlier → yellow
    out = enrich_timeline_events([_tev(1, "NO_ANSWER", user_id=7)])
    _check("first-contact NO_ANSWER → yellow",
           out[0]["risk"] == RISK_YELLOW and out[0]["is_transfer"] is False,
           detail=str(out[0]))

    # Two NO_ANSWERs in a row, no positives → still yellow
    out = enrich_timeline_events([
        _tev(1, "NO_ANSWER", user_id=7),
        _tev(2, "NO_ANSWER", user_id=7),
    ])
    _check("repeated NO_ANSWER (no positive earlier) → yellow on both",
           out[0]["risk"] == RISK_YELLOW and out[1]["risk"] == RISK_YELLOW,
           detail=str([(e["follow_date"].day, e["risk"]) for e in out]))

    # FOLLOWING → NO_ANSWER → orange
    out = enrich_timeline_events([
        _tev(1, "FOLLOWING", user_id=7),
        _tev(2, "NO_ANSWER", user_id=7),
    ])
    _check("FOLLOWING then NO_ANSWER → orange",
           out[1]["risk"] == RISK_ORANGE,
           detail=str(out[1]))
    _check("the FOLLOWING event itself has no risk",
           out[0]["risk"] is None)

    # MEETING → NO_ANSWER → red
    out = enrich_timeline_events([
        _tev(1, "MEETING", user_id=7),
        _tev(2, "NO_ANSWER", user_id=7),
    ])
    _check("MEETING then NO_ANSWER → red",
           out[1]["risk"] == RISK_RED, detail=str(out[1]))

    # FOLLOWING + MEETING + NO_ANSWER → red (meeting outranks)
    out = enrich_timeline_events([
        _tev(1, "FOLLOWING", user_id=7),
        _tev(2, "MEETING",   user_id=7),
        _tev(3, "NO_ANSWER", user_id=7),
    ])
    _check("FOLLOWING + MEETING + NO_ANSWER → red on the NO_ANSWER",
           out[2]["risk"] == RISK_RED, detail=out[2]["risk"])

    # MEETING after NO_ANSWER doesn't promote earlier NO_ANSWER risk —
    # it's a one-pass forward walk, and the meeting hadn't happened yet
    # when that NO_ANSWER occurred.
    out = enrich_timeline_events([
        _tev(1, "NO_ANSWER", user_id=7),
        _tev(2, "MEETING",   user_id=7),
        _tev(3, "NO_ANSWER", user_id=7),
    ])
    _check("first NO_ANSWER stays yellow (meeting hadn't happened yet)",
           out[0]["risk"] == RISK_YELLOW, detail=out[0]["risk"])
    _check("later NO_ANSWER (after meeting) becomes red",
           out[2]["risk"] == RISK_RED, detail=out[2]["risk"])

    # Non-NO_ANSWER stages → risk is None even after positives
    out = enrich_timeline_events([
        _tev(1, "FOLLOWING",    user_id=7),
        _tev(2, "MEETING",      user_id=7),
        _tev(3, "CANCELLATION", user_id=7),
    ])
    _check("MEETING event itself has no risk", out[1]["risk"] is None)
    _check("CANCELLATION event has no risk",   out[2]["risk"] is None)

    # is_transfer when rep flips (same matched-vs-matched comparison)
    out = enrich_timeline_events([
        _tev(1, "FOLLOWING", user_id=7),
        _tev(2, "NO_ANSWER", user_id=9),
    ])
    _check("rep flip → is_transfer=True on the second event",
           out[1]["is_transfer"] is True, detail=str(out[1]))
    _check("first event of timeline is never a transfer",
           out[0]["is_transfer"] is False)

    # is_transfer false when same matched rep continues
    out = enrich_timeline_events([
        _tev(1, "FOLLOWING", user_id=7),
        _tev(2, "MEETING",   user_id=7),
    ])
    _check("same rep continuing → no transfer",
           out[1]["is_transfer"] is False)

    # Unmatched name normalization for transfer detection
    out = enrich_timeline_events([
        _tev(1, "FOLLOWING", raw_name="Rana Hany"),
        _tev(2, "NO_ANSWER", raw_name="rana  hany"),  # normalizes equal
        _tev(3, "FOLLOWING", raw_name="Yara K"),
    ])
    _check("normalized-equal unmatched names → no transfer between 1 and 2",
           out[1]["is_transfer"] is False)
    _check("different unmatched name → transfer at event 3",
           out[2]["is_transfer"] is True)

    # Mixed matched/unmatched → always considered a transfer
    out = enrich_timeline_events([
        _tev(1, "FOLLOWING", user_id=7),
        _tev(2, "NO_ANSWER", raw_name="Rana Hany"),
    ])
    _check("matched → unmatched → transfer",
           out[1]["is_transfer"] is True)

    # Ghost row (no rep info at all) is NOT a transfer and doesn't break
    # the streak — the next rep-bearing event compares against the
    # previous rep-bearing event, skipping the ghost.
    out = enrich_timeline_events([
        _tev(1, "FOLLOWING", user_id=7),
        _tev(2, "NO_ANSWER", user_id=None, raw_name=None),  # ghost
        _tev(3, "MEETING",   user_id=7),  # same rep as event 1
    ])
    _check("ghost row → is_transfer=False, doesn't break the streak",
           out[1]["is_transfer"] is False
           and out[2]["is_transfer"] is False,
           detail=str([(e["follow_date"].day, e["is_transfer"]) for e in out]))


# ─── Response-rate percent helper (P4) ──────────────────────────────────
#
# _response_rate_pct(total, no_answer) returns the percentage of attempts
# that landed on something other than NO_ANSWER. Used by both the daily
# activity endpoint and the marketing report — keeping the formula in one
# place is what makes those two views agree on numbers.

def test_response_rate_pct():
    print("─── _response_rate_pct ───")
    # Normal mid-range case — 11/20 answered = 55%
    _check("11 of 20 answered → 55.0",
           _response_rate_pct(20, 9) == 55.0,
           detail=str(_response_rate_pct(20, 9)))
    # All answered → 100%
    _check("all answered → 100.0",
           _response_rate_pct(10, 0) == 100.0)
    # None answered → 0%
    _check("none answered → 0.0",
           _response_rate_pct(10, 10) == 0.0)
    # total=0 → 0.0 (no NaN division)
    _check("total=0 → 0.0",
           _response_rate_pct(0, 0) == 0.0)
    # total=None defensive guard → 0.0
    _check("total=None → 0.0",
           _response_rate_pct(None, 0) == 0.0)
    # no_answer=None defensive guard treats it as 0
    _check("no_answer=None → 100.0 with total>0",
           _response_rate_pct(5, None) == 100.0)
    # Rounding to one decimal
    _check("rounds to 1 decimal (1/3) → 66.7",
           _response_rate_pct(3, 1) == 66.7,
           detail=str(_response_rate_pct(3, 1)))
    # Negative no_answer clamps to 0 — defensive against bad input
    _check("negative no_answer clamps to 0 → 100.0",
           _response_rate_pct(10, -3) == 100.0)


# ─── Required-column enforcement ───────────────────────────────────────

def test_missing_required_column_raises():
    print("─── missing required column raises ValueError ───")
    wb = Workbook()
    ws = wb.active
    # Intentionally drop "Sales Rep"
    ws.append(["Client name", "Mobile", "Stage", "Follow Date", "Notes"])
    ws.append(["X", "01012345678", "Following",
               datetime(2026, 4, 21, 11, 0), "first call"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    raised = False
    try:
        parse_crm_excel(buf, campaign_id=1, conn=_FakeConn())
    except ValueError as e:
        raised = True
        msg_ok = "sales_rep" in str(e).lower() or "sales rep" in str(e).lower()
        _check("error message names the missing column", msg_ok, detail=str(e))
    _check("ValueError was raised", raised)


# ─── Driver ────────────────────────────────────────────────────────────

def main():
    test_normalize_mobile()
    test_normalize_stage()
    test_normalize_sales_name()
    test_compute_event_hash()
    test_parser()
    test_event_hash_dedup()
    test_missing_required_column_raises()
    test_intervention_classifier()
    test_assignments_from_events()
    test_enrich_timeline_events()
    test_response_rate_pct()

    print()
    if _failures:
        print(f"❌ {_failures} failure(s)")
        sys.exit(1)
    print("✅ all green")


if __name__ == "__main__":
    main()
