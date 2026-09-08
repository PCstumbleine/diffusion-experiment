"""
Historical Replay Phase 1A (specs/historical-replay-phase1a-implementation-spec-final.md),
Section 6's unit tests for public_time_provenance.resolve_relationship_public_time.
Pure-Python, no database involved at all.
"""
from datetime import datetime, timedelta, timezone

import pytest

from public_time_provenance import HistoricalPublicTimeError, resolve_relationship_public_time

CANONICAL = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
FALLBACK = datetime(2026, 3, 5, 9, 0, tzinfo=timezone.utc)
PRECISION = timedelta(minutes=3)


# ===========================================================================
# forward
# ===========================================================================

def test_forward_non_null_canonical_passes_through_unchanged():
    public_at, precision = resolve_relationship_public_time(
        canonical_first_public_at=CANONICAL,
        first_public_timestamp_precision=PRECISION,
        purpose="forward",
        fallback_observed_at=FALLBACK,
    )
    assert public_at == CANONICAL
    assert precision == PRECISION


def test_forward_null_canonical_fires_the_fallback():
    public_at, precision = resolve_relationship_public_time(
        canonical_first_public_at=None,
        first_public_timestamp_precision=PRECISION,
        purpose="forward",
        fallback_observed_at=FALLBACK,
    )
    assert public_at == FALLBACK
    assert precision == PRECISION  # precision is untouched by the canonical fallback


def test_forward_null_precision_passes_through_as_null_no_error():
    public_at, precision = resolve_relationship_public_time(
        canonical_first_public_at=CANONICAL,
        first_public_timestamp_precision=None,
        purpose="forward",
        fallback_observed_at=FALLBACK,
    )
    assert public_at == CANONICAL
    assert precision is None


def test_forward_negative_precision_raises_plain_value_error_not_historical_error():
    with pytest.raises(ValueError) as exc_info:
        resolve_relationship_public_time(
            canonical_first_public_at=CANONICAL,
            first_public_timestamp_precision=timedelta(minutes=-1),
            purpose="forward",
            fallback_observed_at=FALLBACK,
        )
    # Plain ValueError, NOT the HistoricalPublicTimeError subclass -- forward
    # mode's negative-precision rejection is not a "historical" error.
    assert not isinstance(exc_info.value, HistoricalPublicTimeError)
    assert type(exc_info.value) is ValueError


# ===========================================================================
# historical_replay
# ===========================================================================

def test_historical_replay_all_fields_present_passes_through_no_error():
    public_at, precision = resolve_relationship_public_time(
        canonical_first_public_at=CANONICAL,
        first_public_timestamp_precision=PRECISION,
        purpose="historical_replay",
        fallback_observed_at=FALLBACK,
    )
    assert public_at == CANONICAL
    assert precision == PRECISION


def test_historical_replay_null_canonical_raises():
    with pytest.raises(HistoricalPublicTimeError):
        resolve_relationship_public_time(
            canonical_first_public_at=None,
            first_public_timestamp_precision=PRECISION,
            purpose="historical_replay",
            fallback_observed_at=FALLBACK,
        )


def test_historical_replay_null_precision_raises():
    with pytest.raises(HistoricalPublicTimeError):
        resolve_relationship_public_time(
            canonical_first_public_at=CANONICAL,
            first_public_timestamp_precision=None,
            purpose="historical_replay",
            fallback_observed_at=FALLBACK,
        )


def test_historical_replay_negative_precision_raises_historical_error():
    with pytest.raises(HistoricalPublicTimeError):
        resolve_relationship_public_time(
            canonical_first_public_at=CANONICAL,
            first_public_timestamp_precision=timedelta(seconds=-1),
            purpose="historical_replay",
            fallback_observed_at=FALLBACK,
        )


def test_historical_replay_never_substitutes_fallback():
    """Belt-and-suspenders on the NULL-canonical case: confirms the
    exception fires rather than the fallback silently being used -- the
    exact hazard invariant 8 exists to prevent."""
    with pytest.raises(HistoricalPublicTimeError):
        resolve_relationship_public_time(
            canonical_first_public_at=None,
            first_public_timestamp_precision=PRECISION,
            purpose="historical_replay",
            fallback_observed_at=FALLBACK,
        )


# ===========================================================================
# Unknown purpose -- never falls through to the forward branch
# ===========================================================================

@pytest.mark.parametrize("bad_purpose", ["Forward", "historical-replay", "", None])
def test_unknown_purpose_raises_plain_value_error(bad_purpose):
    with pytest.raises(ValueError) as exc_info:
        resolve_relationship_public_time(
            canonical_first_public_at=None,  # would fire the forward fallback if this fell through
            first_public_timestamp_precision=PRECISION,
            purpose=bad_purpose,
            fallback_observed_at=FALLBACK,
        )
    assert not isinstance(exc_info.value, HistoricalPublicTimeError)
