"""
Selection Diagnostics v1 Implementation Spec, FINAL v9
(specs/selection-diagnostics-v1-implementation-spec-final.md) -- self-
contained and authoritative, settled after nine rounds of review.

`build_selection_diagnostics()` is a new, freely re-runnable, purely
descriptive report measuring concentration and repeat-selection patterns
in Arm A's and Arm G's decisions over a scoring epoch. It does NOT alter
candidate eligibility, model decisions, confirmatory sample inclusion,
`D_c`, or `run_confirmatory_promotion_test` in any way; it is not gated by
`CONFIRMATORY_ANALYSIS_TRIGGER` (it computes no p-values or confidence
intervals); it does not compute or reference beta; it accepts no
caller-supplied historical cutoff (spec Section 0).

Per Section 0a, `confirmatory_analysis.py` is NOT modified by this work --
every existing primitive used here (`TRADED`, `ABSTAINED`,
`NO_ELIGIBLE_CANDIDATES`, `CatalystDecisionResult`,
`get_current_event_versions_for_catalyst`, `assert_trade_abstention_invariant`,
`stable_entity_sort_key`) is imported read-only from its current owning
module. `confirmatory_builder.get_confirmatory_catalyst_universe` is reused
for epoch/catalyst membership rather than duplicated -- see that function's
own module for why "visible in this REPEATABLE READ snapshot" and
"admitted_at <= generated_at" are provably the same set here.
`confirmatory_builder.py` does not import this module (no cycle).

No frozen parameter, formula, or rule below is re-derived, re-tuned, or
"improved" based on anything discovered while implementing this.
"""

from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction

import psycopg2.extensions

import confirmatory_analysis
from confirmatory_analysis import (
    TRADED,
    ABSTAINED,
    NO_ELIGIBLE_CANDIDATES,
    CatalystDecisionResult,
    get_current_event_versions_for_catalyst,
    assert_trade_abstention_invariant,
    stable_entity_sort_key,
)
from candidate_coverage import find_incomplete_coverage
from confirmatory_builder import get_confirmatory_catalyst_universe

# ---------------------------------------------------------------------------
# Section 1: NOT_YET_SCORED -- a plain string constant, compared by ==/!=
# only (never is/is not; CPython string interning is not a contract).
# ---------------------------------------------------------------------------

NOT_YET_SCORED = "NOT_YET_SCORED"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SelectionDiagnosticsInternalConsistencyError(Exception):
    """Section 1 / Section 5a: an invariant this builder relies on (e.g. a
    catalyst already established to have N > 0 eligible candidates later
    evaluating to NO_ELIGIBLE_CANDIDATES inside assert_trade_abstention_invariant,
    or a CatalystDecisionResult contradicting its own TRADED/ABSTAINED <->
    selected_candidate_id contract, or a selected candidate that doesn't
    resolve cleanly to an eligible entity in this catalyst's current
    universe) was violated. A real data-integrity/implementation-defect
    signal -- never a case to silently skip or drop a selection."""


class EntityKeyCollisionError(Exception):
    """Section 6: two distinct entity_id values resolved to the same
    stable_entity_sort_key within one report build -- a same-snapshot
    collision, distinct from stable_entity_sort_key's already-documented
    non-determinism across DIFFERENT database rebuilds. Means two
    un-merged entity-master rows for what should be one economic entity;
    surfaced, never silently aggregated together."""


# ---------------------------------------------------------------------------
# Section 1: classify_arm_decision_state
# ---------------------------------------------------------------------------

_EntityKeyInput = namedtuple("_EntityKeyInput", ["entity_id", "cik", "legal_name"])


