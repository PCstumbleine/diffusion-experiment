# The Diffusion Experiment — v2.1

*A buildable Phase I-A blueprint. This revision closes twenty-four gaps a third round of adversarial review found in v2.0's estimator, schema, and experimental design. Plain-text export for cross-review — the published version has a diagram and light styling that don't matter for this purpose.*

**Revised:** 30 August 2026 · **Supersedes:** The Diffusion Experiment v2.0 · **Scope:** Phase I-A — corporate information diffusion only; macro/Fed deferred to Phase I-B

**The mandate, unchanged:** does *new event → economic linkage → measurable underreaction → predictable residual return* exist strongly enough to survive trading costs — and does an LLM add anything a much simpler mechanical model, given the same candidates, doesn't already capture?

---

## 0. Scope, and what changed in this revision

The mandate, the seven-arm core, the two-probability causal framework, and the Postgres/pgvector/Python stack all survive this revision unchanged. A third review round found concrete gaps that could have invalidated results even with flawless code: an estimator that didn't update on same-day price action, an undefined surprise measure, an apples-to-oranges comparison between the AI and its own baseline, no permanent entity identity, and no record of what the system rejected. All are fixed below.

One scope change: Phase I is now explicitly **Phase I-A, corporate information diffusion only** — 8-Ks, guidance revisions, earnings disclosures, supply/customer agreements, capacity changes. Macro/Fed-driven diffusion (FOMC surprises through the yield curve into banks, homebuilders, utilities) is a different transmission mechanism and is deferred to Phase I-B.

## 1. Event & document schema

A single economic event is usually reported more than once (8-K → press release → newswire → licensed-API article). Documents and events are now explicitly separate, many-to-one:

```
canonical_event_id, event_version, event_cluster_id

event_documents  (many documents -> one canonical event)
  event_id, document_id, relationship_to_event, first_seen_at

TIMESTAMP TAXONOMY  (kept distinct, never collapsed into one field)
  source_published_at        - what the source claims
  source_observed_at         - when our ingestion saw it published
  ingested_at                - when our pipeline actually fetched it
  canonical_first_public_at  - best estimate of true first disclosure
  first_public_timestamp_source     - which document/feed set it
  first_public_timestamp_precision  - explicit uncertainty window
  event_effective_at, decision_at, first_executable_at
```

**Why precision is a field, not an assumption:** SEC.gov states that filings are often available on sec.gov within one to three minutes of the EDGAR acceptance timestamp, that the lag can grow under load, and that there is no timestamp marking the exact moment a filing became visible on sec.gov itself (verified directly against SEC.gov before including this). Treating `canonical_first_public_at` as exact would let the system later claim a speed advantage smaller than its own timestamp uncertainty.

The surprise measure, previously present only as an unexplained name, is now fully specified:

```
SURPRISE  (per event)
  surprise_type        e.g. guidance_revision, capacity_change, order_win
  observed_value
  reference_value
  reference_source     company's own prior guidance/disclosure, for
                        Phase I-A — not analyst consensus (deferred)
  reference_timestamp
  unit, period
  surprise_raw          = observed_value - reference_value
  surprise_standardized = surprise_raw / |reference_value|
```

Retrospective processing of historical documents is restricted, by policy, to fact-extraction only (event category, entities, surprise value, cited relationship) — never to judging in hindsight whether a historical trade would have been attractive. Restated explicitly here since an LLM's training data can already contain how an old story played out.

## 2. Entity & instrument master

The largest omission in v2.0: every entity was identified by ticker, and tickers merge, split, delist, and get reused. Every experiment now references a permanent `entity_id`; ticker becomes display metadata, not a key.

```
entities:                  entity_id, legal_name, CIK, ...
instruments:                instrument_id, entity_id, exchange, asset_type
instrument_identifiers:     instrument_id, identifier_type, identifier_value,
                             valid_from, valid_to
corporate_actions:          instrument_id, action_type, effective_at,
                             adjustment_factor
```

This protects the historical comparable-event model from mis-joining a company to the wrong symbol years later, and keeps the universe survivorship-bias-free by construction.

## 3. Causal-link evidence

The two-probability idea from v2.0 stands (whether a relationship exists vs. whether a shock transmits through it, tracked separately). The tier definitions underneath were wrong on two counts.

