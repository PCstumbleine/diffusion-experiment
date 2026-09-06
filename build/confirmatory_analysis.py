"""
Section 8 Implementation Spec, FINAL (specs/section8-implementation-spec-final.md)
-- self-contained and authoritative, settled after eight rounds of review.
This module implements it exactly: the catalyst-wide trade/abstention
invariant (Sections 3-4), the ranking tie-break (Section 5), abstention's
contribution to the statistical comparison (Section 6), the shadow
outcome/execution-pricing contract (Section 7), and the global
confirmatory-configuration preflight (Section 8). It does NOT touch the arm
structure, the underreaction estimator, the bitemporal model, or Section
14's operating plan -- all of that is settled and out of scope here (spec
Section 0).

No frozen parameter, formula, or rule below is re-derived, re-tuned, or
"improved" based on anything discovered while implementing this (spec
Section 11).
"""

from __future__ import annotations

import math
from collections import namedtuple
from dataclasses import dataclass
from decimal import Decimal

from candidate_coverage import assert_full_coverage
from entity_resolution import normalize_entity_name

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidStableEntityKeyError(Exception):
    """Section 5: an entity's cik/legal_name can't produce a stable ranking
    key (malformed cik, or a null cik whose legal_name normalizes empty)."""


class UnresolvedRankingTieError(Exception):
    """Section 4 item 1 / Section 5: two distinct candidates share an
    identical (score, stable_entity_sort_key) pair -- the tie-break did not
    produce a total order over the catalyst-wide candidate union. Raised
    for ANY such tie anywhere in the ranking, not only at the position that
    would determine selection."""


class InvalidCandidateScoreError(Exception):
    """Section 2: a candidate score that is NULL, NaN, or +/-Infinity is
    invalid input -- rejected as a coverage/model-output failure BEFORE
    ranking, never eligible for ranking and never treated as an
    abstention."""


class MissingDecisionError(Exception):
    """assert_trade_abstention_invariant's documented precondition
    (assert_full_coverage has already passed for this (model_id,
    model_version) across every current event_version of this catalyst)
    was violated -- an eligible candidate has no decision row at all from
    this (model_id, model_version). Fails loudly rather than silently
    under-counting the catalyst-wide candidate union."""


class TradeAbstentionInvariantError(Exception):
    """Section 3/4: the catalyst-wide trade/abstention row-level contract
    is violated -- a stored rank sequence that doesn't match the
    recomputed dense 1..N ranking (gapped, duplicated, or otherwise
    incorrect), more than one selected/abstained row, a rank>1 row with
    selected/abstained set, or a rank=1 row whose selected/abstained value
    contradicts its score sign."""


class ConfirmatoryConfigurationIncompleteError(Exception):
    """Section 8/9: one or more of the six deferred parameters/
    requirements is unset or unimplemented. "Not configured" is its own
    distinct refusal, never a convenient default chosen by whoever
    implements this."""


class OutcomeIntegrityError(Exception):
    """Section 7e/7f: an arm_outcomes row fails a hard-failure integrity
    check (methodology/version/domain/fee/recomputation reason) -- an
    implementation defect, never "this catalyst has no data"."""


class QuoteUnobservableError(Exception):
    """Section 7e/7f: a traded arm's outcome has no quote (or insufficient
    quote size) satisfying the lookup-window/staleness/session/validity
    rules on an otherwise-capable, approved provider -- a genuinely
    unobservable market outcome, a legitimate per-catalyst exclusion, never
    a code defect. A distinct exception type from OutcomeIntegrityError so
    a caller (the not-yet-built Section 7f statistical builder) can tell
    the two apart."""


# ---------------------------------------------------------------------------
# Section 2: frozen parameters (never re-tuned, never derived from anything
# discovered while implementing this)
# ---------------------------------------------------------------------------

H = "1day"
DELTA = 0.005
INFERENCE_METHOD = "catalyst_clustered_test"
CONFIRMATORY_N_BOOTSTRAP = 10000
CONFIRMATORY_BOOTSTRAP_SEED = 20260906
NOTIONAL_FRACTION = 0.05
MAX_POSITIONS_PER_CATALYST = 1
RETURN_METHOD_VERSION = "shadow_nbbo_log_v1"