def classify_arm_decision_state(conn, catalyst_id, model_id, model_version):
    """Precondition: this catalyst has already been established to have
    N > 0 eligible candidates (see build_selection_diagnostics) -- this
    function must never be called otherwise.

    Incomplete coverage is a normal, non-exceptional staging state: returns
    the NOT_YET_SCORED module constant (not an exception) in that case.
    Otherwise returns the full CatalystDecisionResult from
    assert_trade_abstention_invariant (.status must be TRADED or ABSTAINED
    at this point; .selected_candidate_id is set only for TRADED).

    This function is NOT unconditionally non-raising: integrity/invariant
    failures still raise and abort the whole diagnostics build --
    SelectionDiagnosticsInternalConsistencyError (the defensive checks
    below), and TradeAbstentionInvariantError / UnresolvedRankingTieError /
    InvalidCandidateScoreError propagated from assert_trade_abstention_invariant
    itself. Only incomplete coverage is treated as an ordinary return value.

    Coverage is checked FIRST and unconditionally, over the union of every
    current event_version under this catalyst -- calling
    assert_trade_abstention_invariant against an incomplete decision set
    would let the currently-highest-scored recorded candidate masquerade as
    the true rank 1 before every eligible candidate has actually been
    decided. Built directly on find_incomplete_coverage (never by calling
    assert_full_coverage and catching its bare ValueError -- that would
    silently reclassify any unrelated ValueError as staging state)."""
    incomplete = []
    for event_version_id in get_current_event_versions_for_catalyst(conn, catalyst_id):
        incomplete.extend(
            find_incomplete_coverage(conn, event_version_id, [model_id], model_version)
        )
    if incomplete:
        return NOT_YET_SCORED

    result = assert_trade_abstention_invariant(conn, catalyst_id, model_id, model_version)

    if result.status == NO_ELIGIBLE_CANDIDATES:
        raise SelectionDiagnosticsInternalConsistencyError(
            f"catalyst {catalyst_id}, model {model_id}: assert_trade_abstention_invariant "
            "returned NO_ELIGIBLE_CANDIDATES after the builder already established N > 0 "
            "eligible candidates for this catalyst in the same snapshot -- this should be "
            "impossible and indicates a real inconsistency, not a legitimate state."
        )
    if result.status == TRADED and result.selected_candidate_id is None:
        raise SelectionDiagnosticsInternalConsistencyError(
            f"catalyst {catalyst_id}, model {model_id}: status=TRADED but "
            "selected_candidate_id is None -- contradicts CatalystDecisionResult's own contract."
        )
    if result.status == ABSTAINED and result.selected_candidate_id is not None:
        raise SelectionDiagnosticsInternalConsistencyError(
            f"catalyst {catalyst_id}, model {model_id}: status=ABSTAINED but "
            f"selected_candidate_id={result.selected_candidate_id!r} is set -- contradicts "
            "CatalystDecisionResult's own contract."
        )
    return result


# ---------------------------------------------------------------------------
# Section 5: get_eligible_entities_for_catalyst
# ---------------------------------------------------------------------------

def get_eligible_entities_for_catalyst(conn, catalyst_id):
    """All and only eligible candidates (candidate_signals rows with
    eligibility_status = 'eligible') under the catalyst's CURRENT
    event_versions -- the exact same universe get_current_event_versions_for_catalyst
    and assert_trade_abstention_invariant already use, no more and no less.
    Never includes a candidate attached only to a superseded/historical
    event_version.

    Returns a dict {entity_id: (cik, legal_name)} -- a distinct-entity
    projection: an entity appearing via more than one candidate_signals row
    under this catalyst (e.g. three separate event_versions referencing the
    same company) contributes exactly one entry, not three."""
    event_version_ids = get_current_event_versions_for_catalyst(conn, catalyst_id)
    if not event_version_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT e.entity_id, e.cik, e.legal_name
            FROM candidate_signals cs
            JOIN entities e ON e.entity_id = cs.entity_id
            WHERE cs.event_version_id = ANY(%s::uuid[]) AND cs.eligibility_status = 'eligible'
            """,
            (event_version_ids,),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Section 5a: selected_candidate_id -> entity resolution
# ---------------------------------------------------------------------------

def _resolve_selected_entity(conn, catalyst_id, selected_candidate_id, eligible_entities):
    """For a TRADED result, resolves result.selected_candidate_id to
    (entity_id, cik, legal_name), in order:
      1-2. Look up the candidate_signals row for this candidate_id (a
           primary key -- exactly one row is a sanity assertion, not a
           real branch, but asserted rather than assumed) and read its
           entity_id/event_version_id/eligibility_status directly.
      3. Require that candidate to belong to one of this catalyst's
         CURRENT event_versions -- not a superseded one.
      4. Require that SPECIFIC candidate row's own eligibility_status ==
         'eligible' -- a narrower, distinct check from step 5: an entity
         can have more than one candidate_signals row under the same
         catalyst, so checking only that the entity appears somewhere in
         the eligible-entity set would still pass if the SELECTED row
         itself were ineligible but the same entity had a separate,
         eligible row. This step is what must catch that case, not step 5.
      5. Require the resolved entity_id to be present in
         `eligible_entities` (this catalyst's get_eligible_entities_for_catalyst
         output).
    Any failure of steps 2-5 is SelectionDiagnosticsInternalConsistencyError
    -- never a silently skipped or dropped selection."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_version_id, entity_id, eligibility_status FROM candidate_signals "
            "WHERE candidate_id = %s",
            (selected_candidate_id,),
        )
        rows = cur.fetchall()

    if len(rows) != 1:
        raise SelectionDiagnosticsInternalConsistencyError(
            f"catalyst {catalyst_id}: selected_candidate_id={selected_candidate_id!r} matched "
            f"{len(rows)} candidate_signals row(s), expected exactly 1 (candidate_id is a "
            "primary key)."
        )
    event_version_id, entity_id, eligibility_status = rows[0]

    current_event_version_ids = set(get_current_event_versions_for_catalyst(conn, catalyst_id))
    if event_version_id not in current_event_version_ids:
        raise SelectionDiagnosticsInternalConsistencyError(
            f"catalyst {catalyst_id}: selected_candidate_id={selected_candidate_id!r} belongs to "
            f"event_version_id={event_version_id!r}, which is not among this catalyst's current "
            f"event_versions {current_event_version_ids!r} -- a superseded event_version."
        )
    if eligibility_status != "eligible":
        raise SelectionDiagnosticsInternalConsistencyError(
            f"catalyst {catalyst_id}: selected_candidate_id={selected_candidate_id!r}'s own "
            f"eligibility_status={eligibility_status!r} != 'eligible' -- the SELECTED row itself "
            "is ineligible, regardless of whether its entity has a separate eligible row."
        )
    if entity_id not in eligible_entities:
        raise SelectionDiagnosticsInternalConsistencyError(
            f"catalyst {catalyst_id}: selected_candidate_id={selected_candidate_id!r} resolves to "
            f"entity_id={entity_id!r}, which is not present in get_eligible_entities_for_catalyst's "
            "output for this catalyst."
        )
    cik, legal_name = eligible_entities[entity_id]
    return entity_id, cik, legal_name