**Correction, verified directly:** v2.0 treated an SEC customer-concentration disclosure as evidence of a *named* relationship. It usually isn't: ASC 280 requires disclosing that a customer represents 10%+ of revenue, but explicitly states the entity "need not disclose the identity of a major customer" (checked directly against the accounting standard via PwC's guide, since this claim had already propagated into two prior documents in this series). A 10% disclosure is real evidence *some* major customer relationship exists, but doesn't identify which company.

The tiers also conflated three axes that should be scored independently:

```
SOURCE AUTHORITY        (how trustworthy is the document itself)
  government | regulatory_filing | company | licensed_commercial | secondary_inference

RELATIONSHIP EVIDENCE   (does the specific link exist)
  explicit_named | quantified_named | inferred_structured | model_inferred

SHOCK TRANSMISSION EVIDENCE   (does this shock move through it)
  historical | economic_model | new_or_unobserved
```

A 10% customer disclosure is high source authority but weak relationship evidence alone (no name). A named partner on an earnings call is lower source authority but strong, explicit relationship evidence. Federal Reserve data is highly authoritative but says nothing about whether company A supplies company B — it belongs in the estimator's macro features (Phase I-B), not the relationship-evidence axis.

Raw LLM output and a calibrated probability are not the same thing:

```
raw_llm_relationship_score, raw_llm_transmission_score      (logged always)
calibrated_p_relationship,  calibrated_p_transmission,       (only once a
  calibration_model_version                                   calibration
                                                                model exists)
```

`relationship_confidence` from v2.0's exposure block is retired — it overlapped with `raw_llm_relationship_score` with no defined distinction.

## 4. The underreaction estimator

The v2.0 estimator compared a static historical expectation against the CAR observed at *detection* time and never updated — wrong whenever price moves between detection and decision, which is most of the time. Corrected to condition on everything observable at the actual decision timestamp:

```
UR_i(t, H) = E[ CAR_i(0,H) | X_i, I_t ] − CAR_i(0, t)

I_t (observable at decision time t), in addition to X_i from v2.0:
  - CAR realized so far, on both the direct entity and the linked entity
  - linked entity's intraday return path since the event
  - elapsed time since canonical_first_public_at
  - abnormal volume accumulated so far
  - contemporaneous sector/index movement
  - current volatility
  - any intervening disclosure specific to the linked entity
```

This makes the quantity what it needs to be — expected abnormal return remaining from now to H — not a historical total minus whatever happened since, which stops meaning anything once new information arrives intraday.

**The historical comparable-event dataset** that `E[CAR|X,I]` is estimated from now has its own requirements: a defined event taxonomy matching Section 1's categories; explicit date/source coverage; point-in-time availability; the Section 2 entity universe including delisted/acquired names; and a relationship-reconstruction method tied to Section 3's tiers. If Phase I-B adds macro events, use vintage-correct data — ALFRED exists because current FRED series get revised, and using today's revised figures would leak information unavailable at the time.

The estimator starts crude (grouped historical average, wide uncertainty interval) and grows more sophisticated only as data accumulates and clears the frozen-epoch discipline (Section 8). Abnormal volume remains a feature inside `I_t`, never a binary override.

## 5. Experimental arms & diagnostics

Arm G in v2.0 was underspecified in a way that would have broken the comparison: if Arm A's LLM both discovers candidates and ranks them, while Arm G has no principled way to generate candidates, A-vs-G tests four different things at once. Fix: force both through the *same* candidate universe.

```
Verified relationship graph (Section 3, evidence tier B or better)
              |
        same candidate set
          /          \
   Arm A: LLM      Arm G: mechanical
   ranks/selects    model ranks/selects
   from the set     from the same set
```

That isolates one question — does the LLM's ranking/interpretation add value, given identical inputs — separated from a later question: does LLM-driven relationship *discovery* itself add value over the structured graph alone.

| Arm | Action | What it isolates |
|---|---|---|
| **A** — AI causal trade | LLM ranks/selects from the shared candidate set | The system's actual proposal |
| **B** — Obvious trade | Buy the directly affected, headline company | Whether second-order reasoning beats chasing the headline |
| **C** — Sector trade | Buy the corresponding sector ETF | Whether the apparent edge is just sector beta |
| **D** — Placebo (cross-sectional) | Matched random security, same sector/liquidity, same time | Whether news-driven selection beats generic exposure right now |
| **E** — Cash-equivalent | Risk-free/short-duration Treasury rate, not literal zero | A correctly measured floor |
| **F** — Delayed-entry ladder | Same Arm-A trade at multiple delays (see below) | The signal-decay curve, α(t) |
| **G** — Mechanical baseline | Same candidate set as Arm A, using only event category, direct return, sector, historical correlation, volume, volatility — no LLM | Does the LLM add anything at all, holding candidate generation fixed |

