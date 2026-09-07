"""
Section 7f Implementation Spec, FINAL (specs/section7f-implementation-spec-final.md)
-- self-contained and authoritative, settled after four rounds of review.
This module implements: explicit catalyst/experiment-epoch membership
(Section 1's Python half -- the schema half is migration 007), the
PENDING_NOT_MATURED classification and its maturity boundary (Section 4),
the frozen exception-to-classification mapping (Section 5), the
confirmatory statistical builder (Section 6), and the separately-gated
promotion test (Section 7).

It does NOT create arm_entries/arm_outcomes rows, fetch quotes, simulate a
stop trigger, or place an order -- this pass only consumes an existing
entry_timestamp/outcome row if one exists (Section 0's non-goals). It does
NOT modify confirmatory_analysis.py -- compute_expected_fees and
FEE_METHOD_VERSION are wired in by fee_methodology.py's own import-time
side effect (imported below), and every other confirmatory_analysis.py
name used here (assert_decision_set_ready_for_comparison,
assert_outcome_ready_for_confirmation, assert_confirmatory_configuration_complete,
the four hard-failure exception types, NO_ELIGIBLE_CANDIDATES/TRADED/ABSTAINED,
H) is used exactly as that module already defines it.

No frozen parameter, formula, or rule below is re-derived, re-tuned, or
"improved" based on anything discovered while implementing this (spec
Section 11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

import confirmatory_analysis
import fee_methodology  # noqa: F401 -- import-time side effect wires FEE_METHOD_VERSION/compute_expected_fees
from statistical_test import catalyst_clustered_test, TestResult
from trading_calendar import next_regular_session_open_strictly_after

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RequiredOutcomeOverdueError(Exception):
    """Section 4/5: a TRADED decision has a known entry_timestamp (an
    arm_entries row exists) but no arm_outcomes row for the confirmatory
    horizon, and analysis_as_of is past outcome_due_at(entry_timestamp) +
    OUTCOME_PROCESSING_GRACE -- the outcome SHOULD exist by now and doesn't.
    Hard failure -- abort the entire confirmatory build."""


class ConfirmatoryAnalysisNotAuthorizedError(Exception):
    """Section 7: CONFIRMATORY_ANALYSIS_TRIGGER is unset, or this report's
    analysis_as_of/included-catalyst count does not yet satisfy it."""


# ---------------------------------------------------------------------------
# Section 9 (new items #7/#8): deferred, fail-closed parameters -- unset,
# exactly like confirmatory_analysis.py's original six. Never given a
# made-up default by this implementation pass.
# ---------------------------------------------------------------------------

OUTCOME_PROCESSING_GRACE = None  # deferred #7 -- a timedelta, frozen before
    # confirmatory collection begins, never a caller-supplied per-run value.

CONFIRMATORY_ANALYSIS_TRIGGER = None  # deferred #8 -- a preregistered
    # calendar date, a preregistered included-catalyst count, or another
    # already-settled criterion. Represented here as a callable
    # `(ConfirmatoryBuildReport) -> bool` (a judgment call -- see the
    # implementation report: the spec leaves the exact shape unspecified,
    # and a predicate is the most criterion-agnostic representation of "a
    # date, a count, or another already-settled criterion" without this
    # pass inventing which one it will actually be). Left unset here;
    # whichever form it eventually takes, it must be frozen BEFORE it is
    # satisfied, never chosen retroactively once a report looks favorable.


# ---------------------------------------------------------------------------
# Section 1 (Python half): explicit catalyst/experiment-epoch membership.
# The schema (experiment_catalysts + its two triggers) is migration 007.
# ---------------------------------------------------------------------------

def get_confirmatory_catalyst_universe(conn, experiment_id: str, scoring_epoch: str) -> list[str]:
    """Every catalyst_id admitted to this experiment's scoring epoch, per
    experiment_catalysts. Raises ValueError if scoring_epoch doesn't match
    the experiment's own recorded epoch (defensive -- the DB trigger already
    prevents this from being written, but a caller could still pass a wrong
    scoring_epoch string that matches zero rows silently; this call makes
    that loud instead)."""
    with conn.cursor() as cur:
        cur.execute("SELECT scoring_epoch FROM experiments WHERE experiment_id = %s", (experiment_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"experiment {experiment_id!r} does not exist")
    if row[0] != scoring_epoch:
        raise ValueError(
            f"scoring_epoch {scoring_epoch!r} does not match experiment {experiment_id!r}'s own "
            f"recorded scoring_epoch {row[0]!r} -- a caller passed a scoring_epoch that would "
            "silently match zero experiment_catalysts rows."
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT catalyst_id FROM experiment_catalysts WHERE experiment_id = %s AND scoring_epoch = %s "
            "ORDER BY catalyst_id",
            (experiment_id, scoring_epoch),
        )
        return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Section 4: PENDING_NOT_MATURED -- a fourth, time-bounded, non-exception state
# ---------------------------------------------------------------------------

PENDING_NOT_MATURED = "PENDING_NOT_MATURED"


def outcome_due_at(entry_timestamp: datetime) -> datetime:
    """outcome_due_at = next_regular_session_open_strictly_after(entry_timestamp)
    -- the exact Section 8-spec 7b exit-timing rule, computed via the shared
    calendar utility (trading_calendar.py), never a hand-rolled
    approximation."""
    return next_regular_session_open_strictly_after(entry_timestamp)


def classify_traded_catalyst_maturity(entry_timestamp: datetime, analysis_as_of: datetime) -> str:
    """
    analysis_as_of <= outcome_due_at(entry_timestamp) + OUTCOME_PROCESSING_GRACE
        and no required outcome row exists yet  -> PENDING_NOT_MATURED
    analysis_as_of >  outcome_due_at(entry_timestamp) + OUTCOME_PROCESSING_GRACE
        and no required outcome row exists yet  -> hard failure (raises RequiredOutcomeOverdueError)
    Both entry_timestamp and analysis_as_of must be timezone-aware; raises
    ValueError on a naive input rather than guessing a timezone.

    Only ever called by the builder once it has already confirmed no
    arm_outcomes row exists for this entry/horizon -- "an outcome row
    exists -> proceed to assert_outcome_ready_for_confirmation as today" is
    handled by the caller before this function is ever reached, not inside
    it.
    """
    if entry_timestamp.tzinfo is None:
        raise ValueError(f"classify_traded_catalyst_maturity: entry_timestamp={entry_timestamp!r} is naive")
    if analysis_as_of.tzinfo is None:
        raise ValueError(f"classify_traded_catalyst_maturity: analysis_as_of={analysis_as_of!r} is naive")
    if OUTCOME_PROCESSING_GRACE is None:
        raise confirmatory_analysis.ConfirmatoryConfigurationIncompleteError(
            "OUTCOME_PROCESSING_GRACE is not frozen -- cannot determine whether a traded catalyst "
            "with no outcome row yet is merely not-yet-due or genuinely overdue."
        )

    due_at = outcome_due_at(entry_timestamp)
    boundary = due_at + OUTCOME_PROCESSING_GRACE
    if analysis_as_of <= boundary:
        return PENDING_NOT_MATURED
    raise RequiredOutcomeOverdueError(
        f"entry_timestamp={entry_timestamp!r}: outcome_due_at={due_at!r}, grace boundary={boundary!r}, "
        f"analysis_as_of={analysis_as_of!r} is past the boundary and no outcome row exists yet."
    )


# ---------------------------------------------------------------------------
# Section 6: the statistical builder
# ---------------------------------------------------------------------------

@dataclass
class ConfirmatoryBuildReport:
    experiment_id: str
    scoring_epoch: str
    analysis_as_of: datetime
    total_catalysts_in_scope: int
    catalysts_with_zero_eligible_candidates: int   # NOT_POLICY_OBSERVATION
    pending_count: int                              # PENDING_NOT_MATURED
    legitimate_exclusion_count: int
    legitimate_exclusions_by_arm: dict[str, int]    # {'A': n, 'G': n, 'both': n}
    included_count: int
    records: list[tuple[str, float]]                # (catalyst_id, D_c) -- feeds catalyst_clustered_test


def _resolve_arm(conn, candidate_id, model_id: str, model_version: str, analysis_as_of: datetime):
    """Resolves ONE arm's contribution for a TRADED decision on one
    catalyst. Returns ("RETURN", value), ("PENDING", None), or
    ("EXCLUDED", None). Any hard-failure exception (OutcomeIntegrityError,
    ConfirmatoryConfigurationIncompleteError, RequiredOutcomeOverdueError,
    or a naive-timestamp ValueError) propagates uncaught -- this function
    never swallows anything except the one explicitly-legitimate exclusion,
    QuoteUnobservableError."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision_id FROM model_candidate_decisions "
            "WHERE candidate_id = %s AND model_id = %s AND model_version = %s",
            (candidate_id, model_id, model_version),
        )
        row = cur.fetchone()
    # assert_trade_abstention_invariant (already run by the caller via
    # assert_decision_set_ready_for_comparison) only ever returns a
    # selected_candidate_id backed by a real decision row for this exact
    # (model_id, model_version) -- this lookup cannot legitimately miss.
    decision_id = row[0]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT entry_id, entry_timestamp FROM arm_entries WHERE model_decision_id = %s",
            (decision_id,),
        )
        entry_row = cur.fetchone()

    if entry_row is None:
        # No execution pipeline exists anywhere in the repo yet (Section 4's
        # own framing text) -- a TRADED decision with no arm_entries row at
        # all has not been entered, so there is no entry_timestamp to
        # measure maturity from. Judgment call, not explicitly stated in
        # the spec: treat this, unconditionally, as PENDING_NOT_MATURED --
        # see the implementation report. classify_traded_catalyst_maturity
        # is not reached here since it requires a concrete entry_timestamp.
        return ("PENDING", None)

    entry_id, entry_timestamp = entry_row

    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome_id, return_net_of_costs FROM arm_outcomes WHERE entry_id = %s AND horizon_label = %s",
            (entry_id, confirmatory_analysis.H),
        )
        outcome_row = cur.fetchone()

    if outcome_row is None:
        classify_traded_catalyst_maturity(entry_timestamp, analysis_as_of)  # raises unless PENDING_NOT_MATURED
        return ("PENDING", None)

    outcome_id, return_net_of_costs = outcome_row
    try:
        confirmatory_analysis.assert_outcome_ready_for_confirmation(conn, outcome_id)
    except confirmatory_analysis.QuoteUnobservableError:
        return ("EXCLUDED", None)
    return ("RETURN", float(return_net_of_costs))