# ---------------------------------------------------------------------------
# Section 6: durable entity identity, with same-snapshot collision detection
# ---------------------------------------------------------------------------

class _EntityKeyRegistry:
    """Tracks entity_id -> stable_entity_sort_key across one whole report
    build, raising EntityKeyCollisionError the moment two DISTINCT
    entity_id values resolve to the same key. entity_id is carried here
    only as debug/collision-detection metadata -- never the report's
    scientific identity key (that is entity_key itself)."""

    def __init__(self):
        self._entity_id_by_key: dict[str, str] = {}
        self._key_by_entity_id: dict[str, str] = {}

    def key_for(self, entity_id, cik, legal_name) -> str:
        cached = self._key_by_entity_id.get(entity_id)
        if cached is not None:
            return cached
        key = stable_entity_sort_key(_EntityKeyInput(entity_id=entity_id, cik=cik, legal_name=legal_name))
        existing_entity_id = self._entity_id_by_key.get(key)
        if existing_entity_id is not None and existing_entity_id != entity_id:
            raise EntityKeyCollisionError(
                f"entity_id {entity_id!r} and entity_id {existing_entity_id!r} both resolve to the "
                f"same stable_entity_sort_key {key!r} within this report build -- two un-merged "
                "entity-master rows for what should be one economic entity."
            )
        self._entity_id_by_key[key] = entity_id
        self._key_by_entity_id[entity_id] = key
        return key


# ---------------------------------------------------------------------------
# Section 8: data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EntitySelectionDiagnostic:
    entity_key: str
    legal_name: str
    eligible_count: int
    selected_count: int
    selection_rate: float


@dataclass(frozen=True)
class ArmSelectionDiagnostics:
    # Pre-pairing staging counts -- each arm's own full ready set.
    decision_ready_catalysts: int
    not_yet_scored_count: int

    # Paired-ready comparative statistics -- computed ONLY over paired_ready.
    paired_traded_count: int
    paired_abstained_count: int

    distinct_selected_entities: int
    top_entities: list  # list[EntitySelectionDiagnostic]; paired_ready only; selected_count > 0 only
    entities: list      # list[EntitySelectionDiagnostic]; paired_ready only; full opportunity table

    hhi: float | None
    effective_names: float | None


_DEFAULT_NOTE = (
    "These diagnostics are descriptive and detect concentration and "
    "cross-catalyst repeat-selection patterns. They do not alter "
    "candidate eligibility, model decisions, confirmatory sample "
    "inclusion, D_c, or the promotion test. Selection frequency is not "
    "interpreted as a measure of media attention or any external "
    "signal. generated_at is captured internally at build time, fixes "
    "epoch membership, and reflects current persisted decision state "
    "as of one consistent database snapshot; this report does not "
    "reconstruct historical coverage or decision values at any other "
    "point in time, and accepts no caller-supplied cutoff."
)


