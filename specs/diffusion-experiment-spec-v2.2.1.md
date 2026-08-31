# The Diffusion Experiment — v2.2.1

*Closes four remaining corrections plus one factual wording fix from a fourth review's confirmation pass, and adds a concrete low-overhead operating model for Phase II now that real deployment (small capital, ~5–10 minutes of human attention per day) is a stated goal rather than a hypothetical. This closes the prose-design cycle — see Section 0.*

**Revised:** 30 August 2026 · **Supersedes:** v2.2 · **Scope:** Phase I-A unchanged; Phase II now has an explicit day-to-day operating design (Section 14)

---

## 0. What changed in this revision

The fourth review round confirmed nine of v2.2's twelve fixes as clean and correct, and found two of the remaining three were slightly wrong rather than incomplete:

1. The bitemporal fix used one field, `system_first_knew_at`, for two different things — when evidence became *publicly* available, and when *this system* actually observed it. Collapsing those meant a historical backtest built today from a 2023 filing would incorrectly treat that relationship as unknowable until today, when the market actually knew it in 2023. Split into two fields (below).
2. `expected_transmission_effect` was added as a fix for the fake "transmission probability," but then folded straight into `X_i`, the same vector used to estimate the outcome it's a proxy for — a real risk of the estimate quietly leaking the answer into its own input. Removed from primary `X_i` until it comes from a separately frozen, prior-only model.
3. Two phrasings in Section 6 overclaimed: "the entire requirement" for EDGAR access, and "large majority" for coverage, neither measured. Softened to what's actually established.
4. "Fully specified" was still premature — the pre-registration object exists and is correctly structured, but its numbers aren't filled in yet. Renamed to "pre-registration contract" with an explicit pilot/confirmatory boundary.
5. One factual tightening, no design impact: the June 2026 day-trading rule change is a FINRA Rule 4210 amendment approved by the SEC, not "an SEC rule elimination" — same practical effect, more accurate institutional description.

**This closes the conceptual-review cycle.** Both this round and the prior one independently concluded the same thing: what's left is either a number to pick (Section 8), a config value to set (Section 1's transform registry), or something to test in code (bitemporal leakage, schema integrity) — not something another round of written critique can usefully resolve. No further rounds are planned. Reopen this process only if implementation surfaces a genuine new design problem, not to double-check something already checked twice.

**Also new:** the goal has shifted from "eventually, maybe" to a concrete Phase II plan — real capital in the $1,000–3,000 range, via Robinhood Agentic, with about 5–10 minutes of human attention per day. Section 14 designs specifically for that constraint, since the human-veto role in Section 11 has to actually fit inside that time budget or it isn't a real safeguard.

## 1. Event & document schema

Unchanged from v2.2 except the field-naming fix below (Section 3) propagates here: the candidate-eligibility check and the bitemporal fields now share one consistent name for "publicly available," instead of v2.2's mismatch between `evidence_published_at` (used in Section 3's eligibility rule) and `system_first_knew_at` (used in the bitemporal paragraph). See Section 3.

## 2. Entity & instrument master

Unchanged from v2.2.

## 3. Causal-link evidence

**Three clocks, not two.** v2.2's bitemporal fields conflated "when the evidence became public" with "when this particular system happened to ingest it" — those are genuinely different things, and using one field for both breaks in both directions: a historical backtest built long after the fact would wrongly treat old, publicly-known information as unknowable, while a system that's slow to ingest something would wrongly treat information it hasn't processed yet as already known if the field were set to the public date instead. Corrected:

```
relationship_valid_from, relationship_valid_to        (economic reality — unchanged)
evidence_publicly_available_at, evidence_public_time_precision   (when the market could know)
system_observed_at                                     (when this pipeline actually ingested it)
record_superseded_at                                   (correction/retraction — unchanged)
```

Two different queries use two different clocks, deliberately:

```
Historical / backtest research:   evidence_publicly_available_at <= historical_decision_at
Live / forward (shadow or real):  system_observed_at <= decision_at
```

The candidate-eligibility rule now uses the same field name as the bitemporal check, closing the inconsistency the fourth review caught:

```
candidate_eligible =
    relationship_evidence IN (explicit_named, quantified_named)
    AND evidence_publicly_available_at <= decision_at
```

**`expected_transmission_effect` and `transmission_effect_interval` remain in the schema** (logged always, as experimental features) but are **removed from primary `X_i`** — see Section 4. They may re-enter the primary estimator only once produced by a transmission model that is separately trained, frozen, and versioned:

```
transmission_model_version, transmission_estimated_as_of, transmission_training_cutoff
```

with `transmission_training_cutoff` enforced to predate the event being scored, so the transmission estimate can never have seen the outcome it's helping predict.

## 4. The underreaction estimator

