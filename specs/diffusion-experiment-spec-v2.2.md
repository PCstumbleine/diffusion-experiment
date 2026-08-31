# The Diffusion Experiment — v2.2

*Fourth-round revision. Closes eight specification-contract gaps and four implementation-completeness gaps found in a fourth adversarial review of v2.1, plus one deliberate scope simplification made for practical rather than technical reasons (Section 6). Plain-text export for cross-review.*

**Revised:** 30 August 2026 · **Supersedes:** v2.1 · **Scope:** Phase I-A — corporate information diffusion only; macro/Fed deferred to Phase I-B (unchanged)

**The mandate, unchanged:** does *new event → economic linkage → measurable underreaction → predictable residual return* exist strongly enough to survive trading costs — and does an LLM add anything a much simpler mechanical model, given the same candidates, doesn't already capture?

---

## 0. What changed in this revision

A fourth review round found that v2.1 was not actually self-contained — two clauses referred to concepts (an undefined `X_i`, an "evidence tier B" system) that v2.1 itself had removed or never defined, meaning two people building from the document alone could produce incompatible systems. It also found a real look-ahead-bias risk (the relationship graph had no notion of "when did we actually know this, versus when did it become true"), a hypothesis section labeled "fully specified" that in fact left every actual number blank, a subtle double-count in how the pass/fail bar was defined, a censoring rule likely to introduce the exact bias it was meant to prevent, a "surprise" formula that produces nonsense near zero, and an undefined transmission "probability" with no real ground truth to calibrate against. All eight are fixed below, along with four further gaps (trading-session handling, per-model decision records, multi-party event roles, and quote snapshots only at decision time rather than at every entry and exit) needed before the schema can actually support what earlier sections describe.

**One deliberate simplification, not a technical fix:** Section 6 no longer requires resolving a paid commercial news vendor's license before Phase I-A can start. That question is deferred indefinitely rather than answered — see Section 6.

This review round also reconfirmed, independently, the SEC EDGAR timestamp claim, the ALFRED/vintage-data claim, and the Robinhood day-trading rule change (which turns out to be a broader SEC rule change, not a Robinhood-specific policy). It tightened the ASC 280 citation to FASB's own material rather than a secondary summary. See Section 13.

## 1. Event & document schema

**Documents and events are many-to-many, not many-to-one.** A single earnings release routinely contains several distinct economic facts at once — an earnings surprise, a guidance revision, a capacity change, a buyback announcement — each of which is its own event. Forcing them into one event record loses information; treating them as separate documents duplicates the source.

```
raw_documents
canonical_events

event_document_links
  event_id, document_id, relationship_type,
  evidence_span_start, evidence_span_end, first_seen_at
```

Evidence spans (which part of the document supports which event) make later human or automated audit of an extraction possible — otherwise there's no way to check whether the system read the document correctly.

**Three separate identifiers, previously conflated into one:**

```
catalyst_id          - the originating disclosure/filing/press release
canonical_event_id   - one economic proposition ("company raised FY26 guidance")
event_version_id     - a successive state of that same proposition
                        (e.g., a same-day correction of the guidance number)
```

Several events can share one `catalyst_id` (one earnings release → guidance event + buyback event + capacity event). Section 8's statistical clustering is keyed to `catalyst_id` specifically, because several trades derived from one disclosure are not independent observations.

```
TIMESTAMP TAXONOMY  (unchanged from v2.1, kept distinct, never collapsed)
  source_published_at, source_observed_at, ingested_at,
  canonical_first_public_at, first_public_timestamp_source,
  first_public_timestamp_precision,
  event_effective_at, decision_at, first_executable_at
```

**Surprise is no longer one universal formula.** `(observed − reference) / |reference|` explodes when the reference value is near zero (guidance moving from $0.01 to $0.05 registers as "+400%," which is meaningless) and mishandles anything that crosses zero. Replaced with a per-event-type transformation registry:

