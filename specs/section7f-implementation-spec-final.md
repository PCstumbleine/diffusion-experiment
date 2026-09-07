# Section 7f Implementation Spec — FINAL

This document is self-contained and authoritative. It supersedes everything
discussed during the four review rounds; if anything elsewhere conflicts with
this document, this document controls. It implements the confirmatory
statistical builder that `specs/section8-implementation-spec-final.md`
Section 7f described but did not build, plus the fee methodology Section 9
left as `FEE_METHOD_VERSION = None` / `compute_expected_fees = None`, plus
the two new deferred parameters and the trading-calendar utility this pass
discovered it needs. It does NOT touch the arm structure, the underreaction
estimator, the bitemporal model, Section 14's operating plan, or anything
already implemented in `build/confirmatory_analysis.py` (commit `c9798fd`) —
all of that is settled and out of scope here.

## 0. Scope and non-goals

In scope: an explicit catalyst/experiment-epoch membership table; a shared
NYSE trading-calendar utility; the `PENDING_NOT_MATURED` classification and
its maturity boundary; an explicit exception-to-classification mapping; the
Robinhood customer fee methodology (`compute_expected_fees`); the statistical
builder (`build_confirmatory_dataset`) and the separately-gated promotion
test (`run_confirmatory_promotion_test`); two new deferred, fail-closed
parameters (`OUTCOME_PROCESSING_GRACE`, `CONFIRMATORY_ANALYSIS_TRIGGER`).

Explicitly out of scope, not to be added speculatively during this
implementation pass:

- No `arm_entries`/`arm_outcomes` row creation, no quote fetching, no stop-
  trigger simulation, no order placement. This pass only *consumes* an
  existing `entry_timestamp`/outcome row if one exists; it never creates one.
- No change to anything already implemented in `confirmatory_analysis.py`'s
  Section 8 functions (`assert_trade_abstention_invariant`,
  `assert_decision_set_ready_for_comparison`,
  `assert_outcome_ready_for_confirmation`,
  `assert_confirmatory_configuration_complete`) beyond wiring in the new
  `compute_expected_fees` implementation and `FEE_METHOD_VERSION` value this
  spec defines.
- No change to the arm structure, the estimator, or the bitemporal model.
- No adjustment of any frozen Section 8 value based on anything discovered
  while implementing this.
- The trading-calendar utility (Section 2 below) is reusable infrastructure
  for `outcome_due_at` in this pass; it is NOT wired into whatever eventually
  sets `arm_entries.entry_timestamp`/`arm_outcomes.exit_timestamp` — that
  execution pipeline does not exist yet and remains out of scope.

## 1. New schema: `experiment_catalysts` — explicit epoch membership

Nothing below `experiments.scoring_epoch` currently says which catalysts
belong to a given experiment/epoch. `candidate_signals` and
`model_candidate_decisions` carry no `experiment_id` at all, and inferring
membership from timestamps is fragile (backfilled filings, late ingestion,
corrected timestamps can all change which catalysts appear to fall inside a
date range). Membership must be its own explicit, auditable fact, decided
once, before either arm's decisions — never duplicated per model.

```sql
CREATE TABLE experiment_catalysts (
    experiment_id  UUID NOT NULL REFERENCES experiments(experiment_id),
    scoring_epoch  TEXT NOT NULL,
    catalyst_id    UUID NOT NULL REFERENCES catalysts(catalyst_id),
    admitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id, catalyst_id)
);

CREATE INDEX idx_experiment_catalysts_epoch ON experiment_catalysts (experiment_id, scoring_epoch);

-- scoring_epoch is redundant with experiments.scoring_epoch by design (for a
-- directly-queryable, self-contained audit row) -- enforce it can never
-- silently drift from the experiment's own value.
CREATE OR REPLACE FUNCTION check_experiment_catalyst_epoch_consistency() RETURNS TRIGGER AS $$
DECLARE
    v_experiment_epoch TEXT;
BEGIN
    SELECT scoring_epoch INTO v_experiment_epoch FROM experiments WHERE experiment_id = NEW.experiment_id;
    IF v_experiment_epoch IS DISTINCT FROM NEW.scoring_epoch THEN
        RAISE EXCEPTION 'experiment_catalysts.scoring_epoch % does not match experiments.scoring_epoch % for experiment_id %',
            NEW.scoring_epoch, v_experiment_epoch, NEW.experiment_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_experiment_catalyst_epoch_consistency
    BEFORE INSERT OR UPDATE ON experiment_catalysts
    FOR EACH ROW EXECUTE FUNCTION check_experiment_catalyst_epoch_consistency();

-- Append-only: admission is a one-time, auditable fact. If a catalyst was
-- wrongly admitted, that is itself data (a later row/process records it),
-- never a reason to edit or remove the original admission.
CREATE OR REPLACE FUNCTION forbid_experiment_catalyst_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'experiment_catalysts is append-only -- UPDATE/DELETE are never permitted (attempted on experiment_id=%, catalyst_id=%)',
        COALESCE(OLD.experiment_id, NEW.experiment_id), COALESCE(OLD.catalyst_id, NEW.catalyst_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_forbid_experiment_catalyst_mutation
    BEFORE UPDATE OR DELETE ON experiment_catalysts
    FOR EACH ROW EXECUTE FUNCTION forbid_experiment_catalyst_mutation();
```