# ---------------------------------------------------------------------------
# Section 9: six deferred parameters/requirements -- ALL fail-closed. None
# of these has a made-up default; every one is unset/False until a real
# preregistration decision sets it explicitly, before confirmatory
# collection begins. assert_outcome_ready_for_confirmation and
# assert_confirmatory_configuration_complete both refuse confirmatory
# validity while any of these is unset -- see both functions below.
# ---------------------------------------------------------------------------

MAX_QUOTE_LOOKUP_DELAY_SECONDS = None       # deferred #1
MAX_QUOTE_STALENESS_SECONDS = None          # deferred #2
CONFIRMATORY_DATA_PROVIDER = None           # deferred #3 -- a single frozen value, not an allowlist
CONFIRMATORY_PROVIDER_SIZE_CAPABLE = False  # deferred #3 -- the provider's normalized-to-shares capability
FEE_METHOD_VERSION = None                   # deferred #4
compute_expected_fees = None                # deferred #4 -- callable(**ctx) -> (entry_fee, exit_fee)
STOP_RULE_PROVENANCE_AVAILABLE = False      # deferred #5
REFERENCE_EQUITY_USD = None                 # deferred #6
# Provenance of REFERENCE_EQUITY_USD (Section 7d) -- recorded explicitly,
# never left implicit. One of "actual_funded_equity" or
# "preregistered_placeholder".
REFERENCE_EQUITY_USD_PROVENANCE = None
# Only consulted when REFERENCE_EQUITY_USD_PROVENANCE == "actual_funded_equity"
# and the real funded amount falls outside Section 14's $1,000-3,000
# envelope -- Section 7d: "that is a discrepancy... to resolve explicitly...
# never silently accepted merely because it's the real number." Set True
# only after that explicit resolution has actually happened.
REFERENCE_EQUITY_USD_DISCREPANCY_ACKNOWLEDGED = False


# ---------------------------------------------------------------------------
# Section 4: get_current_event_versions_for_catalyst
# ---------------------------------------------------------------------------