**Added — an eighth measurement, kept out of the arm count on purpose:** Arm D controls for "would a similar security have moved anyway right now" but not "does ABC just drift regardless of news." A same-security, random-time placebo (diagnostic **H**, not an eighth portfolio) resamples ABC itself at matched non-event times in a similar volatility regime. D and H are complementary: D fixes time and varies the security, H fixes the security and varies time.

**Arm F needs two distinct decay curves**, since "return" is ambiguous once entry is delayed:

```
Event-clock decay:            every delayed entry exits at event_time + H
                               -> how much of the original opportunity
                                  remained available at each delay

Equal-holding-period decay:   every delayed entry exits at its own
                               entry_time + H
                               -> whether a late entry still forecasts
                                  its own future returns
```

"Immediate" entry is redefined precisely: the first executable timestamp *after the full pipeline has produced a signal* (extraction, entity resolution, dedup, underreaction estimate, arm construction) — not the source article's timestamp. Pipeline latency is part of what Arm F measures.

## 6. Phase I-A data sources

Unchanged from v2.0, scoped explicitly to corporate events: SEC EDGAR + company-originated material as the system of record; one paid, API-native news product as the trigger layer; an independent market-data provider decoupled from execution. Federal Reserve feeds move to Phase I-B along with the macro-diffusion estimator they'd require. WSJ/FT/Economist remain excluded pending a licensing review; nothing in the corporate-diffusion mandate requires them.

## 7. Database & schema

Postgres remains right for the experiment database (events, links, estimates, arm outcomes — genuinely low-volume, high-value-per-row). It stops being right if "market data" means continuous minute/tick-level history for a broad universe — that belongs in Parquet/object storage or gets queried on demand, not warehoused wholesale.

| Table | Holds |
|---|---|
| `raw_documents`, `event_documents` | Document/event separation, full timestamp taxonomy |
| `entities`, `instruments`, `instrument_identifiers`, `corporate_actions` | Permanent identity layer |
| `entity_relationships` | Three-axis evidence model + raw and (once it exists) calibrated probabilities |
| `extracted_events` | Event category, economic variables, surprise block, extraction/prompt version |
| `candidate_signals` | **Every candidate considered, selected or not**: candidate_id, event_id, entity_id, eligibility_status, selection_score, selected_boolean, rejection_reason, policy_version, decision_timestamp. Without this, a future threshold change can never be checked against what the prior system would have done |
| `underreaction_estimates` | X_i, I_t features, E[CAR\|X,I], observed CAR, uncertainty interval, scoring-model version/epoch |
| `experiments` → `experiment_arms` → `arm_entries` → `arm_outcomes` | Normalized so Arm F's multiple entries fit naturally |
| `quote_snapshots` | bid, ask, bid_size, ask_size, mid, last, quote_timestamp, data_provider — captured at each decision timestamp; OHLCV alone can't support a spread/slippage estimate |
| `market_data` | Point-in-time OHLCV/factor exposures for the specific windows needed, not a general tick archive |

## 8. Statistical framework

**The primary hypothesis is now fully specified.** One primary horizon fixed before evaluation (e.g., 5-day abnormal return, chosen in advance), against a pre-specified minimum economically meaningful margin δ — set from the realistic round-trip cost model plus a safety margin, not chosen arbitrarily:

```
H0:  mu(A - G) <= delta
H1:  mu(A - G) >  delta

Promotion requires the confidence interval's LOWER BOUND to exceed
delta -- not merely to exclude zero. A CI that excludes zero but sits
entirely below delta means the effect is real but too small to matter.
```

**Dependence is pre-specified, not a vague "effective N."** Inference clustered at the canonical-catalyst/event level, using a time-aware block bootstrap or multiway-clustered standard errors — effective sample size becomes a computed diagnostic.

**Competing-event contamination gets an explicit rule.** If a linked entity has its own material news within the outcome window, its return is no longer clean evidence about diffusion from the original event. Primary analysis censors at the first material competing event (or excludes under a pre-registered rule); the uncensored version is a robustness check only.

**The model is frozen during evaluation, never continuously updated on the data it's being judged against.** Train through a cutoff, freeze, score the next window prospectively without retraining, close that cohort, retrain the next version. Never retroactively replace one epoch's predictions with a later version's.

