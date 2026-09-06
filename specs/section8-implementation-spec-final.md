# Section 8 Implementation Spec — FINAL

This document is self-contained and authoritative. It supersedes every
earlier draft (v1–v8) discussed during review; if anything you find
elsewhere conflicts with this document, this document controls. It
implements everything Section 8 of `specs/diffusion-experiment-spec-v2.2.1.md`
left as placeholders, together with catalyst-level decision invariants and an
outcome/execution-pricing contract that Section 8 required but never
specified. It does NOT touch the arm structure, the underreaction estimator,
the bitemporal model, or Section 14's operating plan — all of that is settled
and out of scope here.

## 0. Scope and non-goals

In scope: filling in Section 8's frozen numeric/rule placeholders; enforcing
the catalyst-level trade/abstention invariant; defining how genuine
abstention enters the statistical comparison; and specifying the shadow
outcome/execution-pricing contract needed to compute `return_net_of_costs`
for the confirmatory `catalyst_clustered_test`.

Explicitly out of scope, not to be added speculatively during this
implementation pass:

- No real broker/Robinhood execution or order placement.
- No `actual_fill` price-source support or durable fill-record storage.
- No opening-auction infrastructure or `quote_snapshots.trading_session`
  schema change (confirmed against Robinhood's own documentation: ordinary
  market orders placed outside regular hours are queued for the next
  regular-hours open with execution price not guaranteed, and Robinhood does
  not support Market-on-Open orders — there is no real auction-priced order
  type behind what this project would actually place).
- No change to the arm structure, the estimator, or the bitemporal model.
- No adjustment of any frozen Section 8 value based on anything discovered
  while implementing this.

## 1. Why catalyst-wide, not event-version-wide (data model grounding)

The real schema chain is:

```
catalysts (1) -> canonical_events (many, no 1:1 constraint)
canonical_events (1) -> event_versions (many, versioned)
event_versions (1) -> candidate_signals (many)
```