```
surprise_transform_registry:
  positive multiplicative variables (revenue, unit volume)  -> log ratio
  percentage variables (margin, growth rate)                -> percentage-point change
  zero-crossing variables (EPS near breakeven)               -> change / historical robust scale
  positive levels with a small-denominator risk               -> % change with a denominator floor

surprise_raw and surprise_reference are always retained regardless of
which transform applied, so the raw numbers are never lost to the transform.
```

Retrospective processing of historical documents remains restricted, by policy, to fact-extraction only — never to judging in hindsight whether a historical trade would have been attractive (unchanged from v2.1).

## 2. Entity & instrument master

Structure unchanged from v2.1: permanent `entity_id`, ticker as display metadata, full instrument-identifier history, corporate actions. **One claim softened:** this identity layer does not make the universe "survivorship-bias-free by construction" — it *enables* survivorship-bias-free sampling. It still has to be populated with historically delisted, acquired, bankrupt, and renamed securities, and every historical query still has to be run point-in-time against that population, not against today's list of active tickers. That population-and-querying work is an explicit build task, not a side effect of the schema existing.

## 3. Causal-link evidence

The three-axis evidence model (source authority / relationship evidence / shock-transmission evidence) from v2.1 stands. Two additions close real gaps.

**Bitemporal history, previously missing.** A 2026 filing stating "we have supplied Company B since 2022" describes a relationship that existed in 2023 — but a system evaluating a 2023 event must not use that fact unless it was *publicly known* in 2023. Without tracking both when a relationship was true and when the system could have known it, a historical backtest can silently leak future knowledge into the past, which would make the strategy look better than it is for reasons that have nothing to do with the strategy.

```
relationship_valid_from, relationship_valid_to      (economic reality)
evidence_published_at, system_first_knew_at         (knowledge state)
record_superseded_at                                (correction/retraction)
```

Every historical query must filter on knowledge time (`system_first_knew_at ≤ decision_at`), not just valid time.

**A deterministic candidate-eligibility rule, replacing the leftover "evidence tier B or better" reference** (v2.1 had removed the old A/B/C tier system but Section 5 still referred to it — an internal contradiction):

```
candidate_eligible =
    relationship_evidence IN (explicit_named, quantified_named)
    AND evidence_published_at <= decision_at

-- inferred_structured relationships may be added to the eligible set only
-- under an explicitly pre-registered exception, stated in the experiment
-- config, never as a silent default.
```

**Shock transmission is not naturally a yes/no label.** A supplier's reaction to a shock can be +0.2%, +1.5%, −1.0%, or a delayed +4% — there's no obvious binary "transmitted or didn't." Replacing the earlier `p_shock_transmits` binary:

```
p_relationship_exists              (a genuine probability — kept)
expected_transmission_effect       (a magnitude, not a probability)
transmission_effect_interval       (its uncertainty band)
```

`raw_llm_relationship_score` / `raw_llm_transmission_score` continue to be logged always, distinct from any calibrated figure, as in v2.1.

## 4. The underreaction estimator

**`X_i`, referenced in v2.1 but never actually defined there, is now explicit:**

```
X_i =
  event_category, surprise_transformed, relationship_type,
  source_authority, relationship_evidence, expected_transmission_effect,
  historical_linkage_measure, linked_security_liquidity, sector,
  volatility_regime, time_since_disclosure
```

The decision-time-conditioned estimator from v2.1 is unchanged:

```
UR_i(t, H) = E[ CAR_i(0,H) | X_i, I_t ] − CAR_i(0, t)
```

**Intraday return calculation, previously unspecified.** Decision time `t` can be mid-morning, while a naive daily factor model can't be applied to a partial day without saying how. Two layers, now explicit:

```
Intraday event-time abnormal return:
  AR_i(t0, t) = r_i − β_M·r_M − β_S·r_S
  betas estimated from a clean prior historical window;
  r_i, r_M, r_S measured over the identical event-time interval (t0, t)

Subsequent daily abnormal returns: same factor model, daily frequency,
  computed consistently with the intraday layer rather than independently of it

CAR uses log abnormal returns throughout, so intraday and daily
  contributions aggregate additively without a separate reconciliation step
```

