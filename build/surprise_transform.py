"""
Deterministic surprise-transform implementation — Section 1 of the spec.

This is the code the extraction prompt explicitly does NOT do itself: given
the raw observed/reference values an LLM extracted, and a transform_type
looked up from surprise_transform_registry for the event's type, compute
surprise_transformed. Never called with the LLM guessing the number.

Each function raises a clear ValueError on a bad/undefined case rather than
silently producing inf/nan/a huge misleading number — the whole point of
this module existing is that the old universal formula didn't do that.
"""

import math


def log_ratio(observed: float, reference: float) -> float:
    """For positive multiplicative variables (revenue, unit volume).
    Requires both values strictly positive — that's the domain this
    transform is valid for; anything else should use a different
    transform_type, not this one with a guard bolted on."""
    if reference <= 0 or observed <= 0:
        raise ValueError(
            f"log_ratio requires both values > 0 (got observed={observed}, "
            f"reference={reference}); this event's transform_type is misconfigured "
            "for a variable that can be zero or negative."
        )
    return math.log(observed / reference)


def pct_point_change(observed: float, reference: float) -> float:
    """For variables already expressed as a percentage/rate (margin, growth
    rate) — the change is just the arithmetic difference in percentage
    points, not a ratio of a ratio."""
    return observed - reference


def robust_scale_change(observed: float, reference: float, robust_scale: float) -> float:
    """For zero-crossing variables (e.g. EPS near breakeven), where dividing
    by the reference value itself is exactly the unsafe case. robust_scale
    should be a historical robust dispersion measure (e.g. a trailing MAD)
    for this metric, supplied by the caller from historical data — not
    derived from this single reference value."""
    if robust_scale is None or robust_scale <= 0:
        raise ValueError(
            "robust_scale_change requires a positive robust_scale from historical "
            "data; a missing or non-positive scale means there isn't enough "
            "history yet to standardize this surprise safely — treat it as "
            "missing, don't fall back to dividing by the reference value."
        )
    return (observed - reference) / robust_scale


def floored_pct_change(observed: float, reference: float, denominator_floor: float) -> float:
    """For positive levels where the reference can be small enough that a
    plain percentage change misleads (the $0.01 -> $0.05 EPS case: raw pct
    change is +400%, which is not a meaningful description of the surprise).
    Floors the denominator at a configured minimum instead."""
    if denominator_floor <= 0:
        raise ValueError("denominator_floor must be positive")
    denom = max(abs(reference), denominator_floor)
    return (observed - reference) / denom


TRANSFORMS = {
    "log_ratio": log_ratio,
    "pct_point_change": pct_point_change,
    "robust_scale_change": robust_scale_change,
    "floored_pct_change": floored_pct_change,
}


def compute_surprise_transformed(transform_type: str, observed: float, reference: float, parameters: dict) -> float:
    fn = TRANSFORMS.get(transform_type)
    if fn is None:
        raise ValueError(f"Unknown transform_type: {transform_type!r}")
    if transform_type == "log_ratio":
        return fn(observed, reference)
    if transform_type == "pct_point_change":
        return fn(observed, reference)
    if transform_type == "robust_scale_change":
        return fn(observed, reference, parameters.get("robust_scale"))
    if transform_type == "floored_pct_change":
        return fn(observed, reference, parameters.get("denominator_floor"))
    raise AssertionError("unreachable")