def _classify_catalyst(conn, catalyst_id: str, model_specs: dict, analysis_as_of: datetime):
    """Returns (classification, attributed_to, d_c) where classification is
    one of 'NOT_POLICY_OBSERVATION', 'PENDING', 'EXCLUDED', 'INCLUDED'.
    attributed_to is one of 'A'/'G'/'both' only when classification ==
    'EXCLUDED', else None. d_c is set only when classification == 'INCLUDED'.

    Any hard-failure exception from assert_decision_set_ready_for_comparison
    or _resolve_arm propagates uncaught."""
    decision_result = confirmatory_analysis.assert_decision_set_ready_for_comparison(
        conn, catalyst_id, [model_specs["A"], model_specs["G"]]
    )
    if decision_result == confirmatory_analysis.NO_ELIGIBLE_CANDIDATES:
        return ("NOT_POLICY_OBSERVATION", None, None)

    resolutions = {}
    for arm in ("A", "G"):
        model_id, model_version = model_specs[arm]
        result = decision_result[(model_id, model_version)]
        if result.status == confirmatory_analysis.ABSTAINED:
            resolutions[arm] = ("RETURN", 0.0)
        else:
            assert result.status == confirmatory_analysis.TRADED
            resolutions[arm] = _resolve_arm(conn, result.selected_candidate_id, model_id, model_version, analysis_as_of)

    kind_a, value_a = resolutions["A"]
    kind_g, value_g = resolutions["G"]

    # Priority, per catalyst, when the two arms' resolutions disagree
    # (judgment call -- the spec's Section 5 table lists conditions
    # per-arm/singly, not a combination rule for one catalyst carrying two
    # different signals; see the implementation report): PENDING takes
    # priority over EXCLUDED, since "not yet due" is temporally prior to
    # "permanently excluded" and re-checkable later, whereas an EXCLUDED
    # arm's outcome row already exists and was already, definitively,
    # found quote-unobservable -- that resolution cannot itself become
    # "pending" on a later run.
    if kind_a == "PENDING" or kind_g == "PENDING":
        return ("PENDING", None, None)
    if kind_a == "EXCLUDED" or kind_g == "EXCLUDED":
        if kind_a == "EXCLUDED" and kind_g == "EXCLUDED":
            attributed_to = "both"
        elif kind_a == "EXCLUDED":
            attributed_to = "A"
        else:
            attributed_to = "G"
        return ("EXCLUDED", attributed_to, None)

    assert kind_a == "RETURN" and kind_g == "RETURN"
    return ("INCLUDED", None, value_a - value_g)


