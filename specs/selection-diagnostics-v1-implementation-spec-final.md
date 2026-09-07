# Selection Diagnostics v1 — Implementation Spec (FINAL v9)

## 0a. Module placement (added post-freeze, before implementation)

Everything this spec introduces — `NOT_YET_SCORED`,
`classify_arm_decision_state`, `SelectionDiagnosticsInternalConsistencyError`,
`EntityKeyCollisionError`, `get_eligible_entities_for_catalyst`,
`build_selection_diagnostics`, and the `EntitySelectionDiagnostic` /
`ArmSelectionDiagnostics` / `SelectionDiagnosticsReport` dataclasses — lives
in a new `selection_diagnostics.py` module. `confirmatory_analysis.py` is
not modified by this work, at all — this matches the pattern already
established (and separately reviewed) for Section 7f and Section 8, where
analogous new state constants and exceptions (`PENDING_NOT_MATURED`,
`RequiredOutcomeOverdueError`, `OUTCOME_PROCESSING_GRACE`) went into the
newer module rather than into `confirmatory_analysis.py` — verified
directly against the repository: none of those three appear in
`confirmatory_analysis.py`.

Where §1 says `NOT_YET_SCORED` "sits beside" `TRADED`/`ABSTAINED`/
`NO_ELIGIBLE_CANDIDATES` "in `confirmatory_analysis.py`," that phrase is
about the *value convention* only (a plain string constant, compared by
`==`/`!=`) — it is not a file-placement instruction. `selection_diagnostics.py`
imports `TRADED`, `ABSTAINED`, `NO_ELIGIBLE_CANDIDATES`,
`CatalystDecisionResult`, `assert_trade_abstention_invariant`,
`stable_entity_sort_key`, and any other existing confirmatory primitive it
needs — read-only, by import. Nothing existing is moved, duplicated, or
re-exported "for convenience": every primitive keeps exactly one owning
module.

If this module also needs the epoch/catalyst-membership query for §2/§7
(`experiment_catalysts` rows for a given `experiment_id` + `scoring_epoch`),
prefer reusing `confirmatory_builder.get_confirmatory_catalyst_universe`
over duplicating its SQL — verified suitable, not just conveniently
similar: that helper has no `admitted_at`/timestamp filter, it returns
every `experiment_catalysts` row currently visible for that
`experiment_id`/`scoring_epoch`; called from inside this spec's own
`REPEATABLE READ` transaction (§2a), *after* that isolation level is
established, every row it can possibly see was committed strictly before
the snapshot was taken, and `generated_at` (§7) is captured from inside
that same snapshot — so "visible in the snapshot" and "`admitted_at <=
generated_at`" are provably the same set here, not merely similar in
practice. Its `ValueError` on a mismatched `scoring_epoch` should propagate
unchanged (it is a legitimate defensive check, consistent with this spec's
fail-loud posture elsewhere) rather than being caught or narrowed. This
reuse is not mandatory — a direct one-line query against
`experiment_catalysts` is also acceptable — but if the helper is used,
`confirmatory_builder.py` must not import `selection_diagnostics.py` in
return (that would create a cycle); nothing in this spec requires that
direction.

Tests for this spec go in a new, dedicated `test_selection_diagnostics.py`
— matching the existing one-test-module-per-source-module convention
already in place (`test_confirmatory_analysis.py`, alongside
`confirmatory_analysis.py`; `test_confirmatory_builder.py`, alongside
`confirmatory_builder.py`) — not appended to either existing test file.

## 0. Scope and non-goals

`build_selection_diagnostics()` is a new, freely re-runnable, purely
descriptive report. It measures concentration and repeat-selection patterns
in Arm A's and Arm G's decisions over a scoring epoch.

Non-goals, restated explicitly because this sits right next to the
confirmatory pipeline and must not be confused with it:

- Does not alter candidate eligibility, model decisions, confirmatory
  sample inclusion, `D_c`, or `run_confirmatory_promotion_test` in any way.
- Not gated by `CONFIRMATORY_ANALYSIS_TRIGGER`. It has no false-positive-rate
  exposure of its own (it computes no p-values or confidence intervals), so
  the repeated-interim-looks concern that motivates that gate for the
  promotion test does not apply here.
- Does not compute or reference beta (`market_data.beta_market` /
  `beta_sector`). A full-repository grep found zero producers of those
  columns — this is unpopulated schema, not an unverified-provenance
  question, and building a beta pipeline is a separate future decision.
- Does not accept a caller-supplied historical cutoff of any kind (§7 —
  `generated_at` is captured internally, never passed in).
- Selection frequency is not interpreted as a measure of media attention or
  any other external signal — it is purely a within-repo repeat-selection
  statistic.

## 1. Decision-readiness classification: `NOT_YET_SCORED`

A catalyst's per-arm decision state is one of three values:

```text
complete coverage + valid decision set        -> TRADED / ABSTAINED
incomplete coverage                            -> NOT_YET_SCORED
complete coverage + malformed decision set     -> hard failure (unchanged)
```

`NO_ELIGIBLE_CANDIDATES` is NOT one of the per-arm classifier's possible
outputs (see §2 for where it actually lives). `classify_arm_decision_state`
has an explicit precondition: it is only ever called for a catalyst already
established to have N > 0 eligible candidates.

`NOT_YET_SCORED` contributes to none of: HHI, effective-names, selection
counts, or opportunity denominators, for that arm, for that catalyst, in
that run.

**Frozen, not left to Claude Code to pick:** `NOT_YET_SCORED` is a plain
string constant, exactly like the existing `TRADED`/`ABSTAINED`/
`NO_ELIGIBLE_CANDIDATES` module constants it sits beside in
`confirmatory_analysis.py`:

```python
NOT_YET_SCORED = "NOT_YET_SCORED"
```

Comparisons against it use `==`/`!=`, never `is`/`is not` -- `is` identity
comparison against a string is not a reliable contract (CPython interning
of short literals can make it appear to work, but that is an
implementation detail, not a guarantee, and this project's own status
constants are already compared by value, not identity). All comparisons to
`NOT_YET_SCORED` MUST use `==`/`!=`; identity comparisons are prohibited.

**Implementation constraint, verified against the real code, not assumed:**
`assert_full_coverage()` (`candidate_coverage.py`) raises a bare `ValueError`
on incomplete coverage — there is no dedicated exception class for this
condition. `classify_arm_decision_state()` below MUST NOT call
`assert_full_coverage()` and catch `ValueError`; doing so would silently
reclassify any future unrelated `ValueError` raised anywhere in that call
path as `NOT_YET_SCORED` instead of surfacing it as the hard failure it
actually is. Instead, build directly on `find_incomplete_coverage()`, which
already returns a plain list (`[]` = complete) with no exception involved:

**Return type, corrected from an earlier draft:** this function must return
the FULL `CatalystDecisionResult` for a ready catalyst, not just its
`.status` string. Downstream code needs `selected_candidate_id` (set only
when `status == TRADED`, per that dataclass's own existing contract) to
compute `selected_count`, top entities, HHI, effective-names, and selection
rate. Returning only the status and re-deriving the selected candidate via
a second query would risk that second query drifting from the
already-validated invariant result — a needless redundant read of state
this function already has in hand.

```python
def classify_arm_decision_state(conn, catalyst_id, model_id, model_version):
    """Precondition: this catalyst has already been established to have
    N > 0 eligible candidates (see §2) -- this function must never be
    called otherwise.

    Incomplete coverage is a normal, non-exceptional staging state:
    returns the NOT_YET_SCORED module constant (not an exception) in that
    case. Otherwise returns the full CatalystDecisionResult from
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
    decided."""
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
```

The builder consumes this as:

```python
result = classify_arm_decision_state(conn, catalyst_id, model_id, model_version)
if result == NOT_YET_SCORED:
    ...  # this arm is staging for this catalyst