**Trading-session handling, previously absent entirely.** Many Phase I-A events (earnings, guidance) happen outside regular market hours. Policy for this experiment: information is timestamped as observed the moment it's published, but *hypothetical execution* begins only at the first eligible regular trading session — extended-hours execution is not assumed by default. If extended-hours execution is later enabled for a specific test, it must use a separately modeled extended-hours bid/ask spread, never regular-session liquidity assumptions.

The historical comparable-event dataset requirements from v2.1 (defined taxonomy, point-in-time availability, delisted/acquired entities included, relationship-reconstruction tied to Section 3's axes) are unchanged. ALFRED vintage data remains the plan for any Phase I-B macro series, to avoid using today's revised figures where the original as-published figures are what mattered at the time (independently reconfirmed this round — see Section 13).

## 5. Experimental arms & diagnostics

The seven-arm structure and the shared-candidate-universe fix (Arm A and Arm G drawing from the identical eligible set, defined deterministically in Section 3) are unchanged from v2.1.

**Arm G is renamed.** Calling it "no LLM" overclaims: the shared candidate graph it ranks from may itself have been extracted or validated using an LLM upstream. Arm G is now labeled **"mechanical ranker on the shared candidate graph"** — the correct comparison for testing whether the LLM's *ranking/interpretation* adds value, holding candidate generation fixed. A separate, fully LLM-free end-to-end pipeline (no LLM anywhere, including candidate discovery) remains a distinct, later question if it's ever wanted.

**Multi-party events need explicit roles.** A supply agreement involves both a supplier and a customer, and both may be public companies — Arm B ("buy the obvious headline company") is ambiguous without knowing which one is "obvious" for a given event type.

```
event_entities
  event_id, entity_id, role
  roles: issuer | subject | supplier | customer | buyer | target | partner | counterparty
```

Arm B's target is now defined per event category using these roles, rather than assumed to be a single unambiguous company.

**Per-model decision records, not just one candidate record.** Arm A and Arm G can rank and select differently from the same candidate pool; v2.1's single `selected_boolean` field on `candidate_signals` couldn't represent that.

```
candidate_signals            (unchanged: the pool itself, eligibility, rejection_reason)

model_candidate_decisions
  candidate_id, model_id, model_version,
  score, rank, selected, abstained, decision_reason, decision_at
```

Diagnostic H (same-security/random-time placebo) and Arm F's two decay curves (event-clock and equal-holding-period) are unchanged from v2.1, now inheriting the trading-session policy above for what "immediate" and "delayed" entry actually mean in wall-clock terms.

## 6. Phase I-A data sources — simplified

v2.1 assumed a paid, API-native commercial news product would serve as the trigger layer, gated on confirming its license actually covers automated ingestion, storage, LLM processing, and algorithmic-trading use. That confirmation is a real legal/contractual step — reading a vendor's terms carefully, or getting specific use cases confirmed in writing with their sales or legal team.

**For this project, that step is deferred indefinitely rather than performed**, as a deliberate scope decision: this is a personal, non-commercial research experiment, not a funded or institutional effort, and negotiating commercial data licenses isn't a reasonable ask for that scope.

**Phase I-A therefore runs on free, publicly documented sources only:**

- **SEC EDGAR** — explicitly built for automated public access; its terms are self-serve and don't require any negotiation (identify your requests with a descriptive User-Agent string and stay under the published rate limit — that's the entire requirement).
- **Company-originated material** published directly by companies (press releases on their own investor-relations pages).
- An independent market-data provider, chosen from providers whose free or low-cost tier's terms are written in plain, self-service language covering personal/research use — read directly, not negotiated.