def get_current_event_versions_for_catalyst(conn, catalyst_id):
    """All event_versions where superseded_by IS NULL, across every
    canonical_event under this catalyst_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ev.event_version_id
            FROM event_versions ev
            JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id
            WHERE ce.catalyst_id = %s AND ev.superseded_by IS NULL
            """,
            (catalyst_id,),
        )
        return [row[0] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Section 5: stable_entity_sort_key
# ---------------------------------------------------------------------------

_EntityForSortKey = namedtuple("_EntityForSortKey", ["entity_id", "cik", "legal_name"])


def stable_entity_sort_key(entity) -> str:
    """Deterministic across independent from-scratch DB rebuilds, unlike
    entity_id/candidate_id/canonical_event_id/event_version_id. Prefixed so
    a CIK-keyed and a name-keyed entity can never collide. Fails closed
    rather than silently producing a degenerate key: cik IS NULL is the only
    legitimate name-fallback case -- a non-null-but-malformed cik (blank,
    whitespace, non-numeric, zero, or too many digits) is a data-quality
    error, never coerced into a fake key. NOTE: canonicalize/validate BEFORE
    zfill -- zfill-ing an empty or whitespace string first produces a
    fake-valid all-zero string that would incorrectly pass isdigit()/length
    checks performed afterward."""
    if entity.cik is not None:
        raw_cik = str(entity.cik).strip()
        if not raw_cik or not raw_cik.isdigit() or len(raw_cik) > 10 or int(raw_cik) <= 0:
            raise InvalidStableEntityKeyError(
                f"entity {entity.entity_id} has malformed cik: {entity.cik!r}"
            )
        return f"CIK:{raw_cik.zfill(10)}"
    normalized = normalize_entity_name(entity.legal_name)
    if not normalized:
        raise InvalidStableEntityKeyError(
            f"entity {entity.entity_id} has no cik and legal_name {entity.legal_name!r} "
            "normalizes to an empty string -- cannot construct a stable ranking key"
        )
    return f"NAME:{normalized}"


def _is_invalid_score(score) -> bool:
    """NULL, NaN, or +/-Infinity -- Section 2's explicit invalid-input rule.
    Postgres NUMERIC can store the literal NaN/Infinity, so this is an
    explicit application-level check, not something the column type
    prevents."""
    if score is None:
        return True
    if isinstance(score, Decimal):
        return score.is_nan() or score.is_infinite()
    try:
        as_float = float(score)
    except (TypeError, ValueError):
        return True
    return math.isnan(as_float) or math.isinf(as_float)


# ---------------------------------------------------------------------------
# Section 4: catalyst-wide invariant functions
# ---------------------------------------------------------------------------

NO_ELIGIBLE_CANDIDATES = "NO_ELIGIBLE_CANDIDATES"
TRADED = "TRADED"
ABSTAINED = "ABSTAINED"


@dataclass(frozen=True)
class CatalystDecisionResult:
    """status is one of NO_ELIGIBLE_CANDIDATES, TRADED, or ABSTAINED
    (module-level constants above). selected_candidate_id is set only when
    status == TRADED."""

    status: str
    selected_candidate_id: object | None = None


def assert_trade_abstention_invariant(conn, catalyst_id, model_id, model_version):
    """Raises if the CATALYST-WIDE trade/abstention invariant is violated,
    over the union of eligible candidates across every current event_version
    under this catalyst -- not a single event_version. Call only after
    assert_full_coverage has passed for every one of those event_versions
    for this (model_id, model_version).

    If the union of eligible candidates is EMPTY (N=0): this is neither a
    model abstention (nothing existed to score) nor a coverage failure
    (coverage over an empty set is vacuously complete -- confirmed against
    the real find_incomplete_coverage, which returns [] when there are no
    eligible candidates). Returns a distinct NO_ELIGIBLE_CANDIDATES result
    rather than raising or fabricating a trade/abstain row. Callers must NOT
    count this catalyst as an A/G policy observation, must NOT include it in
    the confirmatory D_c set, and must NOT include it in the abstention-rate
    denominator -- report it separately as
    catalysts_with_zero_eligible_candidates.

    Otherwise (N >= 1), enforces over the union of eligible candidates:
      1. Dense 1..N ranking, no duplicates or gaps, using the tie-break in
         Section 5 (stable_entity_sort_key). ANY unresolved exact tie
         anywhere in this ranking (not only at the position that would
         determine selection) raises UnresolvedRankingTieError -- Section
         3's promise of a dense, reproducible ranking is about the whole
         ordering, not just about who gets selected.
      2. Exactly one of: trade (rank=1 row selected=true, zero abstained=true
         anywhere in the set) or abstain (zero selected=true, rank=1 row
         abstained=true).
      3. Every rank > 1 row: selected=false AND abstained=false.
      4. rank=1's selected/abstained value matches its score sign
         (Section 3).
    """
    event_version_ids = get_current_event_versions_for_catalyst(conn, catalyst_id)
    if not event_version_ids:
        return CatalystDecisionResult(status=NO_ELIGIBLE_CANDIDATES)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cs.candidate_id, cs.entity_id, e.cik, e.legal_name
            FROM candidate_signals cs
            JOIN entities e ON e.entity_id = cs.entity_id
            WHERE cs.event_version_id = ANY(%s::uuid[]) AND cs.eligibility_status = 'eligible'
            """,
            (event_version_ids,),
        )
        eligible = cur.fetchall()

    if not eligible:
        return CatalystDecisionResult(status=NO_ELIGIBLE_CANDIDATES)

    candidate_ids = [row[0] for row in eligible]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT candidate_id, score, rank, selected, abstained
            FROM model_candidate_decisions
            WHERE candidate_id = ANY(%s::uuid[]) AND model_id = %s AND model_version = %s
            """,
            (candidate_ids, model_id, model_version),
        )
        decisions_by_candidate = {row[0]: row[1:] for row in cur.fetchall()}

    missing = [cid for cid in candidate_ids if cid not in decisions_by_candidate]
    if missing:
        raise MissingDecisionError(
            f"catalyst {catalyst_id}: {len(missing)} eligible candidate(s) have no decision from "
            f"(model_id={model_id!r}, model_version={model_version!r}) -- "
            "assert_trade_abstention_invariant must only be called after assert_full_coverage has "
            f"passed for this (model_id, model_version): {missing}"
        )

    # Validate every score BEFORE ranking (Section 2 / Section 10: "score =
    # NaN/Infinity ... rejected before ranking", checked for every candidate,
    # not only whichever would otherwise land at rank 1).
    for candidate_id in candidate_ids:
        score = decisions_by_candidate[candidate_id][0]
        if _is_invalid_score(score):
            raise InvalidCandidateScoreError(
                f"catalyst {catalyst_id}, candidate {candidate_id}: score={score!r} is NULL/NaN/"
                "Infinity -- invalid input, never eligible for ranking or treated as an abstention."
            )

    # Compute the authoritative dense ranking: score DESC, stable_entity_sort_key ASC.
    scored = []
    for candidate_id, entity_id, cik, legal_name in eligible:
        score, stored_rank, selected, abstained = decisions_by_candidate[candidate_id]
        sort_key = stable_entity_sort_key(_EntityForSortKey(entity_id, cik, legal_name))
        scored.append((candidate_id, score, sort_key, stored_rank, selected, abstained))

    scored.sort(key=lambda row: (-row[1], row[2]))

    for i in range(1, len(scored)):
        prev, this_row = scored[i - 1], scored[i]
        if prev[1] == this_row[1] and prev[2] == this_row[2]:
            raise UnresolvedRankingTieError(
                f"catalyst {catalyst_id}: candidates {prev[0]} and {this_row[0]} share an identical "
                f"(score={prev[1]!r}, stable_entity_sort_key={prev[2]!r}) -- the tie-break did not "
                "produce a total order over the catalyst-wide candidate union."
            )

    trade_count = 0
    abstain_count = 0
    selected_candidate_id = None

    for zero_based_index, (candidate_id, score, _sort_key, stored_rank, selected, abstained) in enumerate(scored):
        computed_rank = zero_based_index + 1
        if stored_rank != computed_rank:
            raise TradeAbstentionInvariantError(
                f"catalyst {catalyst_id}, candidate {candidate_id}: stored rank={stored_rank!r} does "
                f"not match the recomputed dense rank={computed_rank} (score DESC, "
                "stable_entity_sort_key ASC) -- a gapped, duplicated, or otherwise incorrect stored "
                "rank sequence."
            )
        if computed_rank == 1:
            if score > 0:
                if not (selected is True and abstained is False):
                    raise TradeAbstentionInvariantError(
                        f"catalyst {catalyst_id}, candidate {candidate_id}: rank=1, score={score!r} > 0 "
                        f"must be selected=true/abstained=false, got selected={selected!r}, "
                        f"abstained={abstained!r}."
                    )
                trade_count += 1
                selected_candidate_id = candidate_id
            else:
                if not (selected is False and abstained is True):
                    raise TradeAbstentionInvariantError(
                        f"catalyst {catalyst_id}, candidate {candidate_id}: rank=1, score={score!r} <= 0 "
                        f"must be selected=false/abstained=true, got selected={selected!r}, "
                        f"abstained={abstained!r}."
                    )
                abstain_count += 1
        else:
            if selected is not False or abstained is not False:
                raise TradeAbstentionInvariantError(
                    f"catalyst {catalyst_id}, candidate {candidate_id}: rank={computed_rank} > 1 must "
                    f"have selected=false and abstained=false, got selected={selected!r}, "
                    f"abstained={abstained!r}."
                )

    if trade_count == 1 and abstain_count == 0:
        return CatalystDecisionResult(status=TRADED, selected_candidate_id=selected_candidate_id)
    if trade_count == 0 and abstain_count == 1:
        return CatalystDecisionResult(status=ABSTAINED)
    # Unreachable given the rank>1 check above (only rank=1 can ever be
    # non-(false,false), and the rank=1 branch always assigns to exactly one
    # of trade_count/abstain_count) -- a defensive confirmation, not new logic.
    raise TradeAbstentionInvariantError(
        f"catalyst {catalyst_id}: expected exactly one of trade/abstain at rank=1, got "
        f"trade_count={trade_count}, abstain_count={abstain_count}."
    )


