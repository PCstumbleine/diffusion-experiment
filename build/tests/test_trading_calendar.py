"""
Section 7f Implementation Spec, FINAL (specs/section7f-implementation-spec-final.md),
Section 10's calendar-utility tests. Every expected value below was
independently verified empirically against the pinned pandas_market_calendars
5.4.0 (see the module docstring in trading_calendar.py) by directly querying
`mcal.get_calendar("NYSE").schedule(...)` before writing these assertions --
not assumed from documentation or memory. If pandas_market_calendars is ever
bumped past 5.4.0, this file is the regression suite that must pass again
first (trading_calendar.py's own MARKET_CALENDAR_LIBRARY_VERSION pin refuses
to run against any other installed version).
"""
from datetime import datetime, timedelta, timezone

import pytest

from trading_calendar import next_regular_session_open_strictly_after


UTC = timezone.utc


def test_ordinary_tuesday_to_wednesday_open():
    # Tue 2024-07-09, well after that day's own 13:30 UTC open.
    after = datetime(2024, 7, 9, 15, 0, tzinfo=UTC)
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2024, 7, 10, 13, 30, tzinfo=UTC)


def test_friday_to_monday_open():
    # Fri 2024-07-05, well after that day's own open.
    after = datetime(2024, 7, 5, 15, 0, tzinfo=UTC)
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2024, 7, 8, 13, 30, tzinfo=UTC)


def test_friday_preceding_a_monday_nyse_holiday_skips_to_tuesday():
    # Fri 2025-01-17, after that day's 14:30 UTC open; MLK Day is
    # Mon 2025-01-20 (closed) -- next session is Tue 2025-01-21.
    after = datetime(2025, 1, 17, 16, 0, tzinfo=UTC)
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2025, 1, 21, 14, 30, tzinfo=UTC)


def test_independence_day_observed_closure():
    # Wed 2024-07-03 is a valid (early-close) session; Thu 2024-07-04 is
    # closed for Independence Day -- next session is Fri 2024-07-05.
    after = datetime(2024, 7, 3, 18, 0, tzinfo=UTC)  # after that day's 17:00 UTC early close
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2024, 7, 5, 13, 30, tzinfo=UTC)


def test_thanksgiving_next_session_is_the_day_after_not_skipped_further():
    """Thu 2024-11-28 (Thanksgiving) is closed. The very next session is
    Fri 2024-11-29 -- the day-after-Thanksgiving EARLY-CLOSE session
    itself, confirmed as a real, valid session (its market_open exists in
    the schedule, at the normal 14:30 UTC time -- only market_close is
    early), not a second closure that would push the result to the
    following Monday."""
    after = datetime(2024, 11, 28, 12, 0, tzinfo=UTC)  # during Thanksgiving itself, fully closed
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2024, 11, 29, 14, 30, tzinfo=UTC)


def test_christmas_closure():
    # Tue 2024-12-24 is a valid early-close session; Wed 2024-12-25 is
    # closed for Christmas -- next session is Thu 2024-12-26.
    after = datetime(2024, 12, 24, 19, 0, tzinfo=UTC)  # after that day's 18:00 UTC early close
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2024, 12, 26, 14, 30, tzinfo=UTC)


def test_new_years_closure():
    # Tue 2024-12-31 is a valid session; Wed 2025-01-01 is closed for
    # New Year's Day -- next session is Thu 2025-01-02.
    after = datetime(2024, 12, 31, 22, 0, tzinfo=UTC)
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2025, 1, 2, 14, 30, tzinfo=UTC)


def test_crosses_us_dst_fall_back_transition_correct_open_both_sides():
    """US DST 2024 ends Sun 2024-11-03. Fri 2024-11-01 (before) is still
    EDT: 13:30 UTC = 9:30 EDT. The next session, Mon 2024-11-04 (after), is
    EST: 14:30 UTC = 9:30 EST -- the scheduled UTC offset shifts by an
    hour, but the NYSE LOCAL open (9:30) is correctly 9:30 on both sides,
    confirming this comes from the calendar library's own DST handling,
    never a hand-computed fixed UTC offset."""
    after = datetime(2024, 11, 1, 14, 0, tzinfo=UTC)  # after Fri's own 13:30 UTC (9:30 EDT) open
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2024, 11, 4, 14, 30, tzinfo=UTC)
    # 14:30 UTC on 2024-11-04 is exactly 9:30 EST (UTC-5) -- confirms the
    # LOCAL open time, not just the raw UTC instant, is correct post-transition.
    assert result.astimezone(timezone(timedelta(hours=-5))).time() == datetime(2024, 11, 4, 9, 30).time()


def test_crosses_us_dst_spring_forward_transition_correct_open_both_sides():
    """US DST 2025 begins Sun 2025-03-09. Fri 2025-03-07 (before) is EST:
    14:30 UTC = 9:30 EST. The next session, Mon 2025-03-10 (after), is
    EDT: 13:30 UTC = 9:30 EDT."""
    after = datetime(2025, 3, 7, 15, 0, tzinfo=UTC)  # after Fri's own 14:30 UTC (9:30 EST) open
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2025, 3, 10, 13, 30, tzinfo=UTC)
    assert result.astimezone(timezone(timedelta(hours=-4))).time() == datetime(2025, 3, 10, 9, 30).time()


def test_historical_one_off_nyse_closure_hurricane_sandy_2012():
    """Mon 2012-10-29 and Tue 2012-10-30 2012 were NYSE closures for
    Hurricane Sandy -- a genuine one-off historical closure the pinned
    library represents (confirmed absent from the real schedule output,
    not assumed from a holiday calendar), distinct from any regularly
    recurring holiday. Next session after Fri 2012-10-26 is Wed 2012-10-31."""
    after = datetime(2012, 10, 26, 21, 0, tzinfo=UTC)  # after Fri's own 20:00 UTC close
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2012, 10, 31, 13, 30, tzinfo=UTC)


def test_input_exactly_equal_to_a_sessions_market_open_returns_the_following_session():
    """Strictness: Mon 2024-07-08's own market_open (13:30 UTC) passed in
    directly must NOT return that same instant -- it must return the NEXT
    session's open (Tue 2024-07-09), confirming strict '>' semantics."""
    after = datetime(2024, 7, 8, 13, 30, 0, tzinfo=UTC)  # exactly Monday's own open
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2024, 7, 9, 13, 30, tzinfo=UTC)


def test_input_a_few_seconds_after_an_open_returns_the_following_session():
    after = datetime(2024, 7, 8, 13, 30, 5, tzinfo=UTC)  # 5 seconds after Monday's own open
    result = next_regular_session_open_strictly_after(after)
    assert result == datetime(2024, 7, 9, 13, 30, tzinfo=UTC)


def test_naive_input_raises():
    naive = datetime(2024, 7, 8, 13, 30)  # no tzinfo
    with pytest.raises(ValueError):
        next_regular_session_open_strictly_after(naive)


def test_result_is_timezone_aware_utc():
    after = datetime(2024, 7, 8, 15, 0, tzinfo=UTC)
    result = next_regular_session_open_strictly_after(after)
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)
