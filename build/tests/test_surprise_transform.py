"""
Targets the bug the fourth review round caught: the old universal
(observed - reference) / |reference| formula explodes near a zero or
small reference value. These tests check the replacement handles exactly
the cases that broke the old formula, and refuses to silently produce a
misleading number where there isn't enough information to compute one.
"""
import math
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from surprise_transform import compute_surprise_transformed


def test_old_formula_would_have_exploded_here():
    """Sanity-check the exact example from the review: EPS guidance moving
    $0.01 -> $0.05. The naive (observed-reference)/|reference| gives +400%,
    which the review correctly called pathological. floored_pct_change with
    a sane floor gives a bounded, meaningful number instead."""
    naive = (0.05 - 0.01) / abs(0.01)
    assert naive == pytest.approx(4.0)  # confirms this WOULD have been the old (bad) behavior

    floored = compute_surprise_transformed(
        "floored_pct_change", observed=0.05, reference=0.01,
        parameters={"denominator_floor": 0.05},
    )
    assert floored == pytest.approx((0.05 - 0.01) / 0.05)
    assert abs(floored) < 1.0  # bounded, not a 400%-style artifact


def test_zero_crossing_case_does_not_divide_by_a_near_zero_reference():
    """EPS guidance moving from -$0.01 to +$0.01 crosses zero — dividing by
    the reference value directly is undefined-ish (reference is near zero
    AND changes sign). robust_scale_change avoids touching the reference
    value in the denominator at all."""
    result = compute_surprise_transformed(
        "robust_scale_change", observed=0.01, reference=-0.01,
        parameters={"robust_scale": 0.02},
    )
    assert result == pytest.approx((0.01 - (-0.01)) / 0.02)
    assert math.isfinite(result)


def test_robust_scale_change_refuses_to_guess_without_history():
    with pytest.raises(ValueError):
        compute_surprise_transformed(
            "robust_scale_change", observed=0.01, reference=-0.01,
            parameters={"robust_scale": None},
        )
    with pytest.raises(ValueError):
        compute_surprise_transformed(
            "robust_scale_change", observed=0.01, reference=-0.01,
            parameters={"robust_scale": 0},
        )


def test_log_ratio_rejects_nonpositive_inputs_instead_of_returning_nan():
    with pytest.raises(ValueError):
        compute_surprise_transformed("log_ratio", observed=100, reference=0, parameters={})
    with pytest.raises(ValueError):
        compute_surprise_transformed("log_ratio", observed=-5, reference=10, parameters={})


def test_log_ratio_normal_case():
    result = compute_surprise_transformed("log_ratio", observed=110, reference=100, parameters={})
    assert result == pytest.approx(math.log(1.1))


def test_pct_point_change_is_a_plain_difference_not_a_ratio():
    result = compute_surprise_transformed("pct_point_change", observed=0.15, reference=0.12, parameters={})
    assert result == pytest.approx(0.03)