elif result.status == TRADED:
    selected_candidate_id = result.selected_candidate_id  # exactly one, guaranteed above
    ...
elif result.status == ABSTAINED:
    ...  # no selected entity for this arm on this catalyst
```

This mirrors the existing per-event-version loop already used in
`assert_decision_set_ready_for_comparison` — no new low-level coverage
primitive is required, only this catalyst-wide wrapper.

An actual `TradeAbstentionInvariantError`/`UnresolvedRankingTieError`/
`InvalidCandidateScoreError` raised by `assert_trade_abstention_invariant`
after coverage is confirmed complete is a hard abort of the whole
diagnostics build, exactly as in the confirmatory pipeline — this report
being descriptive does not relax that. A malformed decision set is a real
data-integrity problem regardless of which report is asking about it.

## 2. Full per-catalyst state sequence (explicit, not left to inference)

For every catalyst in the epoch (`experiment_catalysts` membership, per §7):

```text
eligible-universe determination for this catalyst (shared, not per-arm)
     |
     N = 0 eligible candidates
     -> report-level NO_ELIGIBLE_CANDIDATES.
        Do NOT call classify_arm_decision_state for either arm.
        Does NOT increment decision_ready_catalysts, not_yet_scored_count,
        paired_traded_count, or paired_abstained_count for EITHER arm.
        This catalyst contributes to nothing but the report-level count.
     |
     N > 0
     |
     classify_arm_decision_state(A) -> NOT_YET_SCORED, or a CatalystDecisionResult
     classify_arm_decision_state(G) -> NOT_YET_SCORED, or a CatalystDecisionResult
     (independently -- one arm's state never depends on the other's; the
     CatalystDecisionResult's .status is TRADED or ABSTAINED here, and for
     TRADED, .selected_candidate_id is what feeds every entity-level statistic)
     |
     both TRADED/ABSTAINED -> this catalyst is in paired_ready
     exactly one TRADED/ABSTAINED, other NOT_YET_SCORED -> A-only or G-only ready
     both NOT_YET_SCORED -> neither ready
