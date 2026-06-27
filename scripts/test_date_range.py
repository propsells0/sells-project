"""
Tests for app.util.date_range — parse_range / resolve_preset / boundary cases.

Run:  PYTHONIOENCODING=utf-8 python scripts/test_date_range.py
Exits non-zero on any failure. No test framework — keeps the dependency
footprint flat per CLAUDE.md ("no test suite, no linter config").
"""
import sys
from datetime import date

# Ensure repo root on sys.path when invoked as a script
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.util.date_range import (  # noqa: E402
    parse_range, InvalidRangeError, PRESET_KEYS,
)


_failures = 0


def _check(name, ok, detail=""):
    global _failures
    if ok:
        print(f"  ok  {name}")
    else:
        _failures += 1
        print(f"  FAIL {name}: {detail}")


def _expect_error(name, code, fn):
    try:
        fn()
        _check(name, False, f"expected InvalidRangeError({code}), got success")
    except InvalidRangeError as e:
        _check(name, e.code == code, f"expected {code}, got {e.code}")
    except Exception as e:
        _check(name, False, f"expected InvalidRangeError, got {type(e).__name__}: {e}")


def main():
    print("─── parse_range: legacy ?month= compat ───")

    pr = parse_range({"month": "2026-04"})
    _check("month → from_date", pr.from_date == date(2026, 4, 1))
    _check("month → to_date",   pr.to_date == date(2026, 4, 30))
    _check("month → month_str", pr.month_str == "2026-04")
    _check("month → is_sub_month false", pr.is_sub_month is False)

    pr = parse_range({"month": "2024-02"})  # leap year
    _check("leap-year Feb 29",  pr.to_date == date(2024, 2, 29))
    _check("leap-year month_str", pr.month_str == "2024-02")

    pr = parse_range({"month": "2025-02"})  # non-leap
    _check("non-leap Feb 28",   pr.to_date == date(2025, 2, 28))

    print("\n─── parse_range: explicit from/to ───")

    pr = parse_range({"from": "2026-04-01", "to": "2026-04-30"})
    _check("calendar-month range → month_str detected",
           pr.month_str == "2026-04" and pr.is_sub_month is False)
    _check("calendar-month range → preset=custom", pr.preset == "custom")

    pr = parse_range({"from": "2026-04-05", "to": "2026-04-12"})
    _check("partial-month range → month_str None",
           pr.month_str is None and pr.is_sub_month is True)

    pr = parse_range({"from": "2026-03-15", "to": "2026-04-10"})
    _check("crosses-month range → is_sub_month true", pr.is_sub_month is True)
    _check("crosses-month range → month_str None", pr.month_str is None)

    pr = parse_range({"from": "2026-01-01", "to": "2026-12-31"})
    # Calendar year is multi-month-aligned (Jan 1 + Dec 31 = full months on both
    # ends), so is_sub_month=False — queried via month BETWEEN, not by timestamp.
    _check("calendar year → is_sub_month False", pr.is_sub_month is False)
    _check("calendar year → month_str None (multi-month, not single)", pr.month_str is None)

    # Sub-month sanity: partial start
    pr = parse_range({"from": "2026-01-15", "to": "2026-12-31"})
    _check("partial-start range → is_sub_month True", pr.is_sub_month is True)

    # Sub-month sanity: partial end
    pr = parse_range({"from": "2026-01-01", "to": "2026-12-15"})
    _check("partial-end range → is_sub_month True", pr.is_sub_month is True)

    print("\n─── parse_range: presets ───")

    today = date.today()
    pr = parse_range({"preset": "today"})
    _check("today preset → from==to==today", pr.from_date == today and pr.to_date == today)

    pr = parse_range({"preset": "yesterday"})
    yest = date.fromordinal(today.toordinal() - 1)
    _check("yesterday preset", pr.from_date == yest and pr.to_date == yest)

    pr = parse_range({"preset": "last_7"})
    _check("last_7 spans 7 days", (pr.to_date - pr.from_date).days == 6)

    pr = parse_range({"preset": "last_30"})
    _check("last_30 spans 30 days", (pr.to_date - pr.from_date).days == 29)

    pr = parse_range({"preset": "this_month"})
    _check("this_month from-day-1", pr.from_date.day == 1)
    _check("this_month to <= today", pr.to_date <= today)

    pr = parse_range({"preset": "last_month"})
    _check("last_month ends before this month", pr.to_date.month != today.month or pr.to_date.year != today.year)
    _check("last_month from-day-1", pr.from_date.day == 1)

    pr = parse_range({"preset": "this_quarter"})
    _check("this_quarter from-day-1", pr.from_date.day == 1)
    _check("this_quarter month is quarter start", pr.from_date.month in (1, 4, 7, 10))

    pr = parse_range({"preset": "ytd"})
    _check("ytd from Jan 1", pr.from_date == date(today.year, 1, 1))

    print("\n─── parse_range: priority + default ───")

    # Explicit from/to wins over preset wins over month
    pr = parse_range({"from": "2026-04-05", "to": "2026-04-12",
                      "preset": "this_month", "month": "2026-01"})
    _check("explicit from/to wins",
           pr.from_date == date(2026, 4, 5) and pr.to_date == date(2026, 4, 12))

    pr = parse_range({"preset": "this_month", "month": "2026-01"})
    _check("preset wins over month", pr.preset == "this_month")

    # No args → default_preset
    pr = parse_range({})
    _check("no args → default this_month", pr.preset == "this_month")

    pr = parse_range({}, default_preset="last_30")
    _check("no args + override default", pr.preset == "last_30")

    print("\n─── parse_range: errors ───")

    _expect_error("inverted range", "range_inverted",
                  lambda: parse_range({"from": "2026-04-30", "to": "2026-04-01"}))

    _expect_error("invalid date string", "invalid_date",
                  lambda: parse_range({"from": "2026-13-01", "to": "2026-12-31"}))

    _expect_error("malformed month", "invalid_date",
                  lambda: parse_range({"month": "26-04"}))

    _expect_error("month month-out-of-range", "invalid_date",
                  lambda: parse_range({"month": "2026-13"}))

    _expect_error("preset unknown", "invalid_preset",
                  lambda: parse_range({"preset": "next_century"}))

    _expect_error("preset=custom without dates", "invalid_date",
                  lambda: parse_range({"preset": "custom"}))

    # > 5 years → range_too_wide
    _expect_error("range_too_wide", "range_too_wide",
                  lambda: parse_range({"from": "2010-01-01", "to": "2026-04-30"}))

    _expect_error("date below MIN_DATE", "invalid_date",
                  lambda: parse_range({"from": "1999-01-01", "to": "1999-01-31"}))

    print("\n─── parse_range: allow_sub_month=False ───")

    # Calendar-aligned month is OK
    pr = parse_range({"month": "2026-04"}, allow_sub_month=False)
    _check("month-aligned passes when sub_month disallowed", pr.month_str == "2026-04")

    # Sub-month should reject
    _expect_error("sub-month rejected when disallowed", "sub_month_not_allowed",
                  lambda: parse_range({"from": "2026-04-05", "to": "2026-04-12"},
                                      allow_sub_month=False))

    # Multi-month aligned: passes — it's queried via month BETWEEN, no timestamp filter needed.
    pr = parse_range({"from": "2026-03-01", "to": "2026-04-30"}, allow_sub_month=False)
    _check("multi-month aligned passes when sub_month disallowed",
           pr.is_sub_month is False and pr.month_str is None)

    # Mixed: aligned start, sub-month end → still sub-month, reject.
    _expect_error("partial-end multi-month rejected when disallowed", "sub_month_not_allowed",
                  lambda: parse_range({"from": "2026-03-01", "to": "2026-04-15"},
                                      allow_sub_month=False))

    print("\n─── PRESET_KEYS exposes the public set ───")
    expected = {"today", "yesterday", "this_week", "last_7", "last_30",
                "this_month", "last_month", "this_quarter", "ytd", "custom"}
    _check("PRESET_KEYS matches expected", PRESET_KEYS == expected)

    print()
    if _failures:
        print(f"❌ {_failures} failure(s)")
        sys.exit(1)
    print("✅ all date_range tests passed")


if __name__ == "__main__":
    main()