@dataclass(frozen=True)
class SelectionDiagnosticsReport:
    experiment_id: str
    scoring_epoch: str
    generated_at: datetime

    total_epoch_catalysts: int
    no_eligible_candidates_count: int

    paired_decision_ready_catalysts: int

    per_arm: dict  # dict[str, ArmSelectionDiagnostics]; keys: 'A', 'G'

    note: str = _DEFAULT_NOTE


# ---------------------------------------------------------------------------
# HHI / effective-names / entity-list assembly helpers
# ---------------------------------------------------------------------------

def _compute_hhi_and_effective_names(selected_count_for_arm: dict):
    """selected_count_for_arm: {entity_key: count}, count > 0 for every
    entry present (a zero-selected entity is simply absent from this dict).
    Computed from exact integer counts via Fraction, converted to float
    only at the reporting boundary -- never mid-calculation."""
    total_selections = sum(selected_count_for_arm.values())
    if total_selections == 0:
        return None, None
    hhi_fraction = sum(
        Fraction(count, total_selections) ** 2 for count in selected_count_for_arm.values()
    )
    hhi = float(hhi_fraction)
    effective_names = float(Fraction(1) / hhi_fraction)
    return hhi, effective_names


def _build_entities_list(eligible_count: dict, selected_count_for_arm: dict, legal_name_by_key: dict):
    """Ordered by selected_count DESC, entity_key ASC -- descriptive
    ordering, not a selection decision, so an exact tie is broken
    deterministically by entity_key alone rather than failing closed."""
    entities = []
    for key, eligible in eligible_count.items():
        selected = selected_count_for_arm.get(key, 0)
        rate = selected / eligible  # eligible is always > 0 -- only ever incremented when eligible
        entities.append(
            EntitySelectionDiagnostic(
                entity_key=key,
                legal_name=legal_name_by_key[key],
                eligible_count=eligible,
                selected_count=selected,
                selection_rate=rate,
            )
        )
    entities.sort(key=lambda e: (-e.selected_count, e.entity_key))
    return entities


def _build_top_entities(entities: list) -> list:
    """First 10 entities with selected_count > 0 -- 'entities' is already
    sorted (selected_count DESC, entity_key ASC), so filtering preserves
    that order; an eligible-but-never-selected entity never appears here,
    even if fewer than 10 entities were ever actually selected."""
    return [e for e in entities if e.selected_count > 0][:10]


# ---------------------------------------------------------------------------
# Section 2 / 2a / 4 / 7: the builder itself
# ---------------------------------------------------------------------------

def build_selection_diagnostics(
    conn,
    experiment_id: str,
    scoring_epoch: str,
    model_specs: dict,
) -> SelectionDiagnosticsReport:
    """model_specs: {'A': (model_id, model_version), 'G': (model_id, model_version)}.
    Validated to exactly {"A", "G"} before the transaction opens (Section 7).

    Runs the whole build inside one REPEATABLE READ transaction against
    the caller-owned `conn` (Section 2a) -- rejects an already-active
    transaction or autocommit=True up front, and restores the connection's
    prior isolation level in a finally regardless of outcome.

    generated_at is captured internally only (Section 7), immediately
    before the first diagnostics data query, after REPEATABLE READ is
    configured -- there is no parameter, public or private, by which a
    caller can supply a different value."""
    if set(model_specs) != {"A", "G"}:
        raise ValueError(
            f"model_specs must have exactly keys 'A' and 'G', got {sorted(model_specs)!r} -- "
            "this report exists specifically for the A-vs-G comparison."
        )

    if conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
        raise ValueError(
            "build_selection_diagnostics requires conn to have no transaction already in "
            "progress -- it manages its own transaction and must not be nested inside a "
            "caller's."
        )
    if conn.autocommit:
        raise ValueError(
            "build_selection_diagnostics requires autocommit=False -- with autocommit=True, "
            "REPEATABLE READ does not hold a snapshot across statements (each autocommitted "
            "statement gets its own snapshot), which defeats the whole point of this section."
        )

    prior_isolation_level = conn.isolation_level
    try:
        conn.set_session(isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ)
        generated_at = datetime.now(timezone.utc)

        report = _build_report_body(conn, experiment_id, scoring_epoch, model_specs, generated_at)

        conn.commit()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        # psycopg2 quirk, verified empirically against a real server: when
        # the connection was at the driver default (conn.isolation_level is
        # None, i.e. == ISOLATION_LEVEL_DEFAULT), calling
        # set_session(isolation_level=None) is a NO-OP -- None here means
        # "leave this parameter as it currently is", not "reset it".
        # Resetting to the real default requires the string "DEFAULT".
        if prior_isolation_level == psycopg2.extensions.ISOLATION_LEVEL_DEFAULT:
            conn.set_session(isolation_level="DEFAULT")
        else:
            conn.set_session(isolation_level=prior_isolation_level)


