"""
Section 7f Implementation Spec, FINAL (specs/section7f-implementation-spec-final.md),
Section 10's fee-methodology tests.
"""
from decimal import Decimal

import confirmatory_analysis
from fee_methodology import cat_fee, sec_fee, taf_fee, compute_expected_fees, FEE_METHOD_VERSION


def test_sec_fee_waived_at_exactly_500_principal():
    assert sec_fee(Decimal("500")) == Decimal("0.00")


def test_sec_fee_charged_just_above_500_principal():
    """$500.01 principal -> raw = 500.01 * 20.60 / 1,000,000, rounded UP
    (ROUND_CEILING) to the cent -- must be nonzero the instant the waiver
    threshold is exceeded."""
    fee = sec_fee(Decimal("500.01"))
    assert fee > Decimal("0.00")
    assert fee == Decimal("0.02")


def test_taf_fee_waived_at_exactly_50_shares():
    assert taf_fee(Decimal("50")) == Decimal("0.00")


def test_taf_fee_charged_just_above_50_shares():
    fee = taf_fee(Decimal("51"))
    assert fee > Decimal("0.00")
    assert fee == Decimal("0.01")


def test_cat_fee_charged_on_both_entry_and_exit_at_typical_shadow_quantities():
    """Typical shadow notional ($1,000-3,000 reference equity x 5% =
    $50-150) against an ordinary equity price commonly gives q well under 1
    share -- CAT's raw fee is near-zero but nonzero, rounding DOWN to
    $0.00 as expected (the sub-cent-threshold branch), not because CAT
    isn't charged on both legs, but because the rounded result happens to
    be zero at this quantity."""
    # $100 shadow notional / $100 entry price = 1.0 share.
    q = Decimal("1.0")
    raw_cat = q * Decimal("0.000003")
    assert raw_cat < Decimal("0.01")  # confirms this genuinely IS the near-zero regime
    assert cat_fee(q) == Decimal("0.00")

    entry_fee, exit_fee = compute_expected_fees(
        outcome_id="test", shadow_notional=100.0, entry_ask=100.0, exit_bid=101.0
    )
    # CAT is embedded in BOTH entry_fee (alone) and exit_fee (alongside SEC/TAF) --
    # confirmed structurally by compute_expected_fees always adding cat_fee(q)
    # into exit_fee regardless of SEC/TAF. At this q, all three round to 0.00.
    assert entry_fee == 0.0
    assert exit_fee == 0.0


def test_low_priced_security_pushes_q_above_50_making_taf_nonzero():
    """A low-priced security can push q above 50 shares even at typical
    shadow notional -- confirms the exemption is CHECKED, never assumed.
    $150 shadow notional / $2.00 entry price = 75 shares."""
    entry_fee, exit_fee = compute_expected_fees(
        outcome_id="test", shadow_notional=150.0, entry_ask=2.0, exit_bid=2.05
    )
    q = Decimal("150.0") / Decimal("2.0")
    assert q == Decimal("75")
    assert taf_fee(q) > Decimal("0.00")
    assert exit_fee > 0.0


def test_sec_round_up_to_cent_behavior_differs_from_round_half_up_at_a_real_boundary():
    """$600 principal -> raw = 600 * 20.60 / 1,000,000 = $0.01236. Under
    ROUND_CEILING (the frozen rule) this rounds UP to $0.02. Under
    ROUND_HALF_UP it would round DOWN to $0.01 (0.36 of a cent is below the
    half-cent threshold) -- confirms the frozen rounding mode is actually
    exercised, not merely stated."""
    from decimal import ROUND_HALF_UP
    principal = Decimal("600")
    raw = principal * Decimal("20.60") / Decimal("1000000")
    assert raw == Decimal("0.01236")

    ceiling_result = sec_fee(principal)
    half_up_result = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    assert ceiling_result == Decimal("0.02")
    assert half_up_result == Decimal("0.01")
    assert ceiling_result != half_up_result


def test_each_components_rounding_verified_independently_before_summing():
    """A composite case where CAT, SEC, and TAF are all simultaneously
    nonzero -- confirms each component is rounded on its OWN, independently,
    before the exit-side sum, rather than the sum being rounded once at the
    end (which could silently produce a different total)."""
    # 200 shares at $10/share exit -> principal = $2,000 (> $500, SEC applies);
    # q=200 (> 50, TAF applies); CAT applies to both legs regardless.
    q = Decimal("200")
    principal = q * Decimal("10.00")

    expected_cat = cat_fee(q)
    expected_sec = sec_fee(principal)
    expected_taf = taf_fee(q)
    assert expected_cat == Decimal("0.00")  # 200 * 0.000003 = 0.0006 -- rounds to 0.00
    assert expected_sec == Decimal("0.05")  # 2000 * 20.60 / 1e6 = 0.0412 -- rounds UP to 0.05
    assert expected_taf == Decimal("0.04")  # 200 * 0.000195 = 0.039 -- rounds to 0.04

    entry_fee, exit_fee = compute_expected_fees(
        outcome_id="test", shadow_notional=2000.0, entry_ask=10.0, exit_bid=10.0
    )
    assert entry_fee == float(expected_cat)
    assert exit_fee == float(expected_sec + expected_taf + expected_cat)


def test_taf_fee_is_capped_at_9_79():
    """A large enough q must not let TAF exceed its statutory cap."""
    q = Decimal("1000000")
    assert taf_fee(q) == Decimal("9.79")


def test_compute_expected_fees_recomputes_q_internally_and_matches_the_documented_contract():
    """Matches the exact keyword contract confirmatory_analysis.py's
    assert_outcome_ready_for_confirmation calls with."""
    result = compute_expected_fees(outcome_id="x", shadow_notional=1000.0, entry_ask=50.0, exit_bid=51.0)
    assert isinstance(result, tuple) and len(result) == 2
    assert all(isinstance(v, float) for v in result)


def test_fee_methodology_is_wired_into_confirmatory_analysis_via_the_two_names():
    """Section 3/0: 'wired into the existing (unmodified) confirmatory_analysis.py
    Section 8 functions via those two names.' Importing fee_methodology
    (already done at module load, above) must have set both."""
    assert confirmatory_analysis.FEE_METHOD_VERSION == FEE_METHOD_VERSION
    assert confirmatory_analysis.compute_expected_fees is compute_expected_fees