def assert_decision_set_ready_for_comparison(conn, catalyst_id, model_specs):
    """model_specs: list of (model_id, model_version) tuples -- the two
    identities are independent, never assumed to share a version string.
    For each pair: calls assert_full_coverage for every current
    event_version under catalyst_id, then assert_trade_abstention_invariant.
    Propagates the NO_ELIGIBLE_CANDIDATES result rather than treating it as
    success or failure -- callers (Section 5f) decide what to do with it.

    Returns NO_ELIGIBLE_CANDIDATES (the module constant) if the union of
    eligible candidates for this catalyst is empty, otherwise a dict mapping
    each (model_id, model_version) pair to its CatalystDecisionResult."""
    results = {}
    for model_id, model_version in model_specs:
        for event_version_id in get_current_event_versions_for_catalyst(conn, catalyst_id):
            assert_full_coverage(conn, event_version_id, [model_id], model_version)
        results[(model_id, model_version)] = assert_trade_abstention_invariant(
            conn, catalyst_id, model_id, model_version
        )

    if any(result.status == NO_ELIGIBLE_CANDIDATES for result in results.values()):
        return NO_ELIGIBLE_CANDIDATES
    return results


# ---------------------------------------------------------------------------
# Section 7e/7d: outcome validation
# ---------------------------------------------------------------------------

