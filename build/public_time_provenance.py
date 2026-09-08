"""Historical Replay Phase 1A: shared, purpose-aware resolution of a
relationship's (evidence_publicly_available_at, evidence_public_time_precision)
pair from its source document's (canonical_first_public_at,
first_public_timestamp_precision). Deliberately its own module, not part of
extraction_runner.py (forward-only orchestrator) or manual_resolve.py (the
dual-purpose caller) -- both import this; neither owns it.

Negative precision is rejected in BOTH purposes -- it is malformed data, not
a legitimate "extra unknown" beyond NULL. Only NULL canonical_first_public_at
(forward) and NULL precision (historical_replay) are purpose-conditional;
negative precision is an unconditional rejection."""

from __future__ import annotations

from datetime import datetime, timedelta


class HistoricalPublicTimeError(ValueError):
    """Raised when a historical_replay-purpose relationship write would
    require incomplete or invalid public-time provenance. Never caught and
    silently worked around -- the caller must fix the source data, or the
    write must not happen."""


def resolve_relationship_public_time(
    *,
    canonical_first_public_at: datetime | None,
    first_public_timestamp_precision: timedelta | None,
    purpose: str,
    fallback_observed_at: datetime,
) -> tuple[datetime, timedelta | None]:
    """Returns (evidence_publicly_available_at, evidence_public_time_precision)
    for a relationship write, given the (canonical, precision) pair read from
    its source raw_documents row.

    purpose="forward": retains the existing observation-time fallback
    (invariant 8's documented exception) -- a NULL canonical_first_public_at
    becomes fallback_observed_at; a NULL precision passes through unchanged.
    A NEGATIVE precision is rejected (raises ValueError) -- NULL is the
    sanctioned "unknown" case, negative is malformed data, and that
    distinction holds in forward mode too.

    purpose="historical_replay": fails closed. Never substitutes
    fallback_observed_at. Raises HistoricalPublicTimeError if
    canonical_first_public_at is NULL, or first_public_timestamp_precision is
    NULL, or precision is negative.

    Any other purpose value raises ValueError -- never silently treated as
    "forward". A malformed or unrecognized purpose must fail loudly, not
    accidentally inherit the historical-replay-prohibited fallback.
    """
    if purpose not in ("forward", "historical_replay"):
        raise ValueError(f"resolve_relationship_public_time: unknown purpose={purpose!r}")

    if first_public_timestamp_precision is not None and first_public_timestamp_precision < timedelta(0):
        if purpose == "historical_replay":
            raise HistoricalPublicTimeError(
                f"historical_replay relationship write with negative precision: "
                f"{first_public_timestamp_precision!r}"
            )
        raise ValueError(
            f"forward relationship write with negative precision: "
            f"{first_public_timestamp_precision!r}"
        )

    if purpose == "historical_replay":
        if canonical_first_public_at is None:
            raise HistoricalPublicTimeError(
                "historical_replay relationship write with NULL canonical_first_public_at"
            )
        if first_public_timestamp_precision is None:
            raise HistoricalPublicTimeError(
                "historical_replay relationship write with NULL first_public_timestamp_precision"
            )
        return canonical_first_public_at, first_public_timestamp_precision

    # purpose == "forward"
    return (
        canonical_first_public_at or fallback_observed_at,
        first_public_timestamp_precision,
    )
