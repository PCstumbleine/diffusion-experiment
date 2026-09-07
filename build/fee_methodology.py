"""
Section 7f Implementation Spec, FINAL (specs/section7f-implementation-spec-final.md),
Section 3: the Robinhood US customer fee methodology -- CAT (both legs), SEC
fee (exit only, waived <= $500 principal), TAF (exit only, waived <= 50
shares, capped at $9.79).

Sourced from Robinhood's current customer-facing fee documentation (US
entity -- Robinhood Financial LLC / Robinhood Securities LLC, not the UK
entity), cross-checked against SEC and FINRA's own statutory rate
publications, and against the CAT NMS Plan's own fee-alert history directly.

Provenance note (frozen into the spec, not just a footnote): CAT customer
charges experienced a real US billing pause after the December 2025 invoice
(CAT NMS Plan fee alerts: last invoice under the prior regime billed
December 2025 for November activity, "no further monthly invoices until
further notice"). A new 2026 funding model resumed billing at $0.000001/share
(CAT Fee 2026-1) plus $0.000002/share (Historical CAT Assessment 1A) = the
same $0.000003/share Robinhood's current schedule shows for listed equities.
CAT is charged on both buys and sells (Robinhood's schedule: "applied to all
equity and options orders"), unlike the SEC fee and TAF, which are sells-only.

The $500/50-share Robinhood customer-pass-through exemptions apply to the
fractional shadow quantity exactly as they would to a real order of that
size -- this is a customer-fee-equivalent measurement, and the exemption
thresholds are part of what a Robinhood customer would actually be charged
for the equivalent trade, not a real-execution concept being smuggled in.
Given typical shadow notional ($1,000-3,000 reference equity x 5% =
$50-150), `principal <= $500` and `q <= 50` will both usually hold --
meaning fees will typically compute to $0.00 -- but a low-priced security
can still push q above 50 shares, so the exemption is checked, never
assumed.

One synthetic execution per shadow leg -- freeze this explicitly: each
shadow entry and each shadow exit is treated as one single execution for
fee purposes. The shadow-NBBO model has no partial-fill engine and none is
being built for fee calculation; TAF's per-trade cap is applied once per
leg, not subdivided.

Rounding -- each component independently, in order, then summed. Decimal
arithmetic throughout, never binary floating point.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING

import confirmatory_analysis


def cat_fee(q: Decimal) -> Decimal:
    raw = q * Decimal("0.000003")
    if raw < Decimal("0.01"):
        return Decimal("0.00")
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def sec_fee(principal: Decimal) -> Decimal:
    if principal <= Decimal("500"):
        return Decimal("0.00")
    raw = principal * Decimal("20.60") / Decimal("1000000")
    return raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)  # rounds UP


def taf_fee(q: Decimal) -> Decimal:
    if q <= Decimal("50"):
        return Decimal("0.00")
    raw = (q * Decimal("0.000195")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return min(raw, Decimal("9.79"))


# NOTE on the freeze-date suffix (judgment call -- see the implementation
# report): the spec's own Section 3 code block gives this literal value,
# placeholder included, and explicitly says the version string must be
# "bind[ing] to captured source artifacts... at the actual freeze date,"
# and Section 9 item 4 separately confirms "the version string's freeze-date
# suffix and source-snapshot binding still need an actual freeze date filled
# in before confirmatory collection." No such freeze date/source-artifact
# capture has actually happened -- inventing a plausible-looking date (e.g.
# "2026q3") would fabricate a preregistration fact this implementation pass
# has no authority to create. The literal, unfilled placeholder is used
# verbatim so this is unmistakably NOT yet a real frozen identifier.
FEE_METHOD_VERSION = "robinhood_us_customer_fees_<freeze-date>_v1"


def compute_expected_fees(outcome_id, shadow_notional: float, entry_ask: float, exit_bid: float) -> tuple[float, float]:
    """Matches the (outcome_id, shadow_notional, entry_ask, exit_bid) keyword
    contract assert_outcome_ready_for_confirmation already calls with
    (confirmatory_analysis.py's outcome-validation function). Recomputes q
    internally."""
    q = Decimal(str(shadow_notional)) / Decimal(str(entry_ask))
    entry_fee = cat_fee(q)
    exit_fee = sec_fee(q * Decimal(str(exit_bid))) + taf_fee(q) + cat_fee(q)
    return float(entry_fee), float(exit_fee)


# Wiring (Section 0/3): "the fee methodology... wired into the existing
# (unmodified) confirmatory_analysis.py Section 8 functions via those two
# names." confirmatory_analysis.py itself is untouched -- these two module-
# level attributes are set here, the moment this module is imported, exactly
# the same mechanism the existing test suite already uses
# (monkeypatch.setattr(ca, "compute_expected_fees", ...)) to override these
# same two None defaults.
confirmatory_analysis.FEE_METHOD_VERSION = FEE_METHOD_VERSION
confirmatory_analysis.compute_expected_fees = compute_expected_fees