```

`NO_ELIGIBLE_CANDIDATES` is therefore never asked "is A scored? is G
scored?" — it is resolved before either arm's per-arm classification runs,
and it is excluded from every per-arm bucket, not merely reported
alongside them. `classify_arm_decision_state` is never called at all for an
N=0 catalyst.

Reconciliation invariant, tested directly (verified: `+`, not `-`, on
every term — this sums the five mutually-exclusive per-catalyst buckets):

```text
total_epoch_catalysts ==
    no_eligible_candidates_count
    + paired_decision_ready_catalysts
    + a_only_ready_count
    + g_only_ready_count
    + neither_ready_count
```

(`a_only_ready_count`, `g_only_ready_count`, `neither_ready_count` need not
be persisted in the public `SelectionDiagnosticsReport` dataclass -- they
exist as an internal reconciliation the test suite checks, not necessarily
as user-facing fields. `A_not_yet_scored`/`G_not_yet_scored` in §4 already
surface the staging-lag information users need.)

### 2a. One consistent database snapshot per report build

The builder performs multiple separate reads (epoch membership,
per-catalyst eligibility, each arm's coverage, each arm's decisions, entity
metadata). Arm A and Arm G score asynchronously and scoring can be
happening concurrently with a diagnostics run, so if these reads are not
mutually consistent, a single report could reflect a state that never
actually existed in the database at any instant — e.g. reading Arm A as
incomplete, then a moment later reading Arm G as complete only because a
race let scoring finish between the two reads, producing a staging
classification that is an artifact of query order rather than of anything
real.

**Requirement:** every read performed for one `build_selection_diagnostics`
call happens against one consistent snapshot, using this project's existing
`psycopg2` connections. Concretely, in this order:

```python
import psycopg2.extensions

def build_selection_diagnostics(conn, experiment_id, scoring_epoch, model_specs):
    if conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
        raise ValueError(
            "build_selection_diagnostics requires conn to have no transaction "
            "already in progress -- it manages its own transaction and must not "
            "be nested inside a caller's."
        )
    if conn.autocommit:
        raise ValueError(
            "build_selection_diagnostics requires autocommit=False -- with "
            "autocommit=True, REPEATABLE READ does not hold a snapshot across "
            "statements (each autocommitted statement gets its own snapshot), "
            "which defeats the whole point of this section."
        )

    prior_isolation_level = conn.isolation_level
    try:
        conn.set_session(isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ)
        generated_at = datetime.now(timezone.utc)
        # ... all diagnostics reads happen here, against this one snapshot ...
        conn.commit()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        # psycopg2 quirk, verified empirically against a real server: when the
        # connection was at the driver default (conn.isolation_level is None,
        # i.e. == psycopg2.extensions.ISOLATION_LEVEL_DEFAULT), calling
        # set_session(isolation_level=None) is a NO-OP -- None here means
        # "leave this parameter as it currently is", not "reset it". Passing
        # the captured None straight back leaves the connection stuck at
        # REPEATABLE READ. Resetting to the real default requires the string
        # "DEFAULT", not the captured None value.
        if prior_isolation_level == psycopg2.extensions.ISOLATION_LEVEL_DEFAULT:
            conn.set_session(isolation_level="DEFAULT")
        else:
            conn.set_session(isolation_level=prior_isolation_level)