def _floats_close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def _require_global_confirmatory_config_for_outcome(outcome_id):
    """Every deferred item Section 7e's checklist actually depends on,
    checked independently of assert_confirmatory_configuration_complete --
    this function must refuse on its own, not merely trust a caller ran the
    preflight first (Section 9: both functions must explicitly refuse)."""
    if not STOP_RULE_PROVENANCE_AVAILABLE:
        raise ConfirmatoryConfigurationIncompleteError(
            f"outcome {outcome_id}: no traded A/G outcome is confirmatory-valid until the "
            "deterministic protective-stop trigger/fill rule and its durable provenance exist "
            "(Section 7b) -- this applies to exit_reason='horizon' exactly as much as "
            "'protective_stop'."
        )
    if FEE_METHOD_VERSION is None or compute_expected_fees is None:
        raise ConfirmatoryConfigurationIncompleteError(
            f"outcome {outcome_id}: fee_method_version and its fee-computation implementation are "
            "not both configured."
        )
    if CONFIRMATORY_DATA_PROVIDER is None or not CONFIRMATORY_PROVIDER_SIZE_CAPABLE:
        raise ConfirmatoryConfigurationIncompleteError(
            f"outcome {outcome_id}: CONFIRMATORY_DATA_PROVIDER is not frozen to a single, "
            "size-capable provider."
        )
    if MAX_QUOTE_LOOKUP_DELAY_SECONDS is None or MAX_QUOTE_STALENESS_SECONDS is None:
        raise ConfirmatoryConfigurationIncompleteError(
            f"outcome {outcome_id}: MAX_QUOTE_LOOKUP_DELAY_SECONDS/MAX_QUOTE_STALENESS_SECONDS are "
            "not both frozen."
        )
    if REFERENCE_EQUITY_USD is None or not (REFERENCE_EQUITY_USD > 0):
        raise ConfirmatoryConfigurationIncompleteError(
            f"outcome {outcome_id}: REFERENCE_EQUITY_USD is not frozen to a finite value > 0."
        )