`X_i` no longer includes `expected_transmission_effect`, to avoid a feature that is itself derived from the kind of outcome data `X_i` is used to predict:

```
X_i =
  event_category, surprise_transformed, relationship_type,
  source_authority, relationship_evidence,
  historical_linkage_measure, linked_security_liquidity, sector,
  volatility_regime, time_since_disclosure
```

Everything else in Section 4 (the decision-time-conditioned estimator, the two-layer intraday/daily return methodology, log returns throughout, the trading-session execution policy) is unchanged from v2.2.

## 5. Experimental arms & diagnostics

Unchanged from v2.2.

## 6. Phase I-A data sources

Two phrasings tightened, no change to the underlying plan (free public sources only, deferred commercial licensing):

**EDGAR access, restated accurately instead of categorically.** Not "identify yourself and stay under the rate limit — that's the entire requirement." EDGAR explicitly supports scripted public access, but subject to the SEC's current fair-access and identification guidance as a whole: efficient, moderate request patterns; downloading only what's needed; identifying automated traffic appropriately; staying within the published rate ceiling (currently 10 requests/second); and the SEC's ability to restrict excessive or unidentified automated access. This project's usage pattern (a handful of filings a day, not bulk scraping) sits comfortably inside that, but the requirement is "follow the actual current guidance," not one rule in isolation.

**Company-IR sources get a defined, bounded architecture instead of an implied general-purpose crawler.** Not every public company's investor-relations page should be treated as a uniformly crawlable API just because it's publicly visible. Concretely:

```
Automated by default:   EDGAR APIs/feeds, EDGAR filing exhibits (earnings decks,
                        press releases, presentations already filed as exhibits)
Company-IR sources:     RSS/Atom feed where the company provides one; an explicit
                        JSON/API endpoint where offered; otherwise a small,
                        explicitly allowlisted set of issuer pages checked
                        individually against their normal access terms —
                        never a generalized crawl of arbitrary IR sites
```

**Coverage is a measured empirical fact, not an asserted one.** Replacing "captures the large majority of in-scope cases": coverage is expected to be substantial for the corporate-event categories in scope, since 8-Ks and earnings releases are routinely filed or issuer-published — but this is an assumption, not yet a number. Phase I-A records source and event-category counts from the start specifically so this can be checked rather than assumed.

**What actually changes as a result of this scope:** the experiment now tests underreaction to *issuer-originated and regulatory disclosures observable through this specific source set*, not underreaction to "all corporate news" in the broadest sense. That's a narrower, more precisely defined question — a cleaner thing to actually measure, not a compromised version of a bigger one.

## 7. Database & schema

Unchanged from v2.2, with the field-name correction from Section 3 (`evidence_publicly_available_at` / `system_observed_at` replacing `evidence_published_at` / `system_first_knew_at` everywhere they appear) and `expected_transmission_effect` moved out of `underreaction_estimates`' `X_i` block into its own logged-but-not-primary column.

## 8. Statistical framework

**Renamed from "fully specified" to what it actually is: a pre-registration contract.** The structure — horizon, confidence level, inference method, δ, position rules, abstention, direction — is correct and complete as a template. It becomes an actual specified hypothesis only once every value in it is a real number, not a placeholder:

```
inference_method, delta, A_position_rule, G_position_rule,
notional_per_event, max_positions_per_catalyst, abstention_rule, direction_rule
```

**An explicit boundary between choosing those numbers and testing them, so the same data can't do both:**

```
ENGINEERING / PILOT COHORT  ->  may inform parameter selection (delta, horizon, etc.)
        |
parameters frozen, written down, dated
        |
CONFIRMATORY COHORT STARTS  ->  never used to select parameters, only to test them
```

Using pilot-period results to pick δ or the horizon, then counting those same pilot observations as confirmatory evidence, would silently invalidate the test. The pilot and confirmatory cohorts must be non-overlapping.

Everything else in Section 8 (the corrected δ definition, the reversed and now-primary fixed-horizon censoring rule, catalyst-level clustering) is unchanged from v2.2.

**Config, not the LLM, computes the standardized surprise.** To keep Section 1's surprise-transform registry engineering-ready: the extraction prompt returns only raw facts (`observed_value`, `reference_value`, `unit`, `period`, `event_type`, `supporting_evidence`) — a deterministic script, not the LLM, applies the transform-registry math. The registry's exact parameters (denominator floors, robust-scale definitions, minimum-history requirements) live in versioned config:

```
surprise_transform_registry:
  event_type, transform_type, scale_method, denominator_floor,
  parameters, effective_from, version
```

## 9. Promotion gates

Unchanged from v2.2, including the bitemporal-leakage test requirement added there.

## 10. Tech stack

Unchanged from v2.2.

## 11. Guardrails, correctly scoped

Unchanged in principle from v2.2 (append-only enforced by database roles; human role in Phase II is a fixed-checklist safety veto only). Section 14 makes this concrete for the actual account size and time budget now in scope.

