# Dry Run 001 — Extraction-Runner Bridge, Real EDGAR Data, No Paid API Calls

> ## Addendum (2026-09-02) — two corrections to what this report establishes
>
> A ChatGPT review of this report, independently re-verified before being
> accepted (same discipline as every other round on this project), caught
> two things worth stating plainly rather than leaving implicit in the
> body below (which is otherwise left as originally written):
>
> 1. **"84/84 tests passing" (§8) was a weaker claim than it sounded.**
>    That number was read from `.pytest_cache/v/cache/nodeids` (a set that
>    only ever grows across some history of runs — it proves 94 [then 84]
>    distinct tests are *known to exist*, not that they all executed and
>    passed in *this specific invocation*) and `lastfailed` being `{}`
>    (which is also only rewritten when it changes, so an old, stale `{}`
>    looks identical to a fresh one). Neither is real evidence of a single
>    passing run. The actual fix-round report that follows this one
>    (dated 2026-09-02) captures real `pytest -v` output and a JUnit XML
>    file as durable, independently-checkable artifacts instead — see
>    `build/tests/pytest_run_002.xml` / `pytest_run_002.out.txt` — and
>    that discipline is now the standard going forward, not cache-file
>    inference.
> 2. **This dry run validated pipeline wiring and precision, not
>    extraction recall/completeness.** The manual extraction below
>    deliberately extracted only *some* of the true events and
>    relationships in each document (2 of Lilly's 4 named acquisitions in
>    §2.6, 1 counterparty per NVIDIA sentence instead of all named ones in
>    §2.9) to keep the round small enough to review by hand. That's a
>    legitimate scope choice for testing "do hand-verified facts survive
>    validation and flow correctly through resolution, canonicalization,
>    relationship-writing, and candidate generation" — but it means the
>    unresolved-mention rate in §3 and the candidate counts in §6 are
>    artifacts of what I *chose* to extract, not a measurement of what a
>    full-recall extraction over these same documents would produce. Don't
>    read this report's numbers as representative of real-world recall.
>
> Everything below this line is the original report, unchanged.

**Date:** 2026-09-01
**Database:** `diffusion_experiment_dryrun` (disposable, separate from the pytest-truncated `diffusion_experiment` DB — see setup below)
**Extractor:** Claude (this session), reading `extraction_prompt_v1.md`'s system + task prompt and each document's real `raw_content` directly, exactly as `AnthropicExtractionClient` would send them — no Anthropic API call was made. Recorded as `extractor_model_id="claude-sonnet-5-manual-dry-run"`, `extractor_model_version="2026-08-31-manual"` in `extraction_runs`.

## tl;dr