```python
def get_confirmatory_catalyst_universe(conn, experiment_id: str, scoring_epoch: str) -> list[str]:
    """Every catalyst_id admitted to this experiment's scoring epoch, per
    experiment_catalysts. Raises ValueError if scoring_epoch doesn't match
    the experiment's own recorded epoch (defensive -- the DB trigger already
    prevents this from being written, but a caller could still pass a wrong
    scoring_epoch string that matches zero rows silently; this call makes
    that loud instead)."""
```

## 2. Shared NYSE trading-calendar utility

Needed for `outcome_due_at` (Section 4 below) and, later, whatever sets
`arm_entries.entry_timestamp`/`arm_outcomes.exit_timestamp` per Section 7b's
"09:30 ET on the next regular trading session" rule — built once here as
reusable infrastructure, not re-derived per caller.

Dependency: `pandas_market_calendars`, calendar `'NYSE'`. Verified directly
(not from documentation alone) against the pinned version: correctly
excludes weekends and NYSE holidays (confirmed against the observed July 4th
closure, Thanksgiving), correctly models special/shortened sessions (the
day-after-Thanksgiving early close), and returns `market_open`/`market_close`
as timezone-aware UTC timestamps.

```python
MARKET_CALENDAR = "NYSE"
MARKET_CALENDAR_LIBRARY = "pandas_market_calendars"
MARKET_CALENDAR_LIBRARY_VERSION = "5.4.0"  # pin exactly; bump requires the
    # calendar regression tests (Section 10) to pass again -- an unpinned
    # dependency update must never silently change historical session
    # resolution mid-collection.

def next_regular_session_open_strictly_after(after: datetime) -> datetime:
    """Return the NYSE calendar's scheduled market_open strictly later than
    `after` -- i.e. outcome_due_at = min{opens : open > entry_timestamp}.
    `strictly` matters: if `after` is itself exactly equal to a session's
    market_open, the result is the FOLLOWING session's open, not that same
    instant.

    `after` must be timezone-aware; raises ValueError if naive. Returns a
    timezone-aware UTC datetime -- the actual scheduled market_open from the
    calendar, never a hand-computed `session_date + time(9, 30)`, so this
    never becomes a second, independently-drifting definition of "session
    open" from whatever the calendar library actually resolves (DST
    transitions, one-off historical closures, etc. are the library's
    responsibility, not reimplemented here).
    """
```

## 3. Fee methodology (`FEE_METHOD_VERSION`, `compute_expected_fees`)

Sourced from Robinhood's current customer-facing fee documentation
(US entity — Robinhood Financial LLC / Robinhood Securities LLC, the entities
this project's account uses — not the UK entity, which is a different legal
entity under a different regulator), cross-checked against SEC and FINRA's
own statutory rate publications, and against the CAT NMS Plan's own fee-alert
history directly. All verified against primary sources, not taken from any
single summary.