This is a real scope narrowing, not a cosmetic one: it removes real-time general news coverage (an announcement covered only by a wire service or a paid outlet, with no SEC filing and no company press release, won't be seen). Corporate 8-Ks, earnings releases, and guidance revisions — the Phase I-A event categories this design already focuses on — are filed with the SEC or issued directly by the company in the large majority of cases, so this narrowing is expected to cost limited coverage for the categories in scope. It should be revisited only if Phase I-A data turns out to be too sparse to reach the sample sizes in Section 9 — at which point the fix is to look harder for another free, self-service source, not to default back into needing a negotiated commercial license.

## 7. Database & schema

```
raw_documents, canonical_events, event_document_links     Many-to-many document/event mapping, evidence spans
entities, instruments, instrument_identifiers,
  corporate_actions                                        Permanent identity layer
entity_relationships                                       Three-axis evidence + bitemporal validity/knowledge fields
event_entities                                              Per-event entity roles
extracted_events                                            Event category, surprise (raw + transformed), catalyst/event/version IDs
candidate_signals                                            The eligible/rejected pool (event-level, model-independent)
model_candidate_decisions                                    Per-model score/rank/selected/abstained
underreaction_estimates                                      X_i, I_t, E[CAR|X,I], observed CAR, uncertainty interval, model version
experiments -> experiment_arms -> arm_entries -> arm_outcomes  Normalized for Arm F's multiple entries
quote_snapshots                                              Attachable to arm_entry_id AND arm_exit_id, not only decision_at;
                                                              includes staleness and trading-session metadata
market_data                                                  Point-in-time OHLCV/factor exposures for needed windows only
```

Postgres for all of the above (low-volume, high-value-per-row); bulk historical tick/minute data stays in Parquet/object storage, as in v2.1.

## 8. Statistical framework

**The primary hypothesis is now actually specified, not merely described as such.** v2.1 said a horizon "e.g., 5-day abnormal return, chosen in advance" — "e.g." meant it hadn't in fact been chosen. Before the confirmatory cohort is collected, the following object is frozen and pre-registered as literal fixed values, not examples:

```
primary_horizon        = 5 trading days
confidence_level        = 95%
alternative              = one-sided
inference_method         = [pick exactly one: time-aware block bootstrap
                             OR multiway-clustered SE — not both, decided
                             before data collection]
delta                    = [numeric value — see below]

A_position_rule, G_position_rule    (top-1 vs. top-k, sizing, tie-breaking)
notional_per_event, max_positions_per_catalyst
abstention_rule          (what happens when a model abstains)
direction_rule           (long/short determination)
```

Anything not in this frozen object is secondary/exploratory by default.

**δ, corrected to avoid double-counting costs.** Returns are already measured net of each arm's own trading costs (`R_A_net = R_A − cost_A`, similarly for G), so `A − G` already reflects their cost differential. Re-deriving δ from the same transaction-cost model would count costs twice. δ is instead defined as *the minimum economically meaningful incremental advantage of A over G, after both are already net of costs* — a number set by power/economic analysis (e.g., "an edge below 15 bps isn't worth the operational complexity of running this"), not by re-adding the cost figure already subtracted. The test itself is unchanged:

```
H0: mu(A-G) <= delta        H1: mu(A-G) > delta
Promotion requires the CI's lower bound to exceed delta, not merely exclude zero.
```

**Competing-event censoring is reversed.** v2.1 excluded an observation from the primary analysis whenever the linked entity had its own material news within the outcome window. But whether such news occurs afterward can itself depend on the original event, the return path, or the company — conditioning inclusion on something that happens *after* the signal risks informative censoring, which can bias the result in either direction while looking like careful hygiene. Corrected:

```
Primary analysis:   fixed-horizon, intention-to-observe return.
                    Record competing_event_flag, competing_event_at,
                    competing_event_type — but never drop the observation
                    on their basis.

Sensitivity analysis (secondary): censor/exclude at unscheduled
                    competing news, as a robustness check only.

Pre-specified exclusion (allowed in the primary set): events with a
                    SCHEDULED competing event known before signal creation
                    (e.g., earnings or FOMC date already on the calendar)
                    may be excluded as an eligibility rule set in advance —
                    this is not post-treatment selection, since it doesn't
                    depend on what happened after the signal.
```

Dependence clustering (now at `catalyst_id`, matching Section 1's identifiers), frozen evaluation epochs, and confirmatory-vs-exploratory separation are otherwise unchanged from v2.1.

## 9. Promotion gates

Unchanged from v2.1 (pipeline-quality metrics on a held-out sample; the four-stage transition table from build through capital scale-up), with one addition: before Build → running Phase I-A, a specific test suite must pass — including at least one test that constructs a scenario where a relationship's `valid_from` predates its `system_first_knew_at` and confirms the query layer correctly excludes it from any decision made before that knowledge date. This is the concrete, checkable version of the bitemporal fix in Section 3 — a schema field that isn't queried correctly provides no actual protection.

## 10. Tech stack

Unchanged from v2.1: Python, an LLM API with versioned prompts and structured output, `pgvector` for novelty dedup, Postgres for the experiment database, Parquet/object storage for bulk market series, restricted database roles (insert-only pipeline credential, separate admin credential, immutable snapshots), a scheduled ingestion worker, a lightweight weekly-review dashboard.

## 11. Guardrails, correctly scoped

Unchanged from v2.1: append-only behavior is enforced by database roles, not merely intended; the human confirmation role in Phase II is a fixed-checklist safety veto only, never investment discretion, with every veto logged against which checklist condition triggered it.

## 12. Open items before Phase II

- Confirm which Robinhood account type the Agentic account will be — cash vs. margin. Cash keeps things simplest under T+1 settlement and avoids leverage entirely by construction. Margin accounts are no longer subject to the old $25k pattern-day-trading threshold following the SEC's June 2026 rule change (confirmed this round as a broader SEC-level change, not just a Robinhood policy) — but a margin account makes leverage *possible* unless separately disabled; it does not require using it. This is a real decision to make deliberately, not a default to fall into.
- Re-verify the LLM API provider's usage terms and Robinhood's Trading MCP capabilities close to the actual Phase II build date — both have changed materially within the life of this review already.
- Wash-sale exposure from rapid re-entry, and, before any real trades occur, a plain-language look at how short-holding-period activity is taxed (not a professional consultation requirement for Phase I-A, which involves no real money and no real gains or losses at all).
- The data-source simplification in Section 6 is explicitly a Phase I-A decision. If the project ever moves toward real capital at meaningful scale, the commercial-news-licensing question deferred here would need to be revisited — at that point, with real money and real scale involved, it becomes reasonable to get professional (legal) input rather than resolve it alone. Not before then.

## 13. Provenance

Fourth document in the series: original design → adversarial audit → v2.0 (merging two review rounds) → v2.1 (a third round, twenty-four gaps) → this v2.2 (a fourth round, eight specification-contract gaps plus four implementation-completeness gaps). This round re-verified, independently, the SEC EDGAR timestamp claim (unchanged, confirmed) and the ALFRED vintage-data claim (unchanged, confirmed); tightened the ASC 280 citation to FASB's own material rather than a secondary summary (the underlying claim was already correct); and confirmed the Robinhood day-trading rule change is in fact a broader SEC rule elimination effective June 2026, not a Robinhood-specific policy — a stronger form of the same fact than previously stated. The bitemporal-history gap, the document/event cardinality error, and the censoring-direction reversal are the three fixes in this round most likely to have mattered for correctness rather than clarity; all three are the kind of error that produces a plausible-looking but wrong result rather than an obvious failure.

---

*This closes the planned prose-review cycle. The remaining open questions are numeric pre-registration choices (Section 8) and a licensing decision explicitly deferred (Section 6) — neither is well served by another round of written critique. The next adversarial step should be code: a test that tries to make the bitemporal query leak future knowledge and confirms it can't, a small simulation checking the δ/censoring logic doesn't produce a biased estimate under a known synthetic answer, and a schema-integrity check that `event_document_links` and `model_candidate_decisions` actually support the seven-arm comparison as described.*