```

`build_selection_diagnostics` receives an existing, caller-owned `conn` —
it must not permanently change that connection's session configuration.
The contract is:

1. Fail loudly (do not silently proceed) if a transaction is already active
   on `conn` when the build starts.
2. Fail loudly if `conn.autocommit` is `True` -- verified empirically (see
   below) that autocommit defeats REPEATABLE READ's snapshot guarantee
   entirely, which would silently void this whole section's purpose.
3. Save the connection's prior isolation level before changing it.
4. Set `REPEATABLE READ` before any diagnostics query runs.
5. Execute all reads inside that single transaction.
6. Commit on success, roll back on any failure.
7. Restore the connection's prior isolation level in a `finally`, handling
   the `ISOLATION_LEVEL_DEFAULT`/`None` case specially as shown above, so
   the caller's connection is left exactly as it was regardless of outcome.

If an existing project transaction-scoping helper already does this
correctly, reuse it — but this behavioral contract governs regardless of
which helper implements it.

**Both of the above were verified empirically against a real PostgreSQL 16
server, not just assumed from documentation:**

- Restoration bug: starting from the driver default, setting REPEATABLE
  READ, then "restoring" via `set_session(isolation_level=None)` leaves the
  session's `transaction_isolation` at `repeatable read` -- confirmed via
  `SHOW transaction_isolation`. Restoring via `set_session(isolation_level=
  "DEFAULT")` correctly returns it to `read committed`.
- Autocommit hole: with `autocommit=True` and `REPEATABLE READ` set, a
  second `SELECT` on the same connection saw a row a concurrent connection
  had committed in between the two reads -- i.e. no snapshot was actually
  held. With `autocommit=False`, the second read correctly did not see the
  concurrently committed row.

`generated_at` (§7) is captured exactly once, immediately before the first
diagnostics data query — after the `REPEATABLE READ` transaction has been
configured. This is a close proxy for, but not literally identical to, the
instant PostgreSQL establishes the REPEATABLE READ snapshot (which happens
at that first query's execution) -- the distinction does not matter for
`generated_at`'s only actual use (the epoch-membership cutoff, §7), so it
is not overclaimed as the literal snapshot instant. Concurrent scoring
writes that commit after that snapshot is established are simply not
visible to that run — they appear on the next report, not this one.

This is also what makes the §1 defensive `SelectionDiagnosticsInternalConsistencyError`
meaningful: under a single consistent snapshot, a catalyst the builder
already measured as N > 0 cannot then evaluate to N = 0 inside
`assert_trade_abstention_invariant` — if it does, that is a real bug, not
a timing artifact.

## 3. `NO_ELIGIBLE_CANDIDATES` is catalyst-level, not per-arm

Verified against `assert_trade_abstention_invariant`'s own eligibility
query: `candidate_signals.eligibility_status` carries no `model_id`, so
eligibility is identical for Arm A and Arm G by construction. The fact
itself does not vary by arm -- consistent with §2's sequencing, where it is
resolved once per catalyst, before any per-arm classification, and never by
calling the per-arm classifier at all.

`SelectionDiagnosticsReport` carries a single, report-level
`no_eligible_candidates_count: int` -- never duplicated per arm.

## 4. Paired-ready intersection is the ONLY denominator for every comparative statistic

**Corrected from an earlier draft:** `classify_arm_decision_state` returns
`NOT_YET_SCORED` or a full `CatalystDecisionResult` (§1), not a bare
`TRADED`/`ABSTAINED` string — the pseudocode below must be read against
that real return type, and each catalyst/arm is classified exactly ONCE
(during §2's per-catalyst sequencing) and the result retained/cached, never
re-classified here. Re-invoking `classify_arm_decision_state` a second time
for the same (catalyst, arm) would not change the answer under the single
snapshot (§2a), but it would re-run coverage and invariant queries for no
reason:

```text
For each N>0 catalyst, classify each arm exactly once (in §2) and retain
A_result and G_result.

arm_ready_A  = catalysts where A_result is a CatalystDecisionResult
               and A_result.status in {TRADED, ABSTAINED}
               (i.e. A_result != NOT_YET_SCORED)

arm_ready_G  = catalysts where G_result is a CatalystDecisionResult
               and G_result.status in {TRADED, ABSTAINED}