**Provenance note** (frozen into the spec, not just a footnote): CAT customer
charges experienced a real US billing pause after the December 2025 invoice
(CAT NMS Plan fee alerts: last invoice under the prior regime billed
December 2025 for November activity, "no further monthly invoices until
further notice"). A new 2026 funding model resumed billing at $0.000001/share
(CAT Fee 2026-1) plus $0.000002/share (Historical CAT Assessment 1A) = the
same $0.000003/share Robinhood's current schedule shows for listed equities.
CAT is charged on both buys and sells (Robinhood's schedule: "applied to all
equity and options orders"), unlike the SEC fee and TAF, which are sells-only.

```
CAT_fee(q)          -- $0.000003 per share, both entry and exit
SEC_fee(principal)  -- $20.60 per $1,000,000 of principal, exit only,
                        waived when principal <= $500
TAF_fee(q)           -- $0.000195 per share, exit only, capped at $9.79,
                        waived when q <= 50 shares

entry_fee = CAT_fee(q)
exit_fee  = SEC_fee(q * exit_bid) + TAF_fee(q) + CAT_fee(q)
```

The $500/50-share Robinhood customer-pass-through exemptions apply to the
fractional shadow quantity exactly as they would to a real order of that
size — this is a customer-fee-equivalent measurement, and the exemption
thresholds are part of what a Robinhood customer would actually be charged
for the equivalent trade, not a real-execution concept being smuggled in.
Given typical shadow notional ($1,000-3,000 reference equity × 5% =
$50-150), `principal <= $500` and `q <= 50` will both usually hold — meaning
fees will typically compute to $0.00 — but a low-priced security can still
push `q` above 50 shares, so the exemption is checked, never assumed.

**Rounding — each component independently, in order, then summed. Decimal
arithmetic throughout, never binary floating point:**

```python
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_DOWN

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

FEE_METHOD_VERSION = "robinhood_us_customer_fees_<freeze-date>_v1"  # e.g.
    # robinhood_us_customer_fees_2026q3_v1 -- bind this version string to
    # captured source artifacts (a saved snapshot or hash of the Robinhood US
    # support page and RHF Fee Schedule PDF, the SEC fee-rate advisory, the
    # FINRA TAF schedule, and the CAT NMS Plan fee-alert page, at the actual
    # freeze date), not to live URLs -- we have directly observed that a live
    # page's stated CAT status can differ from another Robinhood page's, so a
    # version string must be reproducible from what was actually captured,
    # not from whatever a page says if re-fetched later.

def compute_expected_fees(outcome_id, shadow_notional: float, entry_ask: float, exit_bid: float) -> tuple[float, float]:
    """Matches the (outcome_id, shadow_notional, entry_ask, exit_bid) keyword
    contract assert_outcome_ready_for_confirmation already calls with
    (confirmatory_analysis.py line ~567). Recomputes q internally."""
    q = Decimal(str(shadow_notional)) / Decimal(str(entry_ask))
    entry_fee = cat_fee(q)
    exit_fee = sec_fee(q * Decimal(str(exit_bid))) + taf_fee(q) + cat_fee(q)
    return float(entry_fee), float(exit_fee)
```

One synthetic execution per shadow leg — freeze this explicitly: each shadow
entry and each shadow exit is treated as one single execution for fee
purposes. The shadow-NBBO model has no partial-fill engine and none is being
built for fee calculation; TAF's per-trade cap is applied once per leg, not
subdivided.

## 4. `PENDING_NOT_MATURED` — a fourth, time-bounded, non-exception state

Section 8's three-way classification (hard failure / legitimate exclusion /
not-a-policy-observation) doesn't distinguish "this return cannot exist yet"
from "this return should exist but doesn't." Since no execution pipeline
exists anywhere in the repo yet, every currently-`TRADED` decision has zero
`arm_entries`/`arm_outcomes` rows — without this state, the builder could
never run a real confirmatory build until execution code exists, or would
have to treat every such case as a silent hard failure.

```python
OUTCOME_PROCESSING_GRACE = None  # deferred #7, fail-closed like Section 9's
    # six -- a timedelta, frozen before confirmatory collection begins,
    # never a caller-supplied per-run value.

def outcome_due_at(entry_timestamp: datetime) -> datetime:
    """outcome_due_at = next_regular_session_open_strictly_after(entry_timestamp)
    -- the exact Section 7b exit-timing rule, computed via the shared
    calendar utility (Section 2), never a hand-rolled approximation."""

def classify_traded_catalyst_maturity(entry_timestamp, analysis_as_of) -> str:
    """
    analysis_as_of <= outcome_due_at(entry_timestamp) + OUTCOME_PROCESSING_GRACE
        and no required outcome row exists yet  -> PENDING_NOT_MATURED
    analysis_as_of >  outcome_due_at(entry_timestamp) + OUTCOME_PROCESSING_GRACE
        and no required outcome row exists yet  -> hard failure (raise)
    an outcome row exists                       -> proceed to
        assert_outcome_ready_for_confirmation as today
    Both entry_timestamp and analysis_as_of must be timezone-aware; raise on
    a naive input rather than guessing a timezone.
    """
```

`PENDING_NOT_MATURED` is returned as a status, like `NO_ELIGIBLE_CANDIDATES`
already is — it is expected lifecycle state, never raised as an exception.
It contributes no `D_c`, is not counted as a legitimate exclusion, and is not
a hard failure; it is reported under its own separate count so a run that's
mostly-pending is visibly different from a run with real exclusions.

## 5. Exception → classification mapping (frozen, explicit)

| Condition / exception | Classification |
|---|---|
| `ConfirmatoryConfigurationIncompleteError` | Hard failure — abort entire build |
| `TradeAbstentionInvariantError` | Hard failure — abort entire build |
| `UnresolvedRankingTieError` | Hard failure — abort entire build |
| `InvalidCandidateScoreError` | Hard failure — abort entire build |
| `MissingDecisionError` | Hard failure — abort entire build |
| `OutcomeIntegrityError` | Hard failure — abort entire build |
| Required outcome absent, past maturity + grace | Hard failure — abort entire build |
| `QuoteUnobservableError` (methodology otherwise valid) | Legitimate exclusion — drop catalyst, continue |
| `NO_ELIGIBLE_CANDIDATES` | Not a policy observation — separate count |
| Both arms genuinely abstain | Valid observation, `D_c = 0` |
| Required outcome not yet due (within grace) | `PENDING_NOT_MATURED` — separate count, not an exception |

A hard failure aborts the *entire* confirmatory build immediately — no
partial report, no partial `D_c` set. These indicate an implementation
defect, never "this catalyst has no data."

## 6. The statistical builder

```python
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

def build_confirmatory_dataset(
    conn,
    experiment_id: str,
    scoring_epoch: str,
    analysis_as_of: datetime,
    model_specs: dict[str, tuple[str, str]],  # {'A': (model_id, model_version), 'G': (...)}
) -> ConfirmatoryBuildReport:
    """
    1. assert_confirmatory_configuration_complete() -- once, before anything else.
    2. catalyst_universe = get_confirmatory_catalyst_universe(conn, experiment_id, scoring_epoch)
    3. For each catalyst_id: assert_decision_set_ready_for_comparison(...),
       then per Sections 4/5/6 above, classify and accumulate.
    4. Raises immediately on any hard failure (Section 5) -- no partial report.
    5. Otherwise returns the full ConfirmatoryBuildReport.
    Freely re-runnable for QA/monitoring -- does NOT itself run the
    promotion test.
    """
```

## 7. Promotion test — separated, single preregistered look

`catalyst_clustered_test`'s one-sided 95% bootstrap CI has a nominal
false-positive rate for *one* confirmatory look. If the builder is run
repeatedly as catalysts accumulate (week 20, week 21, week 22, ...) and
promotion is granted the first time the lower bound exceeds `delta`, the
overall Type-I error is no longer 5% — this is the classic repeated-looks/
optional-stopping problem. `build_confirmatory_dataset` may run as often as
needed for descriptive monitoring; the promotion decision may not.

```python
CONFIRMATORY_ANALYSIS_TRIGGER = None  # deferred #8, fail-closed -- a
    # preregistered calendar date, a preregistered included-catalyst count,
    # or another already-settled criterion. Left unset here; whichever form
    # it eventually takes, it must be frozen BEFORE it is satisfied, never
    # chosen retroactively once a report looks favorable.

class ConfirmatoryAnalysisNotAuthorizedError(Exception):
    """CONFIRMATORY_ANALYSIS_TRIGGER is unset, or this report's
    analysis_as_of/included-catalyst count does not yet satisfy it."""

def run_confirmatory_promotion_test(report: ConfirmatoryBuildReport) -> TestResult:
    """Raises ConfirmatoryAnalysisNotAuthorizedError unless
    CONFIRMATORY_ANALYSIS_TRIGGER is frozen and satisfied by `report`.
    Otherwise calls catalyst_clustered_test(report.records, delta=DELTA,
    n_bootstrap=CONFIRMATORY_N_BOOTSTRAP, rng=np.random.default_rng(CONFIRMATORY_BOOTSTRAP_SEED)).
    This is the ONLY function that may make the promotion decision -- never
    call catalyst_clustered_test directly on a build report elsewhere."""
```

## 8. Report contents (unchanged requirement, now fully satisfiable)

`ConfirmatoryBuildReport` (Section 6) already carries every field Section
8's Section 7f required: total candidate-bearing catalysts,
`catalysts_with_zero_eligible_candidates`, confirmatory-included catalysts,
quote-unobservable exclusions, and exclusions broken out by which arm's
selection caused them — plus `pending_count`, new to this pass.

## 9. Deferred parameters — consolidated, all fail-closed

The original six (Section 8/9, already implemented) plus two new ones from
this pass:

1. `MAX_QUOTE_LOOKUP_DELAY_SECONDS` — unset
2. `MAX_QUOTE_STALENESS_SECONDS` — unset
3. `CONFIRMATORY_DATA_PROVIDER` (+ size capability) — unset
4. `FEE_METHOD_VERSION` / `compute_expected_fees` — **now defined by this
   spec** (Section 3) — no longer deferred once this pass lands, but the
   version string's freeze-date suffix and source-snapshot binding still
   need an actual freeze date filled in before confirmatory collection.
5. Protective-stop trigger rule + provenance — unset
6. `REFERENCE_EQUITY_USD` (+ provenance) — unset
7. `OUTCOME_PROCESSING_GRACE` — unset (new, Section 4)
8. `CONFIRMATORY_ANALYSIS_TRIGGER` — unset (new, Section 7)

`MARKET_CALENDAR_LIBRARY_VERSION` (Section 2) is pinned, not deferred — it's
an implementation detail, not a preregistration parameter — but it is
recorded and any future bump requires the calendar regression tests to pass
again.

## 10. Tests required

**Calendar utility** — each of these against the pinned library version,
verified empirically, not assumed from documentation:
ordinary Tuesday→Wednesday open; Friday→Monday open; the Friday preceding a
Monday NYSE holiday → Tuesday; Independence Day observed closure; Thanksgiving
(next session is the day after, not skipped further, and note the special
early-close session itself is a valid session, not a closure); Christmas/New
Year's closure; a case crossing a US DST transition, resolving the correct
NYSE local open on both sides; at least one historical one-off NYSE closure
the pinned library represents; input exactly equal to one session's
`market_open` → the *following* session (strictness); input a few seconds
after an open → the following session; a naive (non-timezone-aware) input →
raises.

**`experiment_catalysts`**: happy-path admission; scoring_epoch mismatch
against the experiment's own value → trigger rejects; UPDATE attempt →
rejected; DELETE attempt → rejected.

**Fee methodology**: SEC fee waived at exactly $500 principal and charged
just above it; TAF waived at exactly 50 shares and charged just above it;
CAT charged on both entry and exit at typical shadow quantities (near-zero
but nonzero, rounding to $0.00 as expected); a low-priced-security case
where `q > 50` makes TAF nonzero; SEC's round-up-to-cent behavior at a
boundary that would round differently under round-half-up; each component's
rounding verified independently before summing.

**`PENDING_NOT_MATURED`**: a traded decision with `analysis_as_of` before
`outcome_due_at` (no outcome row) → `PENDING_NOT_MATURED`, not an exception;
the same case exactly at the maturity+grace boundary → still pending;
one second past the boundary with still no outcome row → hard failure; an
outcome row that does exist, regardless of maturity timing → proceeds to the
existing `assert_outcome_ready_for_confirmation` path unchanged.

**Builder-level**: a full-scope run mixing all five per-catalyst outcomes
(included, legitimate exclusion attributable to A, legitimate exclusion
attributable to G, not-a-policy-observation, pending) produces the correct
`ConfirmatoryBuildReport` counts; any hard-failure condition anywhere in
scope aborts the whole build with no partial report; two builds against
different `analysis_as_of` values on the same underlying data produce
consistent, re-runnable results (idempotent given the same inputs).

**Promotion-test gating**: `run_confirmatory_promotion_test` raises
`ConfirmatoryAnalysisNotAuthorizedError` while `CONFIRMATORY_ANALYSIS_TRIGGER`
is unset; raises the same while set but not yet satisfied by a given report;
proceeds to call `catalyst_clustered_test` only once both are true.

## 11. Restated non-goals

No `arm_entries`/`arm_outcomes` row creation, no quote fetching, no stop-
trigger simulation, no order placement, no change to anything already
implemented in `confirmatory_analysis.py`'s Section 8 functions beyond wiring
in `compute_expected_fees`/`FEE_METHOD_VERSION`. No change to arm structure,
estimator, or bitemporal model. No adjustment of any frozen value based on
anything discovered while implementing this.
