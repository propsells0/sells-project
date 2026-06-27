"""
Date-range parsing for cross-cutting filter endpoints.

Inputs (any one form, resolution order: explicit from/to → preset → month → default):

  ?from=YYYY-MM-DD&to=YYYY-MM-DD     explicit range
  ?preset=this_month|...             named preset (resolved server-side)
  ?month=YYYY-MM                     legacy compat — translated to first/last of month

Caller does:

    pr = parse_range(request.args)
    if pr.month_str:           # exact calendar month — fastpath the month-equality SQL
        ...
    else:
        # filter by sales_submitted_at / dataentry_submitted_at within [pr.from_date, pr.to_date]
        ...

All dates are app-server local (Africa/Cairo). TIMESTAMPTZ migration is a
separate phase — see CLAUDE.md "Known issues / future work".
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from config import Config


# ─── Errors ───────────────────────────────────────────────────────────────────

class InvalidRangeError(Exception):
    """Raised when args don't parse to a usable range. .code maps to errors.* i18n keys."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ─── Result type ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ParsedRange:
    from_date: date
    to_date: date
    preset: str
    is_sub_month: bool
    month_str: Optional[str]   # "YYYY-MM" iff range is exactly one calendar month, else None

    def to_dict(self):
        return {
            "from": self.from_date.isoformat(),
            "to": self.to_date.isoformat(),
            "preset": self.preset,
            "is_sub_month": self.is_sub_month,
            "month_str": self.month_str,
        }


# ─── Bounds / sanity ──────────────────────────────────────────────────────────

MIN_DATE = date(2020, 1, 1)


def _today() -> date:
    return date.today()


def _max_date() -> date:
    # No further than 1 year past today — defends against absurd inputs.
    return _today() + timedelta(days=365)


def _max_range_days() -> int:
    return Config.MAX_RANGE_YEARS * 366  # leap-year tolerant


# ─── Preset resolver ──────────────────────────────────────────────────────────

PRESET_KEYS = {
    "today", "yesterday", "this_week", "last_7", "last_30",
    "this_month", "last_month", "this_quarter", "ytd", "custom",
}

# Presets that produce a sub-month range (must be filtered by submission_at).
SUB_MONTH_PRESETS = {"today", "yesterday", "this_week", "last_7"}


def resolve_preset(preset: str, today: Optional[date] = None) -> tuple[date, date]:
    """Return (from_date, to_date) inclusive for the given preset key."""
    t = today or _today()
    if preset == "today":
        return (t, t)
    if preset == "yesterday":
        y = t - timedelta(days=1)
        return (y, y)
    if preset == "this_week":
        # Week starts Saturday in Egypt; we use Monday-as-start to keep it
        # locale-neutral and align with ISO. Bilingual labels handle UX.
        start = t - timedelta(days=t.weekday())
        return (start, t)
    if preset == "last_7":
        return (t - timedelta(days=6), t)
    if preset == "last_30":
        return (t - timedelta(days=29), t)
    if preset == "this_month":
        return (t.replace(day=1), t)
    if preset == "last_month":
        first_this = t.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return (first_prev, last_prev)
    if preset == "this_quarter":
        q_start_month = ((t.month - 1) // 3) * 3 + 1
        return (date(t.year, q_start_month, 1), t)
    if preset == "ytd":
        return (date(t.year, 1, 1), t)
    raise InvalidRangeError("invalid_preset", f"unknown preset: {preset}")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_iso_date(s: str, field: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise InvalidRangeError("invalid_date", f"{field} not a valid ISO date: {s!r}")


def _is_full_calendar_month(d_from: date, d_to: date) -> Optional[str]:
    """If [from, to] exactly covers one calendar month, return 'YYYY-MM'; else None."""
    if d_from.year != d_to.year or d_from.month != d_to.month:
        return None
    if d_from.day != 1:
        return None
    last_day = calendar.monthrange(d_from.year, d_from.month)[1]
    if d_to.day != last_day:
        return None
    return f"{d_from.year:04d}-{d_from.month:02d}"


def _validate_bounds(d_from: date, d_to: date):
    if d_to < d_from:
        raise InvalidRangeError("range_inverted", "to_date is before from_date")
    # Width check first: "your range is too wide" is a clearer error than
    # "your start date is below MIN_DATE" when the user picks Jan 2010 → today.
    if (d_to - d_from).days + 1 > _max_range_days():
        raise InvalidRangeError("range_too_wide",
                                f"range exceeds Config.MAX_RANGE_YEARS={Config.MAX_RANGE_YEARS}")
    if d_from < MIN_DATE or d_to > _max_date():
        raise InvalidRangeError("invalid_date", "date outside accepted bounds")


# ─── Public entry point ──────────────────────────────────────────────────────

def parse_range(args, *, default_preset: str = "this_month",
                allow_sub_month: bool = True) -> ParsedRange:
    """
    Parse a Flask request.args (or any dict-ish) into a ParsedRange.

    Resolution order:
      1. explicit from + to → date range, preset='custom'
      2. preset alone → resolve via resolve_preset()
      3. month=YYYY-MM (legacy) → translate to [first, last] of month
      4. fallback to default_preset
    """
    raw_from = (args.get("from") or "").strip() or None
    raw_to = (args.get("to") or "").strip() or None
    raw_preset = (args.get("preset") or "").strip() or None
    raw_month = (args.get("month") or "").strip() or None

    if raw_from and raw_to:
        d_from = _parse_iso_date(raw_from, "from")
        d_to = _parse_iso_date(raw_to, "to")
        preset = "custom"
    elif raw_preset:
        if raw_preset == "custom":
            # custom without explicit dates is invalid
            raise InvalidRangeError("invalid_date", "preset=custom requires from and to")
        if raw_preset not in PRESET_KEYS:
            raise InvalidRangeError("invalid_preset", f"unknown preset: {raw_preset}")
        d_from, d_to = resolve_preset(raw_preset)
        preset = raw_preset
    elif raw_month:
        # Legacy ?month=YYYY-MM compat: translate to that calendar month.
        try:
            y, m = raw_month.split("-")
            year, month = int(y), int(m)
            if not (1 <= month <= 12) or year < 1900:
                raise ValueError
        except ValueError:
            raise InvalidRangeError("invalid_date", f"month not in YYYY-MM form: {raw_month!r}")
        last = calendar.monthrange(year, month)[1]
        d_from = date(year, month, 1)
        d_to = date(year, month, last)
        preset = "this_month" if (year, month) == (_today().year, _today().month) else "custom"
    else:
        d_from, d_to = resolve_preset(default_preset)
        preset = default_preset

    _validate_bounds(d_from, d_to)

    month_str = _is_full_calendar_month(d_from, d_to)
    # A range is "month-aligned" when from-day=1 AND to-day=last-day-of-its-month.
    # Single calendar month → month_str set, is_sub_month False.
    # Multi-month aligned (e.g. 2026-03-01 → 2026-04-30) → month_str None,
    # is_sub_month False, queried via month BETWEEN '2026-03' AND '2026-04'.
    # Anything else → is_sub_month True, queried via submission timestamp.
    is_aligned = (
        d_from.day == 1
        and d_to.day == calendar.monthrange(d_to.year, d_to.month)[1]
    )
    is_sub_month = not is_aligned

    if is_sub_month and not allow_sub_month:
        raise InvalidRangeError("sub_month_not_allowed",
                                "this endpoint only supports month-aligned ranges")

    return ParsedRange(
        from_date=d_from,
        to_date=d_to,
        preset=preset,
        is_sub_month=is_sub_month,
        month_str=month_str,
    )