paired_ready = arm_ready_A ∩ arm_ready_G
```

EVERY comparative number -- selected counts, eligible counts, selection
rates, HHI, effective-names, top-N rankings, and the paired trade/
abstention composition (§8) for BOTH arms -- is computed from
`paired_ready` alone. It is never the case that A's statistics are computed
over `arm_ready_A` (say, 5 catalysts) while G's are computed over
`arm_ready_G` (say, 3) and only `paired_decision_ready_catalysts=3` is
reported alongside as a caveat.

Per-arm staging counts are still reported separately, over each arm's own
full ready set, so lag stays visible and is clearly distinguished from the
paired comparative universe:

```text
A_not_yet_scored, G_not_yet_scored
A_decision_ready, G_decision_ready   (each arm's own full ready count, pre-pairing)
paired_decision_ready                (the actual comparative-statistics universe)
```

**Explicit cross-arm invariant, tested directly:** because candidate
eligibility (`candidate_signals.eligibility_status`) is shared and does not
depend on which model is asking, both arms must see the exact same
opportunity universe over `paired_ready`:

**Corrected from an earlier draft:** `A.entities`/`G.entities` are lists of
`EntitySelectionDiagnostic` objects, not bare key strings — the set must be
built from each object's `.entity_key` attribute, not from the objects
themselves:

```python
{e.entity_key for e in A.entities} == {e.entity_key for e in G.entities}

for i in {e.entity_key for e in A.entities}:  # == the G set too, by the check above
    eligible_count[i, A] == eligible_count[i, G]
```

The entity-key-SET equality is checked first and separately from the
per-entity count equality: checking only the counts for entities that
happen to appear on both sides would miss a bug where one arm's
aggregation entirely omits an eligible-but-never-selected entity rather
than merely mis-counting it. Only `selected_count[i, A]` and
`selected_count[i, G]` may legitimately differ. A violation of either part
of this invariant means opportunity counts were accumulated incorrectly or
before intersecting the arm-ready sets -- a real implementation bug, not a
legitimate result.

## 5. Opportunity counting: entity × catalyst, not per candidate row

An entity can appear as more than one `candidate_signals` row under the
same catalyst (e.g. three separate `event_version`s referencing the same
company). This must contribute exactly one eligible-opportunity count for
that entity in that catalyst, not three. `get_eligible_entities_for_catalyst`
(new, small -- a distinct-entity projection over the existing eligibility
query) returns a distinct entity set per catalyst.

**`get_eligible_entities_for_catalyst` must use exactly the same eligible
candidate universe as the decision invariant it sits beside, no more and
no less:** all and only eligible candidates (`candidate_signals` rows with
`eligibility_status = 'eligible'`) under the catalyst's CURRENT
`event_version`s -- i.e. built from `get_current_event_versions_for_catalyst`,
the same function `assert_trade_abstention_invariant` and §1's coverage
check both already use. It must never include a candidate attached only to
a superseded/historical `event_version`. This is an explicit restatement of
something §1 already relies on implicitly (its eligibility query is scoped
to `get_current_event_versions_for_catalyst`'s output) -- called out here
so the opportunity-counting helper is built the same way, not
independently reinvented against a different (and possibly broader) query.

At most one entity can be `selected=true` per catalyst per arm -- guaranteed
by `assert_trade_abstention_invariant`'s existing rank=1-only-selection
invariant (independent of `experiments.max_positions_per_catalyst`, which is
an execution-layer field for a different purpose and must not be conflated
with this decision-layer guarantee).

```python
SelectionRate[i, arm] = (
    count(paired-ready catalysts where i selected by arm)
    / count(paired-ready catalysts where i eligible)
)
```

**Derived invariant, tested directly:** for every entity `i` and arm,
`0 <= selected_count[i, arm] <= eligible_count[i, arm]`, and therefore
`0.0 <= selection_rate[i, arm] <= 1.0`. A selected count can never exceed
the number of paired-ready catalysts in which that entity was even
eligible -- a violation means a selection was attributed to an entity
outside the eligible universe the opportunity side is counting over,
almost certainly a resolution bug (see §5a).

## 5a. Freezing `selected_candidate_id` → entity resolution

The classifier (§1) hands the builder `result.selected_candidate_id` for a
`TRADED` catalyst -- a `candidate_signals.candidate_id`, not an entity.
Verified against schema.sql: `candidate_signals.entity_id` is a `NOT NULL`
foreign key to `entities(entity_id)`, so resolving the selected candidate
to an entity is a direct lookup, not a judgment call -- but the exact
resolution steps and their failure handling must be spelled out so Claude
Code isn't left to invent which entity a selected candidate represents.

For a `TRADED` result, in order:

1. Look up `result.selected_candidate_id` in `candidate_signals` and read
   its `entity_id` directly (the `NOT NULL` FK, no join ambiguity).
2. Require exactly one matching `candidate_signals` row for that
   `candidate_id` -- it is a primary key, so this is a sanity assertion,
   not a real branch, but assert it rather than assuming.
3. Require that candidate to belong to one of this catalyst's CURRENT
   `event_version`s (the same set `get_current_event_versions_for_catalyst`
   returns, per §5) -- not a superseded one.
4. Require that SPECIFIC candidate row's own `eligibility_status ==
   'eligible'`. This is a narrower, distinct check from step 5 below and
   must not be skipped in favor of it: an entity can have more than one
   `candidate_signals` row under the same catalyst (§5), so checking only
   that the entity appears somewhere in the eligible-entity set would still
   pass if the SELECTED candidate row itself were ineligible but the same
   entity had a separate, eligible candidate row. `assert_trade_abstention_invariant`
   should already make this unreachable (it only ranks/selects among rows
   with `eligibility_status = 'eligible'` in the first place), but §5a is
   deliberately a fail-closed consistency boundary independent of that --
   it verifies the actual selected row, not a proxy fact about the entity.
5. Require the resolved `entity_id` to be present in
   `get_eligible_entities_for_catalyst(catalyst_id)`'s output for this
   catalyst.
6. Convert that `entity_id` to the report's durable identity via
   `confirmatory_analysis.stable_entity_sort_key` (§6) -- never report the
   raw `entity_id` itself, consistent with §6's existing rule.

**Any failure of steps 2-5 is `SelectionDiagnosticsInternalConsistencyError`
-- never a silently skipped or dropped selection.** A selected candidate
that doesn't resolve cleanly to an eligible entity in this catalyst's
current universe is a real data-integrity problem (e.g. the selection and
the eligibility computation disagreeing about which event_version is
current, or about the selected row's own eligibility), not a case to
quietly omit from the count.

## 6. Durable entity identity: reuse, do not reimplement — and fail loudly on a same-snapshot collision

The report's `EntitySelectionDiagnostic` keys entities by
`confirmatory_analysis.stable_entity_sort_key` (`CIK:<10-digit CIK>` or
`NAME:<normalized legal name>`) -- imported and called directly, NOT
reimplemented. Verified: it is already a plain module-level function in
`confirmatory_analysis.py`, and `confirmatory_builder.py` already does
`import confirmatory_analysis` and calls into it -- this is a zero-cost
reuse, not a refactor. That function has already been hardened over
several review rounds (malformed-CIK rejection, whitespace, zero, >10
digits, normalized-name fallback, empty-name failure); a second
implementation in the diagnostics module would only create an opportunity
for the two to drift apart.

`entity_id` may be carried as optional debug metadata but is never the
report's scientific identity key -- it is documented in
`stable_entity_sort_key`'s own docstring as non-deterministic across
from-scratch DB rebuilds. That non-determinism property is specifically
about the SAME logical entity across DIFFERENT database instances/rebuilds
-- it must not be read as license for two DIFFERENT current entity rows,
within the SAME database snapshot, to silently collide onto one key.

**Requirement:** within one report build, if two distinct `entity_id`
values both resolve to the same `stable_entity_sort_key`, raise
`EntityKeyCollisionError` (or similar) rather than silently aggregating
their counts together. This mirrors the project's existing
fail-closed-on-ambiguity discipline (e.g. `UnresolvedRankingTieError`) --
a same-snapshot collision means two un-merged entity-master rows for what
should be one economic entity, a real data-quality problem the report
must surface, not paper over.

```python
@dataclass(frozen=True)
class EntitySelectionDiagnostic:
    entity_key: str
    legal_name: str
    eligible_count: int
    selected_count: int
    selection_rate: float
```

## 7. `generated_at`: captured internally, never caller-supplied

Verified against schema.sql: `model_candidate_decisions.decision_at` is a
real, non-null column, so a true point-in-time reconstruction of past
coverage state is schema-feasible in principle. However:

- `find_incomplete_coverage()` does not filter by `decision_at` today -- it
  queries current persisted state unconditionally.
- Unlike `experiment_catalysts`, `model_candidate_decisions` has no
  append-only/immutability trigger -- nothing in the schema guarantees a
  decision row is never revised after insertion (only a `UNIQUE
  (candidate_id, model_id, model_version)` constraint, which prevents a
  second row for the same triple, not an UPDATE to the first one).

Separately: `analysis_as_of` is already an established parameter name in
this codebase, in `confirmatory_builder.py` -- but there it is
load-bearing, compared directly against `outcome_due_at(entry_timestamp) +
OUTCOME_PROCESSING_GRACE` to decide real classification outcomes. Reusing
that name (or exposing any caller-settable timestamp) here would invite
exactly the "historical snapshot" misreading that must be avoided, since a
caller could set it to a past date while the underlying reads still
reflect current state.

**Resolution: `generated_at` is not a parameter at all.** It is a plain
local value captured once — immediately before the first diagnostics data
query, after the `REPEATABLE READ` transaction is configured (§2a) — using
the same pattern already used everywhere else in this codebase
(`extraction_runner.py`, `manual_resolve.py`): a direct
`datetime.now(timezone.utc)` call, no clock-injection abstraction, since
none exists elsewhere in this project and introducing one here would be
inconsistent with it. See §2a for the full function body including the
transaction contract; the signature itself is:

```python
def build_selection_diagnostics(
    conn,
    experiment_id: str,
    scoring_epoch: str,
    model_specs: dict[str, tuple[str, str]],
) -> SelectionDiagnosticsReport:
```

`generated_at` governs exactly one thing: `experiment_catalysts.admitted_at
<= generated_at`, i.e. epoch membership as of the moment the report is
generated. It is also stamped onto the report as its generation timestamp.
There is no code path, public or private, by which a caller can supply a
different value -- eliminating the inconsistency risk entirely rather than
just documenting around it.

**`model_specs` validation:** the very first thing `build_selection_diagnostics`
does (before opening the transaction) is validate `set(model_specs) ==
{"A", "G"}`, raising `ValueError` immediately otherwise. This report exists
specifically for the A-vs-G comparison; nothing here generalizes to other
arm sets, and silently accepting `{"A"}`, a third key, or different key
names would just push that decision onto Claude Code with no right answer
to pick.

Tested the same way `test_manual_resolve.py` already tests
`datetime.now(timezone.utc)`-based fields: capture `before =
datetime.now(timezone.utc)` and `after = datetime.now(timezone.utc)`
around the call and assert `before <= report.generated_at <= after`.

## 8. Data model

```python
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

    # Paired-ready comparative statistics -- computed ONLY over paired_ready (§4).
    paired_traded_count: int
    paired_abstained_count: int
    # Invariant, tested: paired_traded_count + paired_abstained_count == paired_decision_ready_catalysts (report-level)

    distinct_selected_entities: int
    top_entities: list[EntitySelectionDiagnostic]   # paired_ready only; selected_count > 0 only; see ordering rule below
    entities: list[EntitySelectionDiagnostic]        # paired_ready only; full opportunity table, INCLUDING zero-selected entities

    hhi: float | None
    effective_names: float | None


@dataclass(frozen=True)
class SelectionDiagnosticsReport:
    experiment_id: str
    scoring_epoch: str
    generated_at: datetime

    total_epoch_catalysts: int
    no_eligible_candidates_count: int      # report-level (§3)

    paired_decision_ready_catalysts: int

    per_arm: dict[str, ArmSelectionDiagnostics]   # keys: 'A', 'G'

    note: str = (
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
```

**HHI / effective-names undefined-case handling:**

```text
zero selections (distinct_selected_entities == 0):
    hhi = None
    effective_names = None
    top_entities = []

one selected entity:
    hhi = 1.0
    effective_names = 1.0

S > 0 total selections generally:
    s_i = selected_count_i / S          (computed from exact integer counts;
    HHI = sum(s_i ** 2)                  convert to float only at the
    effective_names = 1 / HHI            reporting boundary, not mid-calculation)
```

`hhi = 0` is never used for the no-selections case -- 0 is the
maximal-dispersion value of the index, the opposite of what "no data"
means, and `effective_names = 1/hhi` is undefined at `hhi = 0` regardless.

**`top_entities` composition and ordering:** `top_entities` is the first 10
entities with `selected_count > 0`, ordered by `selected_count DESC,
entity_key ASC`; if there are zero selections, `top_entities = []`. An
entity that is eligible but was never selected belongs in `entities` only,
never in `top_entities` -- sorting the full opportunity table by
`selected_count DESC` and truncating to 10 would let zero-selection
entities crowd out real selections whenever fewer than 10 entities were
ever actually picked. `entities` remains the complete paired-ready
opportunity table, including zero-selected entities, ordered by the same
`selected_count DESC, entity_key ASC` rule. This is descriptive ordering,
not a selection decision, so unlike the trade/abstention invariant's
ranking it does not need to fail closed on an exact tie -- the entity key
alone is sufficient to give a total, reproducible order across repeated
report runs.

## 9. Required tests

- `classify_arm_decision_state`: complete coverage + TRADED -> returns the
  full `CatalystDecisionResult` with `status == TRADED` and a non-None
  `selected_candidate_id` matching the real selected candidate; complete
  coverage + ABSTAINED -> returns the full result with `status ==
  ABSTAINED` and `selected_candidate_id is None`; incomplete coverage ->
  `NOT_YET_SCORED` (built on `find_incomplete_coverage`, not by catching
  `assert_full_coverage`'s `ValueError` -- assert this via a spy/mock
  confirming `assert_full_coverage` is never called in this code path);
  malformed decision set -> hard failure propagates unchanged; a fixture
  that forces `assert_trade_abstention_invariant` to return
  `NO_ELIGIBLE_CANDIDATES` despite the N > 0 precondition ->
  `SelectionDiagnosticsInternalConsistencyError`; a fixture that forces a
  `TRADED` result with `selected_candidate_id is None` ->
  `SelectionDiagnosticsInternalConsistencyError`; a fixture that forces an
  `ABSTAINED` result with `selected_candidate_id` set ->
  `SelectionDiagnosticsInternalConsistencyError`.
- §2 sequencing: a fixture with N=0 eligible candidates asserts
  `classify_arm_decision_state` is never called for either arm, and that
  neither arm's `decision_ready_catalysts`/`not_yet_scored_count`/
  `paired_traded_count`/`paired_abstained_count` is incremented.
- §2a transaction contract: a test confirming `build_selection_diagnostics`
  raises `ValueError` immediately if called on a `conn` that already has a
  transaction in progress, without touching isolation level or running any
  query; a test confirming it raises `ValueError` immediately if
  `conn.autocommit` is `True`, before any diagnostics query runs; a test
  confirming the build runs inside a single `REPEATABLE READ` transaction
  (or the project's equivalent) -- e.g. by asserting a concurrent commit
  made after the build starts is not reflected in that build's results; a
  test confirming `generated_at` is captured exactly once, after the
  transaction is configured and before the first data query, not re-read
  per query; and isolation-level restoration tests against a real
  connection: starting from the driver default (`conn.isolation_level is
  None`) and restoring afterward must leave `SHOW transaction_isolation`
  at the true server default (`read committed`), not stuck at `repeatable
  read` -- both after a successful build and after one that raises partway
  through; starting from an explicit non-default prior (e.g. `READ
  COMMITTED`) must restore to that same explicit value, also on both the
  success and failure paths.
- Reconciliation: `total_epoch_catalysts == no_eligible_candidates_count +
  paired_decision_ready_catalysts + a_only_ready_count + g_only_ready_count
  + neither_ready_count` over a fixture engineered to hit every category
  (note: every term is added; there is no subtraction in this formula).
- Paired-ready intersection: a fixture where A is ready on catalysts
  {1,2,3,4,5} and G is ready on {1,2,3} -- assert `paired_decision_ready ==
  {1,2,3}` and that HHI/selection-rate/eligible-counts/`paired_traded_count`/
  `paired_abstained_count` for BOTH arms are computed only over {1,2,3},
  never over A's full 5 or G's full 3.
- Per-arm composition invariant: for each arm, `paired_traded_count +
  paired_abstained_count == paired_decision_ready_catalysts`.
- Cross-arm opportunity-universe invariant: over the paired-ready fixture
  above, assert `{e.entity_key for e in A.entities} == {e.entity_key for e in G.entities}`
  FIRST (catching a bug where one arm's aggregation omits an
  eligible-but-never-selected entity entirely), then assert
  `eligible_count[i, A] == eligible_count[i, G]` for every entity_key `i`
  in that set. A fixture where G's aggregation is deliberately missing one
  eligible entity that A has must fail the set-equality check even though
  no per-entity count comparison would have caught it.
- `model_specs` validation: `{"A": (...), "G": (...)}` succeeds;
  `{"A": (...)}` alone, a third key, or differently-named keys each raise
  `ValueError` immediately, before the transaction (§2a) is ever opened.
- Entity x catalyst opportunity de-duplication: a fixture with the same
  entity appearing via 3 `candidate_signals` rows under one catalyst --
  assert `eligible_count` contributes exactly 1, not 3.
- `get_eligible_entities_for_catalyst` current-vs-superseded scoping: a
  fixture with entity X eligible under the catalyst's current
  `event_version` and entity Y eligible only under a superseded
  `event_version` -- assert the returned eligible entity set is `{X}`, not
  `{X, Y}`.
- Selected-candidate resolution (§5a): a normal `TRADED` fixture asserts
  the resolved `entity_key` matches the selected candidate's real entity;
  a fixture where the selected candidate's `entity_id` is (artificially)
  not in `get_eligible_entities_for_catalyst`'s output asserts
  `SelectionDiagnosticsInternalConsistencyError`, not a silently dropped
  selection; a fixture where the selected candidate belongs to a
  superseded `event_version` asserts the same.
- Selected-candidate-row-itself eligibility (§5a step 4): an entity with
  two `candidate_signals` rows under one catalyst -- candidate A eligible,
  candidate B ineligible -- with the `TRADED` result's
  `selected_candidate_id` artificially forced to candidate B. Asserts
  `SelectionDiagnosticsInternalConsistencyError`, and specifically that
  this must NOT pass merely because candidate A's eligibility puts the
  same entity into `get_eligible_entities_for_catalyst`'s output -- the
  check on the selected row's own `eligibility_status` (step 4) must run
  and must be what catches this, not step 5's entity-level check.
- Derived bounds invariant: over a fixture with several entities at
  different selection frequencies, assert `0 <= selected_count[i, arm] <=
  eligible_count[i, arm]` and `0.0 <= selection_rate[i, arm] <= 1.0` for
  every entity and arm.
- HHI/effective-names: zero-selection case -> both `None`; one-entity case
  -> both `1.0`; a multi-entity concentrated case computed by hand and
  checked against the formula.
- `top_entities` composition: a fixture with 3 selected entities and 7
  eligible-but-never-selected entities -- assert `top_entities` contains
  only the 3 selected ones, while `entities` contains all 10; a
  zero-selection fixture asserts `top_entities == []`.
- Deterministic ordering: a fixture with two entities tied on
  `selected_count` -- assert stable ordering by `entity_key ASC` across
  repeated calls, in both `entities` and `top_entities`.
- Entity identity reuse: assert `confirmatory_analysis.stable_entity_sort_key`
  is imported and called (not reimplemented); separately, assert
  `entity_id` never appears anywhere in the public dataclasses.
- Entity identity collision (same snapshot): a fixture with two distinct
  `entity_id`s that both resolve to the same `stable_entity_sort_key`
  within one build -- assert `EntityKeyCollisionError` is raised.
- Entity identity stability (across rebuilds): the SAME logical entity
  (same CIK) with two DIFFERENT `entity_id`s in two SEPARATE fixture
  databases (simulating a rebuild) resolves to the same `entity_key` in
  each -- this is tested as two independent single-database fixtures, never
  as two colliding rows inside one database.
- `generated_at`: a test confirming `build_selection_diagnostics` has no
  parameter accepting a caller-supplied timestamp; a test confirming
  `find_incomplete_coverage`/`model_candidate_decisions` queries are never
  filtered by `decision_at` (proving the "current state only" claim, not
  just asserting the docstring says so); a test confirming `before <=
  report.generated_at <= after` around the call; a test confirming epoch
  membership correctly uses `experiment_catalysts.admitted_at <=
  generated_at` (reusing Section 7f's existing membership machinery, not
  reinventing it).
- `NOT_YET_SCORED` equality contract: assert `NOT_YET_SCORED ==
  "NOT_YET_SCORED"` (it is a plain string constant); assert a `result`
  built from a freshly-constructed string with the same content (not the
  same object) still compares equal via `==` -- guarding against an
  accidental `is`-based implementation that would only happen to pass
  under CPython string interning.

## 10. Deferred (out of scope for this commit)

- Beta-tilt diagnostic (`market_data.beta_market` / `beta_sector` -- no
  producer exists anywhere in the repo).
- Sector/industry enrichment of the concentration measures.
- True point-in-time historical reconstruction of past coverage/decision
  state (would require both a `decision_at`-aware coverage query and an
  immutability guarantee on `model_candidate_decisions` -- neither exists
  today).