def assert_outcome_ready_for_confirmation(conn, outcome_id):
    """Raises if this arm_outcomes row is not valid for the confirmatory
    A-vs-G statistical comparison. Only ever called for an arm that traded."""
    _require_global_confirmatory_config_for_outcome(outcome_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                o.entry_price_source, o.exit_price_source, o.entry_fee, o.exit_fee,
                o.return_method_version, o.fee_method_version, o.exit_reason,
                o.return_gross, o.return_net_of_costs, o.exit_timestamp, o.exit_quote_snapshot_id,
                e.entry_timestamp, e.entry_quote_snapshot_id
            FROM arm_outcomes o
            JOIN arm_entries e ON e.entry_id = o.entry_id
            WHERE o.outcome_id = %s
            """,
            (outcome_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise OutcomeIntegrityError(f"outcome {outcome_id} does not exist")
    (entry_price_source, exit_price_source, entry_fee, exit_fee,
     return_method_version, fee_method_version, exit_reason,
     stored_return_gross, stored_return_net, exit_timestamp, exit_quote_snapshot_id,
     entry_timestamp, entry_quote_snapshot_id) = row

    if entry_price_source != "nbbo_side_proxy" or exit_price_source != "nbbo_side_proxy":
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: entry_price_source={entry_price_source!r}/"
            f"exit_price_source={exit_price_source!r} -- only 'nbbo_side_proxy' is an approved "
            "confirmatory value."
        )
    if return_method_version != RETURN_METHOD_VERSION:
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: return_method_version={return_method_version!r} != "
            f"{RETURN_METHOD_VERSION!r}."
        )
    if fee_method_version != FEE_METHOD_VERSION:
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: fee_method_version={fee_method_version!r} != the frozen "
            f"{FEE_METHOD_VERSION!r}."
        )
    if exit_reason not in ("horizon", "protective_stop"):
        raise OutcomeIntegrityError(f"outcome {outcome_id}: exit_reason={exit_reason!r} is invalid.")

    def _fetch_snapshot(snapshot_id, label):
        if snapshot_id is None:
            raise QuoteUnobservableError(
                f"outcome {outcome_id}: no {label} quote snapshot is referenced -- unobservable."
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bid, ask, bid_size, ask_size, quote_timestamp, data_provider, "
                "trading_session, staleness_seconds FROM quote_snapshots WHERE quote_snapshot_id = %s",
                (snapshot_id,),
            )
            snapshot_row = cur.fetchone()
        if snapshot_row is None:
            raise OutcomeIntegrityError(
                f"outcome {outcome_id}: {label} quote snapshot {snapshot_id} does not exist"
            )
        return snapshot_row

    (entry_bid, entry_ask, entry_bid_size, entry_ask_size, entry_qts, entry_provider,
     entry_session, entry_staleness) = _fetch_snapshot(entry_quote_snapshot_id, "entry")
    (exit_bid, exit_ask, exit_bid_size, exit_ask_size, exit_qts, exit_provider,
     exit_session, exit_staleness) = _fetch_snapshot(exit_quote_snapshot_id, "exit")

    for label, session, bid, ask, provider, quote_ts, staleness, target_ts in (
        ("entry", entry_session, entry_bid, entry_ask, entry_provider, entry_qts, entry_staleness, entry_timestamp),
        ("exit", exit_session, exit_bid, exit_ask, exit_provider, exit_qts, exit_staleness, exit_timestamp),
    ):
        if session != "regular":
            raise QuoteUnobservableError(
                f"outcome {outcome_id}: {label} quote trading_session={session!r} != 'regular'."
            )
        if bid is None or ask is None or not (bid > 0) or not (ask > 0):
            raise QuoteUnobservableError(
                f"outcome {outcome_id}: {label} quote bid/ask invalid (bid={bid!r}, ask={ask!r})."
            )
        if ask < bid:
            raise QuoteUnobservableError(
                f"outcome {outcome_id}: {label} quote is crossed (ask={ask!r} < bid={bid!r}) -- a "
                "data-quality exclusion, not a policy edge case."
            )
        if provider != CONFIRMATORY_DATA_PROVIDER:
            raise QuoteUnobservableError(
                f"outcome {outcome_id}: {label} quote data_provider={provider!r} != the frozen "
                f"{CONFIRMATORY_DATA_PROVIDER!r}."
            )
        lookup_delay = abs((quote_ts - target_ts).total_seconds())
        if lookup_delay > MAX_QUOTE_LOOKUP_DELAY_SECONDS:
            raise QuoteUnobservableError(
                f"outcome {outcome_id}: {label} quote lookup delay {lookup_delay}s exceeds "
                f"MAX_QUOTE_LOOKUP_DELAY_SECONDS={MAX_QUOTE_LOOKUP_DELAY_SECONDS}."
            )
        if staleness is None or staleness > MAX_QUOTE_STALENESS_SECONDS:
            raise QuoteUnobservableError(
                f"outcome {outcome_id}: {label} quote staleness={staleness!r} exceeds "
                f"MAX_QUOTE_STALENESS_SECONDS={MAX_QUOTE_STALENESS_SECONDS}."
            )

    shadow_notional = REFERENCE_EQUITY_USD * NOTIONAL_FRACTION
    if not (shadow_notional > 0):
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: SHADOW_NOTIONAL_USD={shadow_notional!r} is not > 0."
        )

    entry_ask_f, exit_bid_f = float(entry_ask), float(exit_bid)
    q = shadow_notional / entry_ask_f

    # Quote size covers the shadow quantity, not merely > 0 (Section 7e) --
    # only enforced where the provider actually supplies size data.
    if entry_ask_size is not None and float(entry_ask_size) < q:
        raise QuoteUnobservableError(
            f"outcome {outcome_id}: entry ask_size={entry_ask_size!r} < required q={q!r}."
        )
    if exit_bid_size is not None and float(exit_bid_size) < q:
        raise QuoteUnobservableError(
            f"outcome {outcome_id}: exit bid_size={exit_bid_size!r} < required q={q!r}."
        )

    expected_entry_fee, expected_exit_fee = compute_expected_fees(
        outcome_id=outcome_id, shadow_notional=shadow_notional,
        entry_ask=entry_ask_f, exit_bid=exit_bid_f,
    )
    if not _floats_close(float(entry_fee), expected_entry_fee) or \
       not _floats_close(float(exit_fee), expected_exit_fee):
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: stored entry_fee/exit_fee ({entry_fee!r}/{exit_fee!r}) do not "
            f"match the frozen fee methodology's recomputed values ({expected_entry_fee!r}/"
            f"{expected_exit_fee!r}) -- a wrong-but-internally-consistent fee pair must not pass "
            "merely because the return recomputes consistently from it."
        )

    c_entry = shadow_notional + float(entry_fee)
    c_exit = q * exit_bid_f - float(exit_fee)
    if not (c_entry > 0) or not (c_exit > 0):
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: C_entry={c_entry!r}, C_exit={c_exit!r} -- both must be > 0 "
            "before evaluating either logarithm."
        )

    expected_return_gross = math.log(exit_bid_f / entry_ask_f)
    expected_return_net = math.log(c_exit / c_entry)
    if not math.isfinite(expected_return_gross) or not math.isfinite(expected_return_net):
        raise OutcomeIntegrityError(f"outcome {outcome_id}: recomputed return is not finite.")

    if stored_return_gross is None or stored_return_net is None:
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: stored return_gross/return_net_of_costs is NULL."
        )
    stored_return_gross_f, stored_return_net_f = float(stored_return_gross), float(stored_return_net)
    if not math.isfinite(stored_return_gross_f) or not math.isfinite(stored_return_net_f):
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: stored return_gross/return_net_of_costs is not finite."
        )

    if not _floats_close(stored_return_gross_f, expected_return_gross) or \
       not _floats_close(stored_return_net_f, expected_return_net):
        raise OutcomeIntegrityError(
            f"outcome {outcome_id}: stored return_gross/return_net_of_costs "
            f"({stored_return_gross!r}/{stored_return_net!r}) do not match the values recomputed "
            f"from the referenced snapshots per the Section 7d formula "
            f"({expected_return_gross!r}/{expected_return_net!r}) -- inspecting only the referenced "
            "snapshot IDs cannot prove the stored numbers were actually derived from them."
        )


# ---------------------------------------------------------------------------
# Section 8: global confirmatory-configuration preflight
# ---------------------------------------------------------------------------

def assert_confirmatory_configuration_complete():
    """Called once, before building any confirmatory dataset. An unset
    global configuration aborts the entire confirmatory build outright -- it
    is not a per-catalyst exclusion, since the methodology being incomplete
    is true regardless of which catalysts happen to need market data."""
    missing = []

    if MAX_QUOTE_LOOKUP_DELAY_SECONDS is None:
        missing.append("MAX_QUOTE_LOOKUP_DELAY_SECONDS is not frozen")
    if MAX_QUOTE_STALENESS_SECONDS is None:
        missing.append("MAX_QUOTE_STALENESS_SECONDS is not frozen")

    if CONFIRMATORY_DATA_PROVIDER is None:
        missing.append("CONFIRMATORY_DATA_PROVIDER is not frozen to a single value")
    elif not CONFIRMATORY_PROVIDER_SIZE_CAPABLE:
        missing.append(
            f"provider {CONFIRMATORY_DATA_PROVIDER!r} has not established trustworthy, "
            "normalized-to-shares bid/ask-size semantics -- the provider configuration is "
            "incomplete, full stop (Section 7e)"
        )

    if FEE_METHOD_VERSION is None or compute_expected_fees is None:
        missing.append(
            "fee_method_version and its fee-computation implementation do not both exist "
            "(one methodology bundle, not two separate items)"
        )

    if not STOP_RULE_PROVENANCE_AVAILABLE:
        missing.append(
            "the deterministic protective-stop trigger/fill rule and its durable "
            "trigger-provenance mechanism do not exist"
        )

    if RETURN_METHOD_VERSION != "shadow_nbbo_log_v1":
        missing.append(f"return_method_version={RETURN_METHOD_VERSION!r} != 'shadow_nbbo_log_v1'")

    if REFERENCE_EQUITY_USD is None:
        missing.append("REFERENCE_EQUITY_USD is not frozen")
    else:
        if not (math.isfinite(REFERENCE_EQUITY_USD) and REFERENCE_EQUITY_USD > 0):
            missing.append(f"REFERENCE_EQUITY_USD={REFERENCE_EQUITY_USD!r} is not finite and > 0")
        if NOTIONAL_FRACTION != 0.05:
            missing.append(f"NOTIONAL_FRACTION={NOTIONAL_FRACTION!r} != 0.05 exactly")
        shadow_notional = REFERENCE_EQUITY_USD * NOTIONAL_FRACTION
        if not (shadow_notional > 0):
            missing.append(f"SHADOW_NOTIONAL_USD={shadow_notional!r} is not > 0")
        if REFERENCE_EQUITY_USD_PROVENANCE not in ("actual_funded_equity", "preregistered_placeholder"):
            missing.append(
                f"REFERENCE_EQUITY_USD_PROVENANCE={REFERENCE_EQUITY_USD_PROVENANCE!r} must be "
                "recorded explicitly as one of 'actual_funded_equity' or 'preregistered_placeholder'"
            )
        elif REFERENCE_EQUITY_USD_PROVENANCE == "preregistered_placeholder":
            if not (1000 <= REFERENCE_EQUITY_USD <= 3000):
                missing.append(
                    f"REFERENCE_EQUITY_USD={REFERENCE_EQUITY_USD!r} is a preregistered placeholder "
                    "outside Section 14's $1,000-3,000 envelope"
                )
        elif REFERENCE_EQUITY_USD_PROVENANCE == "actual_funded_equity":
            if not (1000 <= REFERENCE_EQUITY_USD <= 3000) and not REFERENCE_EQUITY_USD_DISCREPANCY_ACKNOWLEDGED:
                missing.append(
                    f"REFERENCE_EQUITY_USD={REFERENCE_EQUITY_USD!r} (actual funded equity) falls "
                    "outside the $1,000-3,000 envelope and this discrepancy has not been explicitly "
                    "acknowledged (REFERENCE_EQUITY_USD_DISCREPANCY_ACKNOWLEDGED) -- never silently "
                    "accepted merely because it's the real number"
                )

    if H != "1day" or DELTA != 0.005 or CONFIRMATORY_N_BOOTSTRAP != 10000 or \
            CONFIRMATORY_BOOTSTRAP_SEED != 20260906:
        missing.append(
            "H/delta/n_bootstrap/CONFIRMATORY_BOOTSTRAP_SEED do not match the Section 2 "
            "preregistration record"
        )

    if missing:
        raise ConfirmatoryConfigurationIncompleteError(
            "confirmatory configuration is incomplete -- the experiment is not configured: "
            + "; ".join(missing)
        )