def _build_report_body(conn, experiment_id, scoring_epoch, model_specs, generated_at):
    """The actual diagnostics computation, run entirely inside the
    REPEATABLE READ transaction build_selection_diagnostics opens.
    Separated out only so the transaction-management wrapper above stays
    small and easy to audit against Section 2a's contract."""
    catalyst_universe = get_confirmatory_catalyst_universe(conn, experiment_id, scoring_epoch)

    model_id_a, model_version_a = model_specs["A"]
    model_id_g, model_version_g = model_specs["G"]

    no_eligible_candidates_count = 0
    a_only_ready_count = 0
    g_only_ready_count = 0
    neither_ready_count = 0
    not_yet_scored_count = {"A": 0, "G": 0}
    decision_ready_catalysts = {"A": 0, "G": 0}
    paired_traded_count = {"A": 0, "G": 0}
    paired_abstained_count = {"A": 0, "G": 0}
    paired_decision_ready_catalysts = 0

    registry = _EntityKeyRegistry()
    eligible_count: dict = {}          # entity_key -> count, shared across both arms by construction
    legal_name_by_key: dict = {}
    selected_count = {"A": {}, "G": {}}  # arm -> entity_key -> count

    for catalyst_id in catalyst_universe:
        eligible_entities = get_eligible_entities_for_catalyst(conn, catalyst_id)
        if not eligible_entities:
            no_eligible_candidates_count += 1
            continue

        a_result = classify_arm_decision_state(conn, catalyst_id, model_id_a, model_version_a)
        g_result = classify_arm_decision_state(conn, catalyst_id, model_id_g, model_version_g)

        a_ready = a_result != NOT_YET_SCORED
        g_ready = g_result != NOT_YET_SCORED

        if a_ready:
            decision_ready_catalysts["A"] += 1
        else:
            not_yet_scored_count["A"] += 1
        if g_ready:
            decision_ready_catalysts["G"] += 1
        else:
            not_yet_scored_count["G"] += 1

        if not (a_ready and g_ready):
            if a_ready and not g_ready:
                a_only_ready_count += 1
            elif g_ready and not a_ready:
                g_only_ready_count += 1
            else:
                neither_ready_count += 1
            continue

        # paired_ready
        paired_decision_ready_catalysts += 1

        for entity_id, (cik, legal_name) in eligible_entities.items():
            key = registry.key_for(entity_id, cik, legal_name)
            eligible_count[key] = eligible_count.get(key, 0) + 1
            legal_name_by_key.setdefault(key, legal_name)

        for arm, result in (("A", a_result), ("G", g_result)):
            if result.status == TRADED:
                paired_traded_count[arm] += 1
                entity_id, cik, legal_name = _resolve_selected_entity(
                    conn, catalyst_id, result.selected_candidate_id, eligible_entities
                )
                key = registry.key_for(entity_id, cik, legal_name)
                selected_count[arm][key] = selected_count[arm].get(key, 0) + 1
            else:
                assert result.status == ABSTAINED
                paired_abstained_count[arm] += 1

    per_arm = {}
    for arm in ("A", "G"):
        entities = _build_entities_list(eligible_count, selected_count[arm], legal_name_by_key)
        top_entities = _build_top_entities(entities)
        hhi, effective_names = _compute_hhi_and_effective_names(selected_count[arm])
        per_arm[arm] = ArmSelectionDiagnostics(
            decision_ready_catalysts=decision_ready_catalysts[arm],
            not_yet_scored_count=not_yet_scored_count[arm],
            paired_traded_count=paired_traded_count[arm],
            paired_abstained_count=paired_abstained_count[arm],
            distinct_selected_entities=len(selected_count[arm]),
            top_entities=top_entities,
            entities=entities,
            hhi=hhi,
            effective_names=effective_names,
        )

    return SelectionDiagnosticsReport(
        experiment_id=experiment_id,
        scoring_epoch=scoring_epoch,
        generated_at=generated_at,
        total_epoch_catalysts=len(catalyst_universe),
        no_eligible_candidates_count=no_eligible_candidates_count,
        paired_decision_ready_catalysts=paired_decision_ready_catalysts,
        per_arm=per_arm,
    )
