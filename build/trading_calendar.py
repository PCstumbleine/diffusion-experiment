"""
Section 7f Implementation Spec, FINAL (specs/section7f-implementation-spec-final.md),
Section 2: shared NYSE trading-calendar utility.

Needed for outcome_due_at (confirmatory_builder.py, Section 4) and, later,
whatever sets arm_entries.entry_timestamp/arm_outcomes.exit_timestamp per
Section 8 spec's 7b "09:30 ET on the next regular trading session" rule --
built once here as reusable infrastructure, not re-derived per caller. NOT
wired into any entry/exit-timestamp-setting code in this pass -- that
execution pipeline does not exist yet (Section 0's non-goals).

Dependency: pandas_market_calendars, calendar 'NYSE'. Verified directly
(not from documentation alone) against the pinned version -- see
tests/test_trading_calendar.py -- correctly excludes weekends and NYSE
holidays (July 4th, Thanksgiving), correctly models special/shortened
sessions (the day-after-Thanksgiving early close is a valid session, not a
closure), and returns market_open/market_close as timezone-aware UTC
timestamps.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pandas_market_calendars as mcal

MARKET_CALENDAR = "NYSE"
MARKET_CALENDAR_LIBRARY = "pandas_market_calendars"
MARKET_CALENDAR_LIBRARY_VERSION = "5.4.0"  # pin exactly; bump requires the
    # calendar regression tests (tests/test_trading_calendar.py) to pass
    # again -- an unpinned dependency update must never silently change
    # historical session resolution mid-collection.

# A single shared Calendar object -- pandas_market_calendars documents this
# as safe and intended for reuse (it's stateless per query beyond its own
# internal holiday/schedule tables), and constructing it is not free.
_calendar = None


def _get_calendar():
    global _calendar
    if _calendar is None:
        if pandas_market_calendars_version() != MARKET_CALENDAR_LIBRARY_VERSION:
            raise RuntimeError(
                f"pandas_market_calendars is installed at version "
                f"{pandas_market_calendars_version()!r}, not the pinned "
                f"{MARKET_CALENDAR_LIBRARY_VERSION!r} -- a bump requires the calendar regression "
                "tests to pass again before this pin is updated; refusing to silently resolve "
                "sessions against an unverified library version."
            )
        _calendar = mcal.get_calendar(MARKET_CALENDAR)
    return _calendar


def pandas_market_calendars_version() -> str:
    return mcal.__version__


# Defensive upper bound on how far forward to search for the next session --
# NYSE has never had a closure remotely approaching this length; this exists
# only so a pathological/misconfigured calendar fails loudly instead of
# looping unboundedly.
_MAX_SEARCH_WINDOW_DAYS = 3650


def next_regular_session_open_strictly_after(after: datetime) -> datetime:
    """Return the NYSE calendar's scheduled market_open strictly later than
    `after` -- i.e. outcome_due_at = min{opens : open > entry_timestamp}.
    `strictly` matters: if `after` is itself exactly equal to a session's
    market_open, the result is the FOLLOWING session's open, not that same
    instant.

    `after` must be timezone-aware; raises ValueError if naive. Returns a
    timezone-aware UTC datetime -- the actual scheduled market_open from the
    calendar, never a hand-computed `session_date + time(9, 30)`, so this
    never becomes a second, independently-drifting definition of "session
    open" from whatever the calendar library actually resolves (DST
    transitions, one-off historical closures, etc. are the library's
    responsibility, not reimplemented here).
    """
    if after.tzinfo is None:
        raise ValueError(
            f"next_regular_session_open_strictly_after: `after`={after!r} is naive (no tzinfo) -- "
            "a timezone-aware datetime is required; refusing to guess a timezone."
        )

    calendar = _get_calendar()
    after_ts = pd.Timestamp(after)
    window_days = 10
    start_date = after.date()

    while window_days <= _MAX_SEARCH_WINDOW_DAYS:
        end_date = start_date + timedelta(days=window_days)
        schedule = calendar.schedule(start_date=start_date, end_date=end_date)
        candidates = schedule[schedule["market_open"] > after_ts]
        if not candidates.empty:
            return candidates.iloc[0]["market_open"].to_pydatetime()
        window_days *= 4

    raise RuntimeError(
        f"next_regular_session_open_strictly_after: found no NYSE session open strictly after "
        f"{after!r} within {_MAX_SEARCH_WINDOW_DAYS} days -- this should be unreachable for any "
        "real input and suggests a calendar-library problem, not a genuine closure."
    )