- Real EDGAR ingest across 4 companies (NVDA, CAT, LLY, JPM): **411 documents / 235 catalysts** ingested for real (full historical backlog, since `edgar_ingest_worker.py` has no per-company filing cap — see "Setup" for why).
- **9 documents** (each company's single most recent 8-K filing, primary + exhibits) were actually extracted by hand, matching the requested 5–10 cap.
- **18 events** produced, **36 evidence spans**, every one independently re-verified as an exact substring of the real `raw_content` *and* passing `jsonschema.validate` against the real schema, before being run through the actual pipeline.
- Full pipeline ran for real: `extract_document` → `process_catalyst` (entity resolution, canonicalization, relationship writes, candidate generation) — no shortcuts, no mocking beyond the LLM-call swap.
- **One real, load-bearing bug surfaced**: `Eli Lilly and Company`, exactly as Lilly's own press release names itself, fails to resolve against its own seeded watchlist entity — 9 mentions, 100% of Lilly's own self-references, all unresolved. Full breakdown below.
- Idempotency re-verified on this real data: re-running `process_catalyst` on all 4 catalysts correctly no-ops (`already_done_or_in_progress`), row counts unchanged.
- Main test suite (separate DB): 84/84 still passing, untouched by any of this.

## Setup

1. Created `diffusion_experiment_dryrun` on the local Postgres 16 cluster (port 5433, separate from the `diffusion_experiment` DB pytest truncates). Applied `schema.sql`, `migrations/002_extraction_runner.sql`, `migrations/003_extraction_runner_fixes.sql` in order.
2. Ran `seed_entities.py --dsn "dbname=diffusion_experiment_dryrun user=postgres host=localhost port=5433"` — all 108 companies, no cost.
3. Added `--only-ciks` and `--dsn` flags to `edgar_ingest_worker.py` (new, reusable), and `--dsn`/`--extractions-dir` to `extraction_runner.py`. Used `filter_watchlist(load_watchlist(conn), "0001045810,0000018230,0000059478,0000019617")` to scope one poll invocation to NVIDIA, Caterpillar, Eli Lilly, and JPMorgan Chase — one company per non-tech sector plus the original AI-hardware sector, without touching `watchlist_membership`.
4. Ran a real (non-dry-run) poll. `edgar_ingest_worker.py` has no per-company filing-count limit by design (a deliberate earlier fix removed lookback filtering entirely — see its module docstring's fix #15), so this pulled in **each company's entire historical 8-K backlog** (CAT and NVDA both back to 2019/2020; LLY and JPM's exact starting points weren't checked, but their lower catalyst counts are consistent with the same multi-year span at lower filing frequency), not just the most recent filing. That's a real, if slightly more than intended, live-data result — flagged under "Observations" below rather than quietly worked around.
5. **I then picked just the single most recent catalyst per company** (by real SEC filing/acceptance date, not ingestion order — see the ingestion-order pitfall in "Observations") for actual extraction, keeping the manual-extraction step within the requested 5–10 document cap. The other ~400 ingested documents sit in `raw_documents` unextracted in this disposable database — real data, deliberately not processed this round.

## 1. Companies polled, filings found

| Company | CIK | Catalysts ingested | Documents ingested | Most recent filing extracted |
|---|---|---|---|---|
| NVIDIA Corporation | 0001045810 | 63 | 120 | `0001045810-26-000073` (filed 2026-08-26) |
| Caterpillar Inc. | 0000018230 | 101 | 174 | `0000018230-26-000040` (filed 2026-08-04) |
| Eli Lilly and Company | 0000059478 | 45 | 72 | `0000059478-26-000077` (filed 2026-08-05) |
| JPMorgan Chase & Co. | 0000019617 | 26 | 45 | `0001193125-26-314128` (filed 2026-07-23) |
| **Total** | | **235** | **411** | |

All 4 filings ingested and extracted this round are genuinely new (SEC accession numbers not previously seen — this is a fresh database).

## 2. Per-document extraction: source quote next to saved JSON

Every JSON file below is saved verbatim at `build/dry_run_extractions/<document_id>.json`. The script that produced them (`build/dry_run_extractions/build_extractions.py`) is also checked in as a durable record of intent. Every `evidence_span` shown was mechanically re-verified as an exact substring of the real `raw_content` fetched from SEC (see `build/dry_run_extractions/sources/<document_id>.txt` for the full saved source) — I did not eyeball this, I wrote a script that checked it after I was done and before running the pipeline.

---

### 2.1 CAT primary — cover page (`1ab03952-2d9d-4ab6-b78e-c4505646b2f6`)

**Source:** https://www.sec.gov/Archives/edgar/data/18230/000001823026000040/cat-20260804.htm

> Item 2.02. Results of Operations and Financial Condition. On August 4, 2026, Caterpillar Inc. issued a press release reporting its financial results for the quarter ended June 30, 2026. A copy of the press release is attached hereto as Exhibit 99.1 and incorporated into this Item 2.02 by reference.

**Extraction:** one minimal `earnings_surprise` event, issuer = Caterpillar Inc., `surprise: null` (no figures are stated on the cover page itself — see `extracted_events.surprise_type` nullable fix from the earlier round, exercised here for real, not just in a test fixture).

---

### 2.2 CAT EX-99.2 — retail sales statistics (`c01f0bbd-4e68-4ccf-b5db-c9ed1e4f90ba`)

**Source:** https://www.sec.gov/Archives/edgar/data/18230/000001823026000040/ex992toformcat2q2026retail.htm

> Caterpillar Inc. ("Caterpillar", "we" or "our") is furnishing supplemental information concerning (i) retail sales of machines (including locomotives) to end users and (ii) retail sales of power systems... The information presented in this report is primarily based on unaudited reports that are voluntarily provided to Caterpillar by its independent dealers... the information presented in this report is intended solely to convey an approximate indication of the trends, direction and magnitude of retail sales and is not intended to be an estimate, approximation or prediction of...

**Extraction:** one `other_material_event`, issuer = Caterpillar Inc., `surprise: null`. I deliberately did not try to force the "Total Combined... UP 25%" retail-sales-growth figures into the `surprise` block — the document's own text explicitly disclaims that this data is "not intended to be an estimate" comparable to guidance, and the schema's `reference_source` field is supposed to be the company's own prior *guidance/disclosure*, which this genuinely isn't.

---

### 2.3 CAT EX-99.1 — Q2 2026 earnings release (`003d1990-3ffb-445d-a871-f47beb2eaca2`)

**Source:** https://www.sec.gov/Archives/edgar/data/18230/000001823026000040/ex991toformcat2q2026earnin.htm (1,120,654 bytes raw — by far the largest document this round; see "Observations")

> Second-quarter 2026 sales and revenues increased 24% to $20.5 billion ... Second-quarter 2026 profit per share of $7.77; adjusted profit per share of $8.17 ... Deployed $2.2 billion of cash for share repurchases and dividends in the second quarter

**Extraction:** 2 events —
- `earnings_surprise`: `surprise_type=revenue_yoy_change`, `observed_value=20.5`, `unit=USD_billions`, `period=Q2 2026`
- `buyback_or_capital_return`: `observed_value=2.2`, `unit=USD_billions`

I checked specifically for a numeric full-year outlook section (searched for "outlook", "full-year", "guidance") — found none; the only "full-year" hit was boilerplate about *when* Lilly-style adjusted metrics would later be discussed, not an actual guidance figure. So no `guidance_revision` event here — a real, checked absence, not an oversight.

---

### 2.4 JPM primary — debt offering closing, no exhibit (`ce47bfbd-5a40-458a-9655-9718bec53689`)

**Source:** https://www.sec.gov/Archives/edgar/data/19617/000119312526314128/d103985d8k.htm

> Item 8.01. Other Events. On July 23, 2026, JPMorgan Chase & Co. closed public offerings of (i) $500,000,000 aggregate principal amount of Floating Rate Notes due 2030 ..., (ii) $2,500,000,000 aggregate principal amount of Fixed-to-Floating Rate Notes due 2030 ..., (iii) $3,000,000,000 aggregate principal amount of Fixed-to-Floating Rate Notes due 2032 ... and (iv) $3,000,000,000 aggregate principal amount of Fixed-Rate Reset Subordinated Notes due 2041...

**Extraction:** one `other_material_event` (~$9B aggregate debt issuance), issuer = JPMorgan Chase & Co., `surprise: null`. This is a genuinely different kind of disclosure than the other three companies' filings this round — a capital-markets debt closing, not an earnings release — and it doesn't cleanly fit any of the 10 categories (it's not `buyback_or_capital_return`, which is specifically about *returning* capital, not raising debt). I used `other_material_event` rather than force a mismatch. No relationship extracted: Simpson Thacher & Bartlett LLP is named as outside counsel giving a legal opinion, not a supplier/customer/competitor/partner/acquirer_target in any real sense.

---

### 2.5 LLY primary — cover page (`38ec0ced-9aef-49bc-a1ee-6ea20e9a3a32`)

**Source:** https://www.sec.gov/Archives/edgar/data/59478/000005947826000077/lly-20260805.htm

> Attached hereto as Exhibit 99.1 and incorporated by reference into this Item 2.02 is a copy of the press release, dated August 5, 2026, announcing the financial results of Eli Lilly and Company for the quarter ended June 30, 2026.

**Extraction:** one minimal `earnings_surprise` event, issuer = **"Eli Lilly and Company"** (exactly as written — this is the entity name that turned out not to resolve; see §3), `surprise: null`.

---

### 2.6 LLY EX-99.1 — Q2 2026 earnings, guidance, M&A, capacity (`8b89a41e-b98a-4e19-8525-5fa9f1bb8f1b`)

**Source:** https://www.sec.gov/Archives/edgar/data/59478/000005947826000077/q226lillysalesandearningsp.htm

This was the richest document this round. Six genuinely distinct economic propositions, each extracted as its own event per the prompt's own instruction not to merge distinct propositions:

> Revenue in Q2 2026 increased 48% to $23.0 billion driven primarily by Mounjaro and Zepbound volume.

→ `earnings_surprise`, `observed_value=23.0`, `unit=USD_billions`

> Increased 2026 full-year revenue guidance to be in the range of $85.0 billion to $87.0 billion

→ `guidance_revision` (revenue). **Left `observed_value`/`reference_value` both `null`** — the schema's surprise block has single-number fields, and this guidance is a *range*; picking either bound as "the" number would be a fabricated simplification the extraction prompt explicitly warns against ("do not infer or estimate"). The real figures are preserved verbatim in `evidence_span`. Flagged under "Observations" as a real schema/reality mismatch.

> resulting in an updated range of $35.50 to $36.50

→ `guidance_revision` (EPS), same range-representation issue.

> completed acquisitions of Orna Therapeutics, Inc., Ajax Therapeutics, Inc., Centessa Pharmaceuticals plc. and Kelonia Therapeutics, Inc.

→ two separate `acquisition_or_divestiture` events (Orna, Ajax — I extracted 2 of the 4 named acquisitions to keep this round's scope reasonable, not all 4), each with a `relationship_type=acquirer_target` relationship (Lilly → target). **Neither target company is in the 108-company watchlist** — both correctly logged to `unresolved_entity_mentions`, not fabricated as new entities (§3).

> Committed an additional $4.5 billion to expand Indiana manufacturing sites.

→ `capacity_change`, `observed_value=4.5`, `unit=USD_billions`.

---

### 2.7 NVDA primary — cover page (`daa5a135-bac6-4eb4-b453-a4ff3fdfd101`)

**Source:** https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/nvda-20260826.htm

> On August 26, 2026, NVIDIA Corporation, or the Company, issued a press release announcing its results for the quarter ended July 26, 2026. The press release is attached as Exhibit 99.1... Attached hereto as Exhibit 99.2... is financial information and commentary by Colette M. Kress, Executive Vice President and Chief Financial Officer...

**Extraction:** one minimal `earnings_surprise` event, issuer = NVIDIA Corporation, `surprise: null`.

---

### 2.8 NVDA EX-99.2 — CFO commentary (`850832f9-b1f7-45ad-95bd-71e332131b1e`)

**Source:** https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/q2fy27cfocommentary.htm (256,881 bytes)

> Revenue for the second quarter was a record $96.2 billion, up 106% from a year ago and up 18% sequentially.

**Extraction:** one `earnings_surprise` event — deliberately given the **same** `surprise_type`/`period`/`observed_value` as the press release's own revenue event (§2.9) to test catalyst-level canonicalization on real, independently-worded duplicate content. It worked (§4).

**A genuine surprise while reading this one, worth flagging on its own** (see "Observations" for the full note): this entire ~20,000-character document is written in first person ("we", "our") and **never names NVIDIA in prose at all**. The only literal, genuine company-name mention in the whole document is "NVIDIA CORPORATION" as a table header in a GAAP-reconciliation appendix near the very end — that's the `evidence_span` I actually used for the issuer entity, not something earlier or more prominent. A stricter extractor (or a lower-effort one) could plausibly have produced zero events for this document by failing to find any citable entity mention at all, since `validate_extraction_output` correctly drops a whole event if no entity survives span verification.

---

### 2.9 NVDA EX-99.1 — Q2 FY2027 press release (`33a7d374-9878-4d14-a039-ee18e591f959`)

**Source:** https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/q2fy27pr.htm (341,113 bytes)

> NVIDIA (NASDAQ: NVDA) today reported revenue for the second quarter ended July 26, 2026, of $96.2 billion, up 18% from the previous quarter and up 106% from a year ago.

→ `earnings_surprise`, `observed_value=96.2` — **canonicalized with §2.8's CFO-commentary event into one `canonical_events` row** (verified: 2 `event_document_links` rows).

> Revenue is expected to be $108.0 billion, plus or minus 2%.

→ `guidance_revision`, `observed_value=108.0` — a genuine single-point-plus-tolerance guidance figure (not a range like Lilly's), so unlike §2.6 this one *does* get a real `observed_value`.

> NVIDIA returned approximately $26.0 billion to shareholders in the form of shares repurchased and cash dividends

→ `buyback_or_capital_return`, `observed_value=26.0`.

> Announced strategic partnerships to establish independent compute financing platforms with Apollo, BlackRock, Blackstone, Brookfield, Goldman Sachs and KKR

and

> racks running at partners including CoreWeave, Google Cloud, Microsoft Azure, Oracle Cloud Infrastructure and Nebius

→ one `other_material_event` with two `partner` relationships: NVIDIA↔Goldman Sachs and NVIDIA↔CoreWeave. I picked one named counterparty from each sentence (not all 11 named companies across both) to keep this reviewable, deliberately choosing one that IS in the 108-company watchlist (Goldman Sachs) and one that isn't (CoreWeave) to get a genuine mix of resolved/unresolved outcomes rather than an artificially easy or artificially hard case.

## 3. Entity resolution — the real numbers

| Metric | Count |
|---|---|
| Total resolution attempts across all 4 catalysts | 30 |
| Matched | 15 |
| Unresolved | 15 |

(Per catalyst: CAT 4 matched / 0 unresolved; JPM 1 matched / 0 unresolved; LLY 0 matched / 13 unresolved; NVDA 10 matched / 2 unresolved.)

**Unresolved, by raw string exactly as extracted (each row is one distinct occurrence, not deduplicated — a mention is logged every time it's encountered, per `entity_resolution.py`'s design):**

| Raw name | Normalized | Occurrences | Why |
|---|---|---|---|
| `Eli Lilly and Company` | `eli lilly and` | **9** | **Real bug — see below.** |
| `Orna Therapeutics, Inc.` | `orna therapeutics` | 2 | Correctly outside the 108-company watchlist. |
| `Ajax Therapeutics, Inc.` | `ajax therapeutics` | 2 | Correctly outside the 108-company watchlist. |
| `CoreWeave` | `coreweave` | 2 | Correctly outside the 108-company watchlist. |

**The one load-bearing finding: `normalize_entity_name` cannot match Eli Lilly's own name to itself.**

- `seed_entities.py` seeded Lilly's `entities.legal_name` as **`ELI LILLY & Co`** (SEC's own `company_tickers.json` title, verbatim), which normalizes to `eli lilly` (the `&` is stripped as punctuation, `Co` is stripped as a corporate suffix).
- Lilly's own press release — the actual, real text a real extraction would see — names itself **`Eli Lilly and Company`**, which normalizes to `eli lilly and`. `"and"` is a real English word, not a corporate suffix, so `normalize_entity_name` correctly does *not* strip it — but that means the two forms never converge.
- **Every single self-reference to Lilly in both of its own documents failed to resolve** — not just the target companies. This means, as extracted this round, Lilly's own 6 canonical events have **zero `event_entities` rows and zero possibility of any relationship or candidate**, even though the events themselves were successfully canonicalized. A real company on the watchlist produced real economic events that are currently invisible to entity resolution, candidate generation, and (eventually) the underreaction estimator — not because of a missing counterparty, but because the issuer itself doesn't match its own seed data.
- This is exactly the class of gap the design doc's `normalize_entity_name` docstring already flagged as a known limitation for SEC-title oddities (`build/seed_entities.py`'s own docstring warned about name-*order* quirks like "HORTON D R INC") — but this is a different, previously-unflagged case: an `and`/`&` abbreviation mismatch, not a name-order problem. **Not fixed in this round** (out of scope for a dry run whose job is to surface findings, not patch them), but this is the single most actionable thing to fix before a wider run — I'd guess this exact `&`-vs-`and` pattern likely affects other watchlist companies too (any SEC title using `&`).

**Two things that correctly resolved and are worth confirming, not just assuming:**
- `NVIDIA` / `NVIDIA Corporation` / `NVIDIA CORPORATION` — matched every one of its 10 occurrences across all 3 NVDA documents. Suffix-stripping (`Corporation`) works as intended here.
- `Goldman Sachs` — matched against the seeded `GOLDMAN SACHS GROUP INC`. This one is *not* obvious: it only works because `"group"` is itself in `normalize_entity_name`'s corporate-suffix list, so `GOLDMAN SACHS GROUP INC` → strip `Inc` → strip `Group` → `goldman sachs`, exactly matching the press release's informal `Goldman Sachs`. Worth knowing this was a deliberate design choice earning its keep, not a coincidence.

## 4. Canonicalization — verified on real duplicate content

| Catalyst | Canonical events | Notable |
|---|---|---|
| CAT | 4 | Two *separate* `earnings_surprise` events (cover page's null-surprise stub + exhibit's real figures) — **did not merge**. See "Observations." |
| JPM | 1 | — |
| LLY | 6 | Two distinct `guidance_revision` events (revenue range, EPS range) correctly stayed separate — different `surprise_type`. |
| NVDA | 5 | One `earnings_surprise` event has **2** `event_document_links` — the press release and CFO commentary's independently-worded but matching revenue statements correctly merged into one canonical event. |

The NVDA merge is the real-data confirmation of the exact mechanism the design doc's catalyst-level canonicalization exists for. The CAT (and, by the same mechanism, NVDA's and LLY's) *non*-merges of a cover page's contentless stub against its own exhibit's full event are a genuine, previously-untested consequence of the fingerprint approach — see "Observations."

## 5. Relationships written

| entity_a | entity_b | type | evidence |
|---|---|---|---|
| NVIDIA CORP | GOLDMAN SACHS GROUP INC | `partner` | `explicit_named` |

I extracted 4 relationships this round: Lilly→Orna, Lilly→Ajax, NVIDIA→Goldman Sachs, and NVIDIA→CoreWeave. Only the last one actually got **written**, because writing requires **both** sides to resolve — the other 3 each had an unresolved counterparty (Orna, Ajax, CoreWeave) or, in Lilly's two, an unresolved issuer as well. This is `process_catalyst` behaving exactly as designed — not a bug — but it's a stark illustration of how much of the relationship graph a single normalization gap or a single out-of-watchlist counterparty can silently prune: 75% of the relationships extracted this round never made it into `entity_relationships` at all.

## 6. Candidates generated

| eligibility_status | eligibility_reason | entity | event_category | count |
|---|---|---|---|---|
| eligible | — | GOLDMAN SACHS GROUP INC | buyback_or_capital_return | 1 |
| eligible | — | GOLDMAN SACHS GROUP INC | earnings_surprise | 2 |
| eligible | — | GOLDMAN SACHS GROUP INC | guidance_revision | 1 |
| eligible | — | GOLDMAN SACHS GROUP INC | other_material_event | 1 |

**5 candidates, all eligible, all Goldman Sachs, all under NVIDIA's catalyst.** `generate_candidates_for_event_version` runs once per event *version*, checking *all* of the issuer's relationships regardless of which specific event within the catalyst produced them — so with only one relationship written for the whole NVDA catalyst, Goldman Sachs shows up as a candidate for all 5 of NVIDIA's canonical events this round, not just the one event that actually named it. That's correct behavior (a real supplier/partner relationship is relevant context for every event from that issuer, not just the one where it was mentioned), but it's a real, visible consequence worth being aware of before reading too much into "5 candidates" as if they were 5 independently-discovered signals.

No `ineligible` candidates were produced this round — every relationship that *did* get written happened to pass every timing/validity check (evidence was `explicit_named`, `evidence_publicly_available_at` was the real historical filing-observation timestamp from hours earlier, `system_observed_at`/`decision_at` were captured seconds apart during this run). The `ineligible` path (and its `eligibility_reason` variety) is already covered by the unit/integration tests from the previous round; this dry run didn't happen to exercise it on real data, which is itself worth noting rather than implying it was tested here.

## 7. Observations — things that looked surprising or wrong while actually doing this

1. **The `&` vs `and` entity-resolution gap (Eli Lilly) — see §3.** The single most concrete, actionable finding from this round.
2. **`edgar_ingest_worker.py` has no per-company filing-volume control.** Scoping to 4 companies via `--only-ciks` still pulled in 411 documents / 235 catalysts (each company's multi-year 8-K history (CAT and NVDA both went back to 2019/2020; I didn't check exactly how far LLY and JPM's backlogs reached, but their catalyst counts — 45 and 26 respectively, vs. CAT's 101 and NVDA's 63 — suggest a similar multi-year span, just lower filing frequency)), because the worker's own design deliberately removed lookback filtering (see its fix #15). That's correct for a real production poller (SEC's own submissions API already bounds it, and dedup handles re-polls), but it means `--only-ciks` alone is not sufficient to keep a *first-time* backfill small — a `--since` or `--max-filings-per-company` cutoff would be a reasonable follow-up if repeated small dry runs like this one become routine. I didn't add this since the current task only asked for company scoping, but it's the natural next ask.
3. **Ingestion order is not filing-recency order.** My first attempt to find "the most recent catalyst per company" ordered by `canonical_first_public_at` (this pipeline's own observation timestamp) — which is monotonically increasing in *ingestion* order, not filing date, since `edgar_ingest_worker.py` processes SEC's `recent` array newest-filing-first but stamps its own wall-clock time as it goes. That ordering gave me CAT's *oldest* filing (2019) as the "most recent." Caught before extraction started by switching to `sec_acceptance_at` (SEC's own real timestamp). Worth being deliberate about which timestamp column means what — this project's schema is careful about exactly this distinction (§3 of the spec) and I still nearly used the wrong one myself.
4. **A cover page's contentless stub genuinely does not canonicalize with its own exhibit's full event.** For CAT, LLY, and NVDA alike, the 8-K cover page states "we reported earnings, see the exhibit" with no numbers, and — because `extracted_events.surprise_type`/`observed_value`/`reference_value` are part of the canonicalization fingerprint — a `null`-surprise stub event never matches a numeric event describing the same real-world earnings release. This means, for a typical earnings 8-K, expect **two** `earnings_surprise` canonical events per catalyst as the normal case (one contentless, one real), not one — worth knowing before treating "canonical event count" as a proxy for "distinct real-world events." Whether the contentless stub should even be extracted as an event at all (vs. an empty `events` array) is a genuine judgment call I flagged rather than silently resolved in the earlier round; this round is real evidence for revisiting it.
5. **`reference_source`'s JSON Schema type doesn't allow `null`, unlike `observed_value`/`reference_value`.** `extraction_prompt_v1.md`'s own task instructions say "If a field cannot be determined from the document, use null rather than guessing," but the schema declares `reference_source` as `{"type": "string"}` with no `null` option — three of my extractions initially failed real `jsonschema.validate()` for exactly this reason before I caught it and used an explicit "not stated" string instead. This is a real inconsistency in `extraction_prompt_v1.md` itself, not something I introduced — worth fixing in the prompt/schema so a real LLM extraction doesn't hit the same wall.
6. **Range-valued guidance doesn't fit the surprise block's single-number fields.** Lilly's real guidance ("$85.0 billion to $87.0 billion") is a range, and the schema's `observed_value`/`reference_value` are single numbers. I left both `null` rather than fabricate a midpoint or an endpoint, preserving the real figures only in `evidence_span`. This is common in real guidance language (most companies guide to a range, not a point), so this will recur constantly in a wider run — worth a real design decision (a `low`/`high` pair? a documented "use the midpoint" convention?) rather than leaving every range-guidance event's numeric fields empty by default.
7. **A CFO-commentary-style exhibit can genuinely never name its own company in prose** (§2.8). Not a bug — `validate_extraction_output`'s "drop the whole event if no entity survives" behavior is correct and was NOT triggered here only because I found a real, if buried, company-name mention in a reconciliation table appendix. A real LLM extractor reading only the first few thousand characters (context-window or attention effects) could plausibly miss it and legitimately produce zero events for an entire exhibit that's obviously "about" NVIDIA to a human reader. Worth knowing this failure mode exists before being surprised by it later.
8. **The 1.1MB CAT earnings-release exhibit** was by far the largest document handled this round and the only one I didn't read in full character-for-character (I located and verified its substantive earnings/guidance/capital-return content specifically rather than reading every financial-statement table row). Nothing in the pipeline itself has a document-size limit that I'm aware of, but it's worth flagging that "read the document and extract" gets meaningfully more expensive (in tokens, for either a human or a real LLM call) as filings grow, and Workiva-style inline-XBRL HTML is extremely verbose relative to its actual prose content (1.12MB of markup for a press release whose substance is a few thousand words).

## 8. What was NOT touched

- The main `diffusion_experiment` test database — untouched; `pytest` still shows 84/84 passing against it, confirmed after this dry run.
- The full 108-company watchlist — only 4 companies' `watchlist_membership` rows were ever queried; nothing was added, removed, or reordered.
- No `ANTHROPIC_API_KEY` was read or used. `llm_client.FileBackedExtractionClient` was the only client instantiated; `AnthropicExtractionClient` was never constructed in this session.

## Artifacts

- `build/dry_run_extractions/*.json` — the 9 extraction outputs, exactly as fed to the real pipeline.
- `build/dry_run_extractions/build_extractions.py` — the script that generated them (a durable record of intent, including the evidence spans and the reasoning for each).
- `build/dry_run_extractions/sources/*.txt` — the real, saved `raw_content` for all 9 documents, exactly as fetched from SEC, for independent re-verification of every `evidence_span` above.
- `llm_client.FileBackedExtractionClient` (new, in `llm_client.py`) — replays a saved JSON file instead of calling a real LLM; permanent, reusable for future no-cost dry runs.
- `edgar_ingest_worker.py --only-ciks` / `--dsn`, `extraction_runner.py --dsn` / `--extractions-dir` (new, permanent CLI flags) — reusable for scoping a future dry run the same way.