def build_confirmatory_dataset(
    conn,
    experiment_id: str,
    scoring_epoch: str,
    analysis_as_of: datetime,
    model_specs: dict,  # {'A': (model_id, model_version), 'G': (...)}
) -> ConfirmatoryBuildReport:
    """
    1. assert_confirmatory_configuration_complete() -- once, before anything else.
    2. catalyst_universe = get_confirmatory_catalyst_universe(conn, experiment_id, scoring_epoch)
    3. For each catalyst_id: assert_decision_set_ready_for_comparison(...),
       then per Sections 4/5/6 above, classify and accumulate.
    4. Raises immediately on any hard failure (Section 5) -- no partial report.
    5. Otherwise returns the full ConfirmatoryBuildReport.
    Freely re-runnable for QA/monitoring -- does NOT itself run the
    promotion test. Purely read-only: creates no arm_entries/arm_outcomes
    rows, fetches no quotes, writes nothing to the database.
    """
    if analysis_as_of.tzinfo is None:
        raise ValueError(f"build_confirmatory_dataset: analysis_as_of={analysis_as_of!r} is naive")
    if set(model_specs.keys()) != {"A", "G"}:
        raise ValueError(f"model_specs must have exactly keys 'A' and 'G', got {sorted(model_specs.keys())!r}")

    confirmatory_analysis.assert_confirmatory_configuration_complete()

    catalyst_universe = get_confirmatory_catalyst_universe(conn, experiment_id, scoring_epoch)

    catalysts_with_zero_eligible_candidates = 0
    pending_count = 0
    legitimate_exclusions_by_arm = {"A": 0, "G": 0, "both": 0}
    records: list[tuple[str, float]] = []

    for catalyst_id in catalyst_universe:
        classification, attributed_to, d_c = _classify_catalyst(conn, catalyst_id, model_specs, analysis_as_of)
        if classification == "NOT_POLICY_OBSERVATION":
            catalysts_with_zero_eligible_candidates += 1
        elif classification == "PENDING":
            pending_count += 1
        elif classification == "EXCLUDED":
            legitimate_exclusions_by_arm[attributed_to] += 1
        else:
            assert classification == "INCLUDED"
            records.append((catalyst_id, d_c))

    return ConfirmatoryBuildReport(
        experiment_id=experiment_id,
        scoring_epoch=scoring_epoch,
        analysis_as_of=analysis_as_of,
        total_catalysts_in_scope=len(catalyst_universe),
        catalysts_with_zero_eligible_candidates=catalysts_with_zero_eligible_candidates,
        pending_count=pending_count,
        legitimate_exclusion_count=sum(legitimate_exclusions_by_arm.values()),
        legitimate_exclusions_by_arm=legitimate_exclusions_by_arm,
        included_count=len(records),
        records=records,
    )