The schema's own comment on `event_document_links` states outright: "a
single earnings release can support several distinct events at once." This
is not theoretical — real Dry Run 002 extractions already produced 8
separate events from a single exhibit (Broadcom's EX-99.1, and separately
lululemon's EX-99.1). Every rule below that says "per catalyst" is scoped
that way specifically because scoping by `event_version_id` instead would
allow a single filing to produce multiple simultaneous selected positions
under one catalyst, directly violating `max_positions_per_catalyst = 1`.

`event_versions.version_number` is currently hard-coded to `1` for every
event system-wide (`extraction_runner.py`'s own module docstring flags this
as a known, deliberate gap: no cross-catalyst event-matching mechanism
exists yet to drive real versioning). Everything below still selects on
`superseded_by IS NULL` rather than assuming `version_number = 1`, so it
stays correct once versioning/corrections are eventually implemented.

## 2. Frozen Section 8 parameters

- `H = "1day"` — see Section 5b for the exact temporal definition.
- `delta = 0.005`: the minimum acceptable mean CATALYST-level A-minus-G
  policy-return difference, log-return, net-of-costs scale (not "per-event"
  — each arm selects at most one position per catalyst, so
  `D_c = R_A,c - R_G,c` is the actual confirmatory observation). Justified by
  account-level materiality (at 5% notional sizing, a 0.005 per-event edge is
  2.5bps of account equity per selected event; illustrative volume framing
  is explanatory context only) — never a trading-cost multiple, never
  derived from a specific broker's fee schedule, and never adaptively
  re-tuned based on realized event frequency or statistical power.
- `A_position_rule` / `G_position_rule`: rank all eligible candidates for a
  catalyst by the arm's own estimated UR (descending); select the single
  highest-ranked candidate if its score is strictly `> 0`; otherwise abstain.
  Both arms use the identical selection mechanism — only the underlying
  score differs. Tie-break: Section 6.
- `notional_per_event` = 5% of `REFERENCE_EQUITY_USD` for confirmatory shadow
  outcome construction (Section 5d) — a separate concept from 5% of actual
  live account equity, which Section 14 uses for real operational sizing.
- `max_positions_per_catalyst = 1` (per arm), catalyst-scoped per Section 1.
- `abstention_rule`: abstain if no eligible candidate has a finite estimated
  UR strictly `> 0`. A score of exactly `0` counts as abstain, not select —
  freeze `> 0`, never `>= 0`. A missing/uncomputable score is a coverage
  failure (`assert_full_coverage`), not an abstention.
- `direction_rule` = long-only for Phase II. (Not "only positive source
  shocks" — a negative shock to one company can produce `UR > 0` for a
  competitor, bought long. What's excluded is ever shorting `UR < 0`.)
- `inference_method` = the existing `catalyst_clustered_test` in
  `build/statistical_test.py`, unchanged. For the real confirmatory
  promotion run: `n_bootstrap = 10000` and `CONFIRMATORY_BOOTSTRAP_SEED =
  20260906` (pre-specified now, never chosen post-hoc). Routine dev tests
  keep `n_bootstrap = 2000` as today. Promotion criterion unchanged: the
  bootstrap CI's lower bound must exceed `delta`.
- A candidate score that is `NULL`, `NaN`, or `Infinity`/`-Infinity` is
  invalid input, rejected as a coverage/model-output failure — never
  eligible for ranking or treated as an abstention. (Postgres `NUMERIC` can
  store the literal `NaN`, so this is an explicit application-level check,
  not something the column type prevents.)

## 3. Decision-recording row-level contract

Per **catalyst**, per arm (`model_id`, `model_version`), across the union of
eligible candidates from **all current event_versions belonging to that
catalyst** (never per individual event_version):

```
rank 1, score > 0   -> selected=true,  abstained=false
rank 1, score <= 0  -> selected=false, abstained=true
rank 2+ (always)    -> selected=false, abstained=false
```

`assert_full_coverage` (existing, unchanged) remains scoped per
`event_version_id` — that completeness check was already correctly scoped.
Ranking and trade/abstention semantics sit catalyst-wide, on top of it.

Abstention-rate reporting unit: `(catalyst_id, model_id, model_version)`,
denominator = catalysts with `N >= 1` eligible candidates only. Never
`AVG(model_candidate_decisions.abstained)` across raw candidate rows — that
denominator is distorted by candidate-set size per catalyst.

## 4. Catalyst-level invariant functions

```python
def get_current_event_versions_for_catalyst(conn, catalyst_id):
    """All event_versions where superseded_by IS NULL, across every
    canonical_event under this catalyst_id."""
```

```python
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
    eligible candidates). Return a distinct NO_ELIGIBLE_CANDIDATES result
    rather than raising or fabricating a trade/abstain row. Callers must NOT
    count this catalyst as an A/G policy observation, must NOT include it in
    the confirmatory D_c set, and must NOT include it in the abstention-rate
    denominator -- report it separately as
    catalysts_with_zero_eligible_candidates.

    Otherwise (N >= 1), enforce over the union of eligible candidates:
      1. Dense 1..N ranking, no duplicates or gaps, using the tie-break in
         Section 6. ANY unresolved exact tie anywhere in this ranking (not
         only at the position that would determine selection) raises
         UnresolvedRankingTieError -- Section 3's promise of a dense,
         reproducible ranking is about the whole ordering, not just about
         who gets selected.
      2. Exactly one of: trade (rank=1 row selected=true, zero abstained=true
         anywhere in the set) or abstain (zero selected=true, rank=1 row
         abstained=true).
      3. Every rank > 1 row: selected=false AND abstained=false.
      4. rank=1's selected/abstained value matches its score sign
         (Section 3).
    """
```

```python
def assert_decision_set_ready_for_comparison(conn, catalyst_id, model_specs):
    """model_specs: list of (model_id, model_version) tuples -- the two
    identities are independent, never assumed to share a version string.
    For each pair: calls assert_full_coverage for every current
    event_version under catalyst_id, then assert_trade_abstention_invariant.
    Propagates the NO_ELIGIBLE_CANDIDATES result rather than treating it as
    success or failure -- callers (Section 5f) decide what to do with it."""
```

## 5. Ranking tie-break: `stable_entity_sort_key`

The same `entity_id` can legitimately appear as a candidate under two
different `event_versions` of one catalyst (the schema's only relevant
constraint, `UNIQUE(event_version_id, entity_id)`, does nothing across
event_versions), so a bare `entity_id` tie-break is not a total order over
the catalyst-wide candidate union. Checked directly: `candidate_id`,
`canonical_event_id`, `event_version_id`, and `entity_id` are all
`gen_random_uuid()`/`uuid.uuid4()`-assigned — `entity_id` specifically is
reused within one database's lifetime (`seed_entities.py`'s
`find_entity_by_cik` reuses an existing row when one exists) but gets a
fresh random value on any independent from-scratch rebuild. This is not
hypothetical for this project: a disposable database was already lost and
rebuilt once, during Dry Run 002.

`entities.cik` (SEC Central Index Key) is genuinely external and
rebuild-stable — looked up, never regenerated — but nullable ("where
applicable"), so it can't fully replace `entity_id` alone. The final rule:
prefer CIK; when CIK is genuinely absent, fall back to a deterministic
normalization of `legal_name` (reusing the existing, verified-pure
`normalize_entity_name()` from `entity_resolution.py` — lowercase, strip
punctuation, strip a trailing corporate suffix; no DB access, no
randomness); if both are unusable, or an exact tie remains, fail closed.
`entity_id` is never used as a ranking discriminator.

```python
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
```

Ranking key: `score DESC, stable_entity_sort_key ASC`. This project's actual
entity-seeding pipeline (`seed_entities.py`) always sources `cik` from a real
CSV value, so every currently-seeded entity has a genuine CIK; the
name-fallback path exists for a future entity type not currently present in
the database, and the stricter malformed-CIK check will not spuriously
reject anything already seeded.

## 6. Abstention's contribution to the statistical comparison

Never addressed anywhere in the design docs — confirmed by search; the spec
contains no mention of "abstain" at all. Per catalyst `c` with `N >= 1`
eligible candidates and no unresolved ranking tie:

```
R_arm,c = R_net_of_costs(selected outcome)   if arm selected a rank-1 candidate (traded)
R_arm,c = 0                                   if arm genuinely abstained
D_c = R_A,c - R_G,c
```

An abstaining arm requires no `arm_entries`/`arm_outcomes` row, no quote
snapshot, nothing — its contribution is the literal value `0`, not a missing
observation. This is required specifically because dropping abstention
catalysts from the confirmatory sample would condition the sample on which
catalysts each arm chose to trade, silently biasing the very comparison this
whole design exists to make.

A `NO_ELIGIBLE_CANDIDATES` catalyst (Section 4) is different again: it does
not enter the `D_c` set at all, in either arm's favor, and is reported under
its own separate count (`catalysts_with_zero_eligible_candidates`).

`assert_outcome_ready_for_confirmation` (Section 7e) is only ever called for
an arm that actually traded.

## 7. Outcome / execution-pricing contract

### 7a. Reuse `quote_snapshots`; no new market-data table

`arm_entries.entry_quote_snapshot_id` and `arm_outcomes.exit_quote_snapshot_id`
already FK into `quote_snapshots` (`bid`, `ask`, `bid_size`, `ask_size`,
`mid`, `last`, `quote_timestamp`, `data_provider`, `trading_session`,
`staleness_seconds`), with existing triggers enforcing instrument
consistency. No changes to `quote_snapshots` or its `trading_session` CHECK
(`'regular'|'pre_market'|'after_hours'`).

### 7b. Entry/exit price-sourcing rule and temporal semantics

- **Entry**: target time 09:30:00 America/New_York (exchange trading
  calendar — actual calendar days accounting for weekends/holidays/DST,
  never a hard-coded UTC offset) on the entry trading session. First valid
  `'regular'`-session `quote_snapshots` row for the instrument at or after
  target time, within `MAX_QUOTE_LOOKUP_DELAY_SECONDS` AND with staleness
  within `MAX_QUOTE_STALENESS_SECONDS` (two separate deferred constants —
  Section 9; `staleness_seconds` is an existing, distinct schema column from
  `quote_timestamp`-based lookup delay). Entry price = that snapshot's
  `ask`.
- **Exit, horizon-triggered, H=1day specifically**: target time 09:30:00
  America/New_York on the NEXT regular trading session after entry — this is
  the only reading consistent with Section 14's single once-daily,
  before-market-open review ritual, which both opens new approved positions
  and closes yesterday's 1-day positions in the same daily check. Same
  lookup-delay/staleness rules. Exit price = that snapshot's `bid`.
  `exit_reason = 'horizon'`.
- **Exit, stop-triggered**: once the (separately specified) deterministic
  stop-trigger rule fires, first qualifying executable `bid` snapshot under
  the same rules. `exit_reason = 'protective_stop'`.
- **No fallback to `mid`/`last`, ever.** No qualifying quote within the
  window = outcome not confirmatory-valid.
- Opening-auction pricing is explicitly rejected for Phase II (Section 0).
- Symmetric for Arm A and Arm G, staying symmetric even after real trading
  begins: the confirmatory comparison always uses this shadow-NBBO
  methodology for both arms. Any actual executed fill (once real broker
  execution exists) is stored separately for operational P&L only and never
  substituted into one arm's confirmatory return but not the other's.
- Scope note: this section covers the primary confirmatory A/G outcome path
  only. Arm F's delayed-entry decay-ladder mechanics remain diagnostic and
  are NOT generalized from this immediate-entry rule.

**Stop-gating — blocks ALL traded outcomes, not only `protective_stop`
rows.** Section 14 of the design spec states outright that "a broker-native
protective stop-loss order is placed immediately alongside" every approved
trade, and that "positions are protected by their own standing stop-loss
orders" between the once-daily reviews. A stop-loss is a structural part of
every position in the deployed policy, not an occasional feature. That means
a `horizon`-labeled shadow outcome that never checks whether a stop would
have fired between entry and the next day's open is not simulating the
frozen policy — it's simulating an unhedged variant that happens to share
the same entry rule. Until the deterministic protective-stop rule and
durable trigger evaluation are implemented:

> **No traded A/G outcome is confirmatory-valid** — this applies to
> `exit_reason='horizon'` exactly as much as `exit_reason='protective_stop'`.
> Once the stop implementation lands, validation becomes: a `horizon`
> outcome must prove no valid stop trigger occurred before its horizon exit;
> a `protective_stop` outcome must prove the trigger occurred, its
> timestamp, the stop-rule version, and the resulting executable exit
> pricing.

No speculative stop-related columns are added in this implementation pass.

### 7c. Migration — new `arm_outcomes` columns, no silent defaults

```sql
-- Precondition: this migration must fail loudly if arm_outcomes already has
-- any rows (confirmed today: zero execution/outcome-computation code exists
-- anywhere in the repo, so this table has no rows yet in practice -- but the
-- migration itself must assert this, not assume it).
ALTER TABLE arm_outcomes
    ADD COLUMN entry_price_source    TEXT NOT NULL CHECK (entry_price_source IN ('nbbo_side_proxy')),
    ADD COLUMN exit_price_source     TEXT NOT NULL CHECK (exit_price_source IN ('nbbo_side_proxy')),
    ADD COLUMN entry_fee             NUMERIC NOT NULL CHECK (entry_fee >= 0),
    ADD COLUMN exit_fee              NUMERIC NOT NULL CHECK (exit_fee >= 0),
    ADD COLUMN return_method_version TEXT NOT NULL,
    ADD COLUMN fee_method_version    TEXT NOT NULL,
    ADD COLUMN exit_reason           TEXT NOT NULL CHECK (exit_reason IN ('horizon', 'protective_stop'));
```

No `DEFAULT` on any of these — every writer must supply them explicitly, so
a forgotten fee or an unproven methodology surfaces as a write-time error,
not a silently-wrong `0` or `'nbbo_side_proxy'`. `entry_fee`/`exit_fee` are
non-negative USD amounts. `'actual_fill'` is intentionally excluded from
both price-source CHECKs — adding it requires durable fill-record storage
(e.g. `entry_fill_id`/`exit_fill_id`) that doesn't exist and is out of scope
here. No `confirmatory_execution_valid` column — validity is derived (7e),
matching the existing `assert_full_coverage` pattern of a function, not a
cached flag.

### 7d. Frozen return formula, with `REFERENCE_EQUITY_USD` and fractional quantity

A compounding per-arm equity curve would make `R_arm,c` a function of that
arm's entire prior history rather than primarily the current catalyst's
candidates — introducing path- and catalyst-order-dependence into what's
supposed to be a per-catalyst policy comparison, and is unnecessary since
`return_net_of_costs` is already a normalized log-return where `notional`'s
absolute size only matters through fee drag. Use a single frozen constant
instead:

```python
REFERENCE_EQUITY_USD = <one preregistered value, frozen before confirmatory collection begins>
NOTIONAL_FRACTION = 0.05
SHADOW_NOTIONAL_USD = REFERENCE_EQUITY_USD * NOTIONAL_FRACTION
```

For every catalyst and both arms: `notional_A,c = notional_G,c =
SHADOW_NOTIONAL_USD`. No compounding, no per-arm shadow account, no update
after wins or losses. `notional_per_event = 5%` remains the general policy
sizing parameter; for confirmatory shadow outcome construction specifically,
the equity base is this single preregistered constant, shared by both arms
and held constant across every catalyst. Real-money execution may later
apply the same 5% fraction to actual current account equity for operational
risk sizing (Section 14) — that operational calculation does not feed into
or get fed by the confirmatory A/G return calculation; they are two
different uses of the same 5% fraction, not one shared computation.

`REFERENCE_EQUITY_USD` is resolved either by the actual Phase II account's
initial funded equity (if fixed before confirmatory collection starts) or a
pre-registered value within Section 14's existing $1,000–3,000 range if
funding timing doesn't allow the former — either way, frozen before any
outcomes are observed, never adjusted afterward, and its provenance (which
of the two it is, and why) recorded explicitly. If the actual funded amount
is used and it falls outside the $1,000–3,000 envelope, that is a
discrepancy between preregistration and implementation to resolve explicitly
before confirmatory collection begins — never silently accepted merely
because it's the "real" number.

Return calculation:

```
q                    = SHADOW_NOTIONAL_USD / entry_ask
C_entry              = SHADOW_NOTIONAL_USD + entry_fee
C_exit               = q * exit_bid - exit_fee
return_gross         = ln(exit_bid / entry_ask)              -- before fees
return_net_of_costs  = ln(C_exit / C_entry)
return_method_version = 'shadow_nbbo_log_v1'
```

`q` is permitted to be fractional — this is a measurement normalization for
the statistical comparison, not a claim about a real broker order. Flooring
`q` to whole shares would make exposure vary arbitrarily with a candidate's
share price (a $100 shadow notional against a $500 stock must not become
`q=0`). Do not separately subtract an estimated spread cost — ask-to-bid
already embeds the full spread. Any fractional-share constraint a real
broker imposes is a separate, later, real-execution concern.

### 7e. `assert_outcome_ready_for_confirmation` — full integrity check

```python
def assert_outcome_ready_for_confirmation(conn, outcome_id):
    """Raises if this arm_outcomes row is not valid for the confirmatory
    A-vs-G statistical comparison. Only ever called for an arm that traded."""
```

Checks, all of the following:

- `entry_price_source`/`exit_price_source` are approved confirmatory values
  (currently only `'nbbo_side_proxy'`).
- Referenced entry/exit quote snapshots: `trading_session = 'regular'`,
  `bid > 0`, `ask > 0`, `ask >= bid` (a crossed market is a data-quality
  exclusion, not a policy edge case).
- **Quote size covers the shadow quantity**, not merely `> 0`: where the
  provider supplies size data, `ask_size_entry >= q` and `bid_size_exit >=
  q`, in shares, after the provider adapter normalizes whatever unit that
  provider reports into shares (`bid_size`/`ask_size` carry no documented
  unit anywhere in the schema today — normalization is the adapter's
  responsibility). A prior rule of merely "size `> 0`" only proves *some*
  quantity was available at the quoted price, not that the full shadow
  position was.
- `data_provider` equals the single frozen `CONFIRMATORY_DATA_PROVIDER`
  (Section 9) — not an allowlist; mixing providers within one confirmatory
  dataset would mix quote construction, latency, and staleness semantics for
  no benefit.
- Quote timestamps within `MAX_QUOTE_LOOKUP_DELAY_SECONDS` of target times,
  AND staleness within `MAX_QUOTE_STALENESS_SECONDS` — checked as two
  separate conditions.
- `exit_reason` is populated and consistent with which trigger rule produced
  the row, and (per 7b) `protective_stop` never passes until stop provenance
  exists — nor, currently, does `horizon`.
- `fee_method_version` equals the frozen value; the validator **recomputes
  the expected entry/exit fees** from that frozen fee methodology and
  asserts the stored `entry_fee`/`exit_fee` match — a wrong-but-internally-
  consistent fee pair must not pass merely because the return recomputes
  consistently from it.
- `return_method_version` equals `'shadow_nbbo_log_v1'` exactly.
- Domain/finiteness checks: `SHADOW_NOTIONAL_USD > 0`, `C_entry > 0`,
  `C_exit > 0` before evaluating either logarithm; stored `return_gross`/
  `return_net_of_costs` must be finite, not `NaN`/`Infinity`.
- **Recomputes** expected entry/exit prices from the referenced snapshots
  per direction (`ask` in, `bid` out) and recomputes `return_gross`/
  `return_net_of_costs` via the Section 7d formula (using the now-validated
  fees), asserting the stored values match within floating-point tolerance.
  A mismatch is a hard failure — inspecting only the referenced snapshot IDs
  cannot prove the stored numbers were actually derived from them.

**Two distinct levels of size/depth failure**: the confirmatory provider
adapter must, as a global capability requirement, define trustworthy
bid/ask-size semantics and normalize them to shares. Failure to establish
that capability (undocumented/unreliable units, inconsistent population, no
way to normalize to shares) makes the provider configuration incomplete and
causes `assert_confirmatory_configuration_complete()` (Section 8) to fail —
the experiment isn't configured, full stop. After such a provider has been
approved as capable, an individual snapshot with missing or insufficient
normalized executable-side size for one particular catalyst is a legitimate
quote-unobservable exclusion for that catalyst only (Section 7f), not a
configuration problem. The provider's size-unit normalization convention
(e.g. "round lots → shares") must be frozen together with the provider
configuration/version — changing that convention mid-collection is a
methodology change and must be recognized as such (a version bump), not a
silent adapter tweak.

### 7f. Statistical builder — fail-closed, three distinct outcomes per catalyst

For each catalyst, using the catalyst-level decision set (Section 4) and the
per-arm policy return (Section 6):

**Hard failure — abort the entire confirmatory build, do not continue:**
`assert_decision_set_ready_for_comparison` raising (including
`UnresolvedRankingTieError`); a traded arm's outcome failing
`assert_outcome_ready_for_confirmation` for a methodology/version/domain/fee/
recomputation reason. These indicate an implementation defect, never treated
as "this catalyst has no data."

**Legitimate exclusion — drop this catalyst from the confirmatory pair set,
log the reason, continue the build:** a traded arm has no quote (or
insufficient quote size) satisfying the lookup-window/staleness/session/
validity rules on an otherwise-capable, approved provider — a genuinely
unobservable market outcome, not a code defect.

**Not a policy observation, excluded from `D_c` without being counted as an
exclusion either:** `NO_ELIGIBLE_CANDIDATES` catalysts (Section 4), reported
under their own separate count.

An abstaining arm (Section 6) contributes `R_arm,c = 0` unconditionally,
entering none of these three paths — no outcome validation is ever performed
for it.

**Report contents** (documentation requirement, not a new statistical rule):
the confirmatory report must retain and report at minimum: total
candidate-bearing catalysts; `catalysts_with_zero_eligible_candidates`;
confirmatory-included catalysts; quote-unobservable exclusions; and
exclusions broken out by which arm's selected security caused them
(attributable to A's selection vs. G's selection) — so that if quote-
unobservable exclusions later concentrate on one arm's typically-
thinner-liquidity picks, that non-random missingness is visible and
reviewable rather than silently absorbed into an aggregate count. No new
exclusion-rate threshold is being introduced now.

## 8. Global confirmatory-configuration preflight

An abstaining arm never calls `assert_outcome_ready_for_confirmation` (no
outcome row exists) — so a run whose early catalysts happen to be mutual
abstentions could assemble a technically-valid-looking confirmatory dataset
without ever exercising the deferred-parameter checks. This function proves
the EXPERIMENT is configured, as distinct from `assert_outcome_ready_for_confirmation`
proving one REALIZED outcome obeys that configuration:

```python
def assert_confirmatory_configuration_complete():
    """Called once, before building any confirmatory dataset. An unset
    global configuration aborts the entire confirmatory build outright -- it
    is not a per-catalyst exclusion, since the methodology being incomplete
    is true regardless of which catalysts happen to need market data."""
```

Requires:

- `MAX_QUOTE_LOOKUP_DELAY_SECONDS` frozen.
- `MAX_QUOTE_STALENESS_SECONDS` frozen (separately from the above).
- `CONFIRMATORY_DATA_PROVIDER` frozen to a single value, AND that provider's
  adapter has established trustworthy, normalized-to-shares size semantics
  (Section 7e) — pinned to the provider configuration/version, not
  independently mutable.
- `fee_method_version` and its underlying fee-computation implementation
  exist (one methodology bundle — a version identifier and the
  implementation it identifies are not counted as two separate items).
- The protective-stop rule, version, and durable trigger-provenance
  mechanism exist (Section 7b).
- `return_method_version == 'shadow_nbbo_log_v1'`.
- `REFERENCE_EQUITY_USD` is frozen, finite, and `> 0`; `NOTIONAL_FRACTION ==
  0.05` exactly; `SHADOW_NOTIONAL_USD > 0`; if bound to Section 14's
  account-size assumption rather than actual initial funding, also
  `1000 <= REFERENCE_EQUITY_USD <= 3000`; either way its provenance is
  recorded explicitly.
- `H`, `delta`, `n_bootstrap`, and `CONFIRMATORY_BOOTSTRAP_SEED` match the
  preregistration record in Section 2.

```
assert_confirmatory_configuration_complete()
        |
build catalyst observations
        |
per-trade assert_outcome_ready_for_confirmation()
```

## 9. Deferred parameters/requirements — six total, all fail-closed

1. `MAX_QUOTE_LOOKUP_DELAY_SECONDS` — how long after the target execution
   time a snapshot's `quote_timestamp` may fall and still count.
2. `MAX_QUOTE_STALENESS_SECONDS` — separate from #1; checked against the
   existing `quote_snapshots.staleness_seconds` column.
3. `CONFIRMATORY_DATA_PROVIDER` — a single frozen value (not an allowlist,
   not an ordered fallback list), including its size-unit normalization
   capability and convention, pinned to the provider configuration/version.
4. `fee_method_version` and its fee-computation model.
5. The deterministic protective-stop trigger/fill rule and its durable
   provenance storage — gates ALL traded outcomes, not only
   `protective_stop`-labeled ones, until implemented.
6. `REFERENCE_EQUITY_USD`.

`assert_outcome_ready_for_confirmation` and
`assert_confirmatory_configuration_complete` must explicitly refuse
confirmatory validity while any of these six is unset/unimplemented —
"not configured" is its own distinct refusal, never a convenient default
chosen by whoever implements this.

## 10. Required tests

- **Catalyst-wide invariant**: fixture with ONE catalyst and TWO
  canonical_events (mirroring the real Dry Run 002 shape), each with its own
  event_version/candidate set — two candidates selected under different
  canonical_events of the same catalyst, both `selected=true` for the same
  model, must fail loudly. Plus: rank-1/score>0 positive case; rank-1/score<=0
  abstain positive case; two different candidates both `selected=true`;
  two different candidates both `abstained=true`; a `rank>1` candidate with
  `selected=true`; a `rank=1` candidate whose selected/abstained contradicts
  its score sign; a gapped/duplicated rank sequence; `score = NaN`/`Infinity`
  at what would otherwise be rank 1 rejected before ranking; a zero-eligible-
  candidate catalyst produces `NO_ELIGIBLE_CANDIDATES`, not a raised error
  and not a fabricated row, and is excluded from both the confirmatory set
  and the abstention-rate denominator.
- **Tie-break** (`stable_entity_sort_key`): `None` cik → name fallback;
  `"1045810"` → `CIK:0001045810`; `" 1045810 "` → same (whitespace
  stripped); `""`, `"   "`, `"abc"`, `"0"`, and an 11+-digit string → all
  raise `InvalidStableEntityKeyError`; equal scores with two different
  non-null CIKs → deterministic ordering stable across simulated-rebuild
  insertion-order changes; equal scores with two CIK-null entities with
  different normalized legal names → deterministic ordering; equal scores
  with an identical `stable_entity_sort_key` (e.g. the same entity appearing
  via two event_versions of one catalyst with the same CIK and same score)
  → raises `UnresolvedRankingTieError`.
- **Abstention semantics**: all four trade/abstain states (both trade; A
  trades/G abstains; A abstains/G trades; both abstain, `D_c=0` and still
  counted as a real observation) plus the zero-eligible-candidates exclusion
  as a fifth, distinct case.
- **Outcome validation**: recomputed price/return mismatch is a hard
  failure even when the referenced quote snapshot IDs are individually
  valid; insufficient quote size for `q` on an approved provider is a
  legitimate per-catalyst exclusion; a provider incapable of establishing
  normalized size fails the global preflight instead; fee values that don't
  match the frozen fee methodology fail even when the resulting return
  recomputes internally consistently; `horizon` and `protective_stop`
  outcomes both fail confirmatory validation while stop provenance is
  unimplemented.

## 11. Restated non-goals

No real broker/Robinhood execution. No `actual_fill` price-source or fill
storage. No opening-auction infrastructure or `trading_session` schema
change. No change to arm structure, estimator, or bitemporal model. No
adjustment of any frozen Section 8 value based on anything discovered while
implementing this.