**Confirmatory analysis is a frozen, named family; everything else is exploratory by default and must replicate** on a genuinely new subsequent cohort before being acted on.

## 9. Promotion gates

Pipeline-quality gates are now measurable, checked against a held-out human-labeled sample distinct from whatever sample tuned the extraction prompts:

```
citation-validity precision AND recall
entity-resolution accuracy
relationship-link precision AND recall
duplicate-event precision AND recall
timestamp completeness
schema validation rate
pipeline failure rate
```

Recall matters as much as precision — a pipeline with zero hallucinated links because it rejects 99.9% of real ones has optimized for a metric, not usefulness.

| Transition | Gate |
|---|---|
| Build → running Phase I-A | All pipeline-quality metrics meet pre-fixed thresholds on the held-out sample; every table above populating correctly on live data, including `candidate_signals` for rejections |
| Phase I-A → human-confirmed trades | Pre-registered primary test clears (CI lower bound for A−G exceeds δ, net of costs) on an effective, correlation-adjusted sample in the low hundreds, across ≥2 distinct volatility/rate regimes, no single trade/category driving the result |
| Human-confirmed → limited autonomous | 50–100 human-confirmed trades consistent with the Phase I-A distribution; human role has functioned strictly as a safety veto (Section 11), every veto logged and reasoned; account type confirmed directly with Robinhood |
| Capital scale-up | Positive out-of-sample expectancy on a genuinely new cohort; drawdown inside a pre-committed budget; incremental increases, each re-confirming the edge |

## 10. Tech stack

Python, an LLM API with a versioned prompt and structured output matching Section 1's schema, `pgvector` for embedding-based novelty dedup, Postgres for the experiment database, an independent market-data API, a simple scheduled worker for ingestion, a lightweight dashboard for weekly human review. Two additions: bulk historical market series live in Parquet/object storage rather than Postgres; database access is split into restricted roles from the start (insert-only pipeline credential, separate rarely-used administrative credential, immutable periodic snapshots) — that's what makes the append-only claim in Section 11 actually true rather than aspirational.

## 11. Guardrails, correctly scoped

v2.0 stated append-only tables mean even a compromised component can't rewrite history. That's a design intent, not a fact, unless the database enforces it — broad credentials can still `UPDATE`/`DELETE`/`DROP TABLE`. It becomes true once the pipeline runs under an insert-only role, admin access is separated, an audit log captures every write, and periodic immutable snapshots exist as backstop.

**The human confirmation role, once Phase II begins, is a safety veto and nothing more.** If a reviewer approves attractive-looking trades and rejects uncomfortable ones, Phase II measures "AI plus human discretion," not the system under test. The reviewer's authority is limited to a fixed checklist of objective, pre-agreed disqualifying conditions (stale price, data error, obvious duplicate event, technical/execution problem) — never "does this thesis seem good" — and every veto is logged with which checklist condition triggered it.

## 12. Open items before Phase II

- Read the actual current WSJ, FT, and Economist terms of service before any re-enters the pipeline.
- Confirm directly which Robinhood account type the Agentic account will be — cash (consistent with "no leverage": T+1 settlement, good-faith/free-riding constraints) vs. margin (no longer subject to PDT rules as of Robinhood's June 2026 change, but reintroduces leverage).
- Re-verify the LLM API provider's usage terms and Robinhood's Trading MCP capabilities close to the Phase II build date — both have changed materially within the life of this review already.
- Wash-sale exposure from rapid re-entry, and a tax professional's review of the short-holding-period activity this design generates.

## 13. Provenance

Third document in the series: original design → adversarial audit → v2.0 spec (merging two rounds of cross-review) → this v2.1 revision (closing twenty-four gaps from a third round). Nearly all of round three's critique held up and is incorporated directly. Two claims were checked against primary sources before acceptance — SEC.gov's own statement on EDGAR timestamp precision, and ASC 280's actual text on customer-identity disclosure (the latter catching an error that had propagated through two earlier documents). A few points were extended rather than merely adopted: the δ-margin tied explicitly to transaction costs, the D/H placebo relationship, the human-veto checklist mechanism, and the Postgres/Parquet resolution.

---

*Ask: does anything in v2.1 still look wrong, underspecified, or likely to invalidate the experiment before this moves to implementation (schema DDL, extraction prompt, ingestion worker)?*