## 12. Open items before Phase II

Unchanged from v2.2 (Robinhood account type, LLM/Robinhood terms re-verification close to build date, wash-sale/tax awareness, revisit data-licensing only if scale changes materially) — one item resolved: the day-trading rule change is a **FINRA Rule 4210 amendment, approved by the SEC**, effective 4 June 2026, with a transition period for firms through 20 October 2027. Same practical effect as previously described (no $25k PDT threshold on margin accounts); more precise attribution.

## 13. Provenance

Fifth document in the series (original design → audit → v2.0 → v2.1 → v2.2 → this v2.2.1). This round's fourth-review-pass confirmed nine of v2.2's twelve fixes as clean, corrected two (bitemporal clock semantics, transmission-effect circularity), reframed one claim (pre-registration completeness), tightened two overclaims in Section 6, and made one factual attribution more precise (FINRA Rule 4210 via SEC approval, not "an SEC rule elimination"). No further prose-review round is planned; the reviewer independently reached the same conclusion. Both reviews are in full agreement that the schema (Sections 1–3, 5, 7), the extraction prompt's scope (raw facts only, no LLM-computed statistics), and the SEC ingestion worker are ready to build now; the market-data worker's interface is ready but its concrete adapter waits on picking a provider.

## 14. Phase II operating model for a small account with limited daily attention

This section didn't exist before v2.2.1 because Phase II was previously discussed only in the abstract. It's now concrete: real capital in the **$1,000–3,000** range, via Robinhood Agentic, with a stated **5–10 minutes of human attention per day** — small enough that the safety design has to actually fit that budget, not just claim to.

**The mechanism that makes low daily attention safe: nothing waits on the human to prevent a loss in real time.** The human's fixed-checklist veto (Section 11) happens once, in a scheduled batch, not by watching the market. Concretely:

```
During the day:   pipeline runs unattended, extracting events, estimating
                  underreaction, generating candidate signals — nothing
                  executes yet

Once per day,     human opens a short review queue: each pending signal
~5-10 min         shown against the fixed checklist (stale price? data
(e.g. before       error? duplicate event? technical/execution problem?)
market open):     Approve or veto each. Nothing else is being judged --
                  not "does this look like a good trade."

On approval:      the trade executes at that session's regular-hours open
                  (per the Section 4 trading-session policy), and a
                  broker-native protective stop-loss order is placed
                  immediately alongside it, sized per the risk budget below

Between reviews:  positions are protected by their own standing stop-loss
                  orders, not by a person watching a screen. A lost
                  connection or an outage means "place no new orders,"
                  never "auto-liquidate everything," per the existing
                  connectivity guardrail
```

If signal volume ever exceeds what can be reviewed properly inside that daily window, that is itself a stop signal — the answer is to pause new approvals and find out why volume jumped, never to start rubber-stamping the queue to keep up. Given Phase I-A's expected event cadence (low hundreds of qualifying events a year, across all categories combined), a handful a week is the realistic range to plan around, not several a day.

**Position sizing, sized honestly for $1,000–3,000, not scaled down from an institutional design.** Two numbers from Section 8's pre-registration contract get concrete starting values here (to be revisited once pilot data exists, per Section 8's pilot/confirmatory boundary):

```
notional_per_event      starting point: 5-10% of account equity per position
                         ($50-300 at this account size)
max_concurrent_positions  starting point: 3-5, to avoid concentrating a small
                         account in one or two correlated bets
```

**Worth being direct about, since it affects whether this is worth doing at this size:** at $1,000–3,000, even a genuinely real, statistically confirmed edge produces small dollar amounts — a meaningfully positive result in percentage terms might still be a few hundred dollars a year in absolute terms once real. Robinhood doesn't charge per-trade commissions, but the bid-ask spread on a $100–300 position is a larger fraction of that position than it would be on a $10,000 one, so the net-of-cost accounting Section 8 already requires matters more at this size, not less — a strategy that clears its statistical bar gross of costs can still fail to clear it net of costs at small size. None of this is a reason not to do it if the goal is genuinely to learn whether the idea works and to build the system as a project — it's a reason not to expect meaningful income from $1,000–3,000, and a reason to treat this money the way the rest of this document already implicitly assumes: money whose complete loss would be disappointing, not damaging. I'm not a financial advisor and this isn't financial advice — it's just the arithmetic of trading costs versus position size, so it's factored into sizing decisions rather than discovered after the fact.

---

*This is the terminal document of the conceptual-design phase. The next artifacts in this series should be code, not more specification text: the database DDL, the extraction prompt, the SEC ingestion worker, and a small test suite specifically targeting the bitemporal-leakage scenario, the surprise-transform edge cases, and the A-vs-G shared-candidate-universe logic.*
