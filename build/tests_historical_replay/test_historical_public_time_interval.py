"""
Historical Replay Phase 1B (specs/historical-replay-phase1b-implementation-spec-final.md),
Section 8's historical_public_time_interval tests. Pure-Python, no database
involved -- confirms the DST-aware civil-day formula empirically, per
Section 2's frozen policy.

US DST transition dates used below were independently confirmed
empirically against the pinned zoneinfo database (not assumed/looked up
from memory): 2026-03-08 (second Sunday in March) is the spring-forward
date, and 2026-11-01 (first Sunday in November) is the fall-back date.
"""
from datetime import date, timedelta

from historical_edgar_ingest import historical_public_time_interval


def test_ordinary_day_produces_24h_precision():
    canonical, precision = historical_public_time_interval(date(2026, 1, 15))
    assert precision == timedelta(hours=24)


def test_spring_forward_day_produces_23h_precision():
    """2026-03-08: the Eastern civil day loses an hour (clocks spring
    forward at 2am local) -- the UTC width of that local day is 23h."""
    canonical, precision = historical_public_time_interval(date(2026, 3, 8))
    assert precision == timedelta(hours=23)


def test_fall_back_day_produces_25h_precision():
    """2026-11-01: the Eastern civil day gains an hour (clocks fall back
    at 2am local) -- the UTC width of that local day is 25h."""
    canonical, precision = historical_public_time_interval(date(2026, 11, 1))
    assert precision == timedelta(hours=25)


def test_canonical_first_public_at_is_always_the_upper_bound():
    from zoneinfo import ZoneInfo
    sec_tz = ZoneInfo("America/New_York")
    for filing_date in (date(2026, 1, 15), date(2026, 3, 8), date(2026, 11, 1)):
        canonical, precision = historical_public_time_interval(filing_date)
        lower_bound = canonical - precision
        # canonical == upper bound; the interval is [canonical - precision, canonical].
        assert canonical > lower_bound
        # The upper bound must correspond to local Eastern midnight at the
        # START of the NEXT day (filing_date + 1), never a midpoint or the
        # filing_date's own midnight -- confirmed by converting the UTC
        # canonical value back to Eastern local time and checking both the
        # calendar date and the wall-clock time.
        canonical_local = canonical.astimezone(sec_tz)
        assert canonical_local.date() == filing_date + timedelta(days=1)
        assert canonical_local.hour == 0 and canonical_local.minute == 0 and canonical_local.second == 0
        # The lower bound is exactly filing_date's own local midnight.
        lower_local = lower_bound.astimezone(sec_tz)
        assert lower_local.date() == filing_date
        assert lower_local.hour == 0 and lower_local.minute == 0 and lower_local.second == 0