# ---------------------------------------------------------------------------
# Section 7: promotion test -- separated, single preregistered look
# ---------------------------------------------------------------------------

def run_confirmatory_promotion_test(report: ConfirmatoryBuildReport) -> TestResult:
    """Raises ConfirmatoryAnalysisNotAuthorizedError unless
    CONFIRMATORY_ANALYSIS_TRIGGER is frozen and satisfied by `report`.
    Otherwise calls catalyst_clustered_test(report.records, delta=DELTA,
    n_bootstrap=CONFIRMATORY_N_BOOTSTRAP, rng=np.random.default_rng(CONFIRMATORY_BOOTSTRAP_SEED)).
    This is the ONLY function that may make the promotion decision -- never
    call catalyst_clustered_test directly on a build report elsewhere."""
    if CONFIRMATORY_ANALYSIS_TRIGGER is None:
        raise ConfirmatoryAnalysisNotAuthorizedError(
            "CONFIRMATORY_ANALYSIS_TRIGGER is unset -- the promotion decision may not be made until "
            "a trigger criterion is frozen, preregistered BEFORE it is satisfied."
        )
    if not CONFIRMATORY_ANALYSIS_TRIGGER(report):
        raise ConfirmatoryAnalysisNotAuthorizedError(
            f"CONFIRMATORY_ANALYSIS_TRIGGER is frozen but not yet satisfied by this report "
            f"(analysis_as_of={report.analysis_as_of!r}, included_count={report.included_count!r})."
        )
    rng = np.random.default_rng(confirmatory_analysis.CONFIRMATORY_BOOTSTRAP_SEED)
    return catalyst_clustered_test(
        report.records,
        delta=confirmatory_analysis.DELTA,
        n_bootstrap=confirmatory_analysis.CONFIRMATORY_N_BOOTSTRAP,
        rng=rng,
    )
