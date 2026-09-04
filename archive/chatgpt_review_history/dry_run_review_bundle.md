# Dry Run 001 Review Bundle — Extraction-Runner Bridge, Real EDGAR Data

This bundle supports an adversarial review of the first live (real EDGAR
filings, no paid API calls) dry run of the extraction-runner bridge. It
contains: (1) the full dry-run report as Claude Code produced it, (2) the
real code for the one module the report's central finding concerns
(`entity_resolution.py`), (3) the relevant schema/prompt fragments for the
two other real findings, (4) two of the real extraction JSON files that
show the issues directly, and (5) my own independent verification notes —
what I checked myself before trusting any of this, and three proposed
fixes for review, not yet implemented.

---

## Part 0 — Independent verification notes (from Claude, in Cowork)

Before trusting this report, I independently checked every load-bearing
claim in it against the actual repository state (via a read/write file
bridge to the developer's machine — I do not have shell access there, so
I could not literally re-run pytest or `git log` myself):

- **The Eli Lilly `&`/`and` normalization failure**: traced
  `normalize_entity_name()`'s actual code (reproduced below) by hand
  against both strings. `"ELI LILLY & Co"` → `eli lilly`.
  `"Eli Lilly and Company"` → `eli lilly and`. Confirmed exactly as
  reported — this is a real, reproducible bug, not a hallucinated finding.
- **The `reference_source` non-nullable JSON Schema field**: read
  `extraction_prompt_v1.md`'s actual fenced output schema. Confirmed:
  `reference_source` is `{"type": "string"}` (no `null` option), while
  `observed_value`/`reference_value` are `{"type": ["number", "null"]}`.
  Real inconsistency, confirmed in the current file, not something I'm
  taking on faith. (Separately confirmed `schema.sql`'s
  `extracted_events.reference_source` column IS nullable at the database
  level — the design doc's claim about the SQL schema was correct; this
  gap is specifically in the LLM-facing JSON Schema.)
- **The range-valued-guidance mismatch**: confirmed structurally (schema
  fields are single `number | null`, never an array/object) and at the
  data level — read the actual saved extraction JSON for Lilly's guidance
  events and confirmed `observed_value`/`reference_value` are genuinely
  both `null` there, with the real range preserved only in `evidence_span`.
- **Evidence spans are real substrings, not fabricated**: pulled six
  evidence spans from two different saved extraction JSON files and the
  real saved source text files, and confirmed by direct string search
  that every one is an exact substring of the real fetched SEC document
  text (not a paraphrase).
- **The "84/84 tests passing" claim**: I could not run pytest myself
  (no shell access to the developer's machine from this session). Instead
  I read the raw pytest cache artifacts directly:
  `.pytest_cache/v/cache/nodeids` (the full set of collected test IDs)
  contains exactly 84 entries, and `.pytest_cache/v/cache/lastfailed`
  contains `{}` (empty — no recorded failures). This is the actual
  on-disk pytest state, not a claim I'm taking at face value.
- **The "5 commits, 3c58fa2..bc541b9" claim**: read the raw git reflog
  (`.git/logs/HEAD`) directly. It shows `3c58fa2e79...` as the last commit
  of the *prior* (fix) round, followed by exactly five new commits ending
  at `bc541b9c2e...`, which matches the current `refs/heads/master` tip
  exactly. Independently confirmed from git's own append-only log, not
  from a narrative summary.

**Net result: every specific, checkable claim in this report held up.**
Nothing was fabricated, exaggerated, or hallucinated as far as I can tell.
This is a different outcome than the previous two review rounds, where
real bugs were found — worth stating plainly rather than manufacturing
concerns to seem thorough.

What I have NOT done, and would like this review to help with: think
through whether my three *proposed fixes* below are actually correct and
complete, since design/edge-case judgment is exactly where I've been
wrong before in this project.

---

## Part 1 — The dry-run report, in full

```markdown
# Dry Run 001 — Extraction-Runner Bridge, Real EDGAR Data, No Paid API Calls

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

```

---

## Part 2 — `entity_resolution.py`, in full (the module with the confirmed bug)

```python
"""
Entity resolution -- Extraction-Runner Design v2, Section 3.

Normalizes a raw entity name as extracted from a document (e.g. "NVIDIA
Corporation") and matches it against entities.legal_name / entity_aliases
.normalized_alias. A match returns that entity's entity_id. No match logs
an unresolved_entity_mentions row and returns None -- this module never
creates an entity or writes a relationship on a failed match (see
resolve_entity_name's docstring and the tests in
tests/test_entity_resolution.py for the explicit "no-match never creates
an entity" assertion the design doc's Section 6 calls for).

Ambiguous matches (a normalized name found under more than one entity_id --
possible because entity_aliases.normalized_alias is deliberately NOT
globally unique, only unique per-entity, so the resolver can detect this
case rather than the schema silently forbidding or silently resolving it)
are treated the same as no-match: logged, not guessed at. Silently picking
one of several candidates would be a wrong-entity assignment risk with no
way for a human to know it happened.
"""

from __future__ import annotations

import re
import uuid

import psycopg2.extras

# Ordered longest-first so a multi-word suffix ("l l c") is tried before its
# component tokens could be individually mistaken for something else.
_CORPORATE_SUFFIXES = sorted(
    [
        "incorporated", "corporation", "company", "limited",
        "holdings", "holding", "group", "l l c", "l p",
        "inc", "corp", "co", "ltd", "llc", "plc", "lp",
        "n v", "s a", "ag", "se",
    ],
    key=lambda s: -len(s.split(" ")),
)

_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# SEC company_tickers.json / submissions titles routinely carry a trailing
# state/country-of-incorporation tag in slashes (e.g. "MASCO CORP /DE/",
# "PULTEGROUP INC/MI/") that is SEC filing-index formatting, not part of
# the company's actual legal name -- confirmed mechanically (it's always a
# short all-caps code in slashes at the very end), not inferred from memory.
_SEC_STATE_SUFFIX_RE = re.compile(r"\s*/\s*[A-Z]{2,3}\s*/?\s*$")


def strip_sec_filing_index_noise(raw_title: str) -> str:
    """Removes a trailing SEC filing-index artifact like ' /DE/' or '/MI/'
    from a company_tickers.json / submissions API title. Deliberately does
    NOT attempt to fix name-order oddities some SEC titles also have (e.g.
    "HORTON D R INC" for D.R. Horton) -- that would require judgment this
    function can't verify mechanically; see build/seed_data/watchlist_ciks.csv
    and the seeding script's docstring for that known limitation."""
    return _SEC_STATE_SUFFIX_RE.sub("", raw_title).strip()


def normalize_entity_name(name: str) -> str:
    """lowercase, strip punctuation, strip a trailing corporate suffix
    (possibly more than one, e.g. "Eaton Corp plc" -> "eaton") -- per
    design doc Section 3. Suffixes are only stripped from the END of the
    name, never from the middle, so a company whose actual name happens to
    contain a suffix-like word elsewhere is not mangled."""
    s = name.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    tokens = s.split(" ") if s else []

    changed = True
    while changed and tokens:
        changed = False
        for suffix in _CORPORATE_SUFFIXES:
            suffix_tokens = suffix.split(" ")
            n = len(suffix_tokens)
            if n <= len(tokens) and tokens[-n:] == suffix_tokens:
                tokens = tokens[:-n]
                changed = True
                break
    return " ".join(tokens).strip()


def build_resolution_index(conn) -> dict[str, list[str]]:
    """One dict, built fresh per run: normalized_name -> [entity_id, ...].
    A list of length > 1 means an ambiguous normalized name (see module
    docstring). Rebuilt every call rather than cached -- resolution runs
    are infrequent (per extraction batch, not per document) and this
    project's entity count is in the low hundreds at most, so a fresh
    SELECT is cheap and never risks a stale in-memory index after a manual
    resolution adds a new alias."""
    index: dict[str, list[str]] = {}

    with conn.cursor() as cur:
        cur.execute("SELECT entity_id, legal_name FROM entities")
        for entity_id, legal_name in cur.fetchall():
            key = normalize_entity_name(legal_name)
            if key:
                index.setdefault(key, [])
                if entity_id not in index[key]:
                    index[key].append(entity_id)

    with conn.cursor() as cur:
        cur.execute("SELECT entity_id, normalized_alias FROM entity_aliases")
        for entity_id, normalized_alias in cur.fetchall():
            index.setdefault(normalized_alias, [])
            if entity_id not in index[normalized_alias]:
                index[normalized_alias].append(entity_id)

    return index


def log_unresolved_mention(conn, raw_name: str, normalized_name: str, document_id: str,
                            extraction_run_id: str) -> str:
    mention_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO unresolved_entity_mentions
                (mention_id, raw_name, normalized_name, document_id, extraction_run_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (mention_id, raw_name, normalized_name, document_id, extraction_run_id),
        )
    return mention_id


def resolve_entity_name(conn, raw_name: str, document_id: str, extraction_run_id: str,
                         index: dict[str, list[str]] | None = None) -> str | None:
    """Returns an entity_id on an unambiguous match. On no match OR an
    ambiguous match (>1 entity sharing the normalized name), logs to
    unresolved_entity_mentions and returns None -- this function never
    creates a row in `entities` and never writes to `entity_relationships`
    itself; callers must treat None as "cannot write a relationship
    involving this entity right now"."""
    if index is None:
        index = build_resolution_index(conn)

    normalized = normalize_entity_name(raw_name)
    candidates = index.get(normalized, [])

    if len(candidates) == 1:
        return candidates[0]

    log_unresolved_mention(conn, raw_name, normalized, document_id, extraction_run_id)
    return None


def list_unresolved_mentions(conn, limit: int = 100):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT mention_id, raw_name, normalized_name, document_id,
                   extraction_run_id, first_seen_at
            FROM unresolved_entity_mentions
            WHERE status = 'unresolved'
            ORDER BY first_seen_at
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def count_pending_unresolved_mentions(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM unresolved_entity_mentions WHERE status = 'unresolved'")
        return cur.fetchone()[0]

```

---

## Part 3 — The relevant schema fragments

### 3a. `extraction_prompt_v1.md`'s output schema, `surprise` block only

```json
{
  "surprise": {
    "type": ["object", "null"],
    "properties": {
      "surprise_type": {"type": "string"},
      "observed_value": {"type": ["number", "null"]},
      "reference_value": {"type": ["number", "null"]},
      "reference_source": {"type": "string"},
      "reference_timestamp": {"type": ["string", "null"]},
      "unit": {"type": ["string", "null"]},
      "period": {"type": ["string", "null"]},
      "evidence_span": {"type": "string"}
    }
  }
}
```

Note `reference_source` is the only field in this block without a `null`
option, despite the task instructions saying: *"If a field cannot be
determined from the document, use null rather than guessing."*
(`extraction_prompt_version` is currently `"1.1.0"`.)

### 3b. `schema.sql`'s `extracted_events` table (surprise-block columns only)

```sql
CREATE TABLE extracted_events (
    extracted_event_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_version_id        UUID NOT NULL REFERENCES event_versions(event_version_id),

    surprise_type            TEXT NOT NULL,   -- made nullable by migration 002
    observed_value           NUMERIC,
    reference_value          NUMERIC,
    reference_source         TEXT,            -- already nullable at the DB level
    reference_timestamp      TIMESTAMPTZ,
    unit                     TEXT,
    period                   TEXT,
    surprise_raw             NUMERIC,
    surprise_transformed     NUMERIC,
    surprise_transform_registry_id UUID REFERENCES surprise_transform_registry(registry_id),

    extraction_prompt_version TEXT NOT NULL,
    raw_llm_output            JSONB NOT NULL,

    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`observed_value`/`reference_value` are `NUMERIC` — single values, no
array/range type — confirming the range-valued-guidance mismatch is real
at the database level too, not just the JSON Schema.

---

## Part 4 — Two real extraction JSON files showing the issues directly

### 4a. Lilly's guidance events — both range-valued, both left null (`8b89a41e...json`, excerpt)

```json
{
  "event_category": "guidance_revision",
  "catalyst_description": "Lilly raised full-year 2026 revenue guidance to a range of $85.0-$87.0 billion.",
  "surprise": {
    "surprise_type": "revenue_guidance",
    "observed_value": null,
    "reference_value": null,
    "reference_source": "Lilly's own prior full-year 2026 guidance (not itself re-stated with a figure in this specific passage)",
    "reference_timestamp": null,
    "unit": "USD_billions",
    "period": "FY2026",
    "evidence_span": "Increased 2026 full-year revenue guidance to be in the range of $85.0 billion to $87.0 billion"
  }
}
```

Every one of Lilly's own self-references in this same document (issuer
entity name `"Eli Lilly and Company"`) failed to resolve against the
seeded watchlist entity `"ELI LILLY & Co"` — this is the central finding.

---

## Part 5 — Three proposed fixes (NOT yet implemented — for your review)

**5a. Eli Lilly `&`/`and` gap.** Proposed: in `normalize_entity_name()`,
before punctuation-stripping, replace a literal `&` with the word `and`
(so both spellings converge to the same intermediate token stream), and
add `"and"` to `_CORPORATE_SUFFIXES` so a trailing `"...and Company"` /
`"...and Co"` reduces the same way `"...& Co"` already does. Worked
through by hand: `"ELI LILLY & Co"` → `&`→`and` → `"eli lilly and co"` →
strip `co` → `"eli lilly and"` → strip `and` → `"eli lilly"`.
`"Eli Lilly and Company"` → strip `company` → `"eli lilly and"` → strip
`and` → `"eli lilly"`. Both converge. This would also fix the general
class of company (e.g. a hypothetical `"Barnes & Noble"` vs.
`"Barnes and Noble"`) where one form uses `&` and the other spells it out
mid-name, since both would now normalize through the same `"and"` token
instead of one silently dropping it via punctuation-stripping and the
other keeping it as a real word. **What I'm not sure about**: whether
unconditionally stripping a trailing `"and"` is safe for every real
company name, and whether canonicalizing `&`→`and` could ever cause two
genuinely *different* companies to collide (a false match is worse than a
missed one, since resolution failures fail safe but a wrong match
wouldn't). Please stress-test this against real SEC titles you're aware
of, not just the one case observed here.

**5b. `reference_source` schema fix.** Proposed: change
`reference_source`'s type to `{"type": ["string", "null"]}` in
`extraction_prompt_v1.md`'s output schema, matching the other optional
surprise-block fields, and bump `extraction_prompt_version` to `1.2.0`
per the prompt's own versioning rule (any schema change requires a version
bump, tracked per the design doc's promotion-gate requirement). This one
seems low-risk and mechanical — flagging mainly to confirm I'm not missing
a reason it was deliberately `string`-only.

**5c. Range-valued guidance — a real design decision, not a quick fix.**
Three options I can see, no clear favorite:
  - (i) Add explicit `observed_value_low`/`observed_value_high` (and
    `reference_value_low`/`_high`) columns/fields, used only when the
    real disclosure is a range; leave the existing single-value fields
    for point disclosures. Most faithful to what companies actually say,
    but touches the schema, the extraction prompt, and downstream
    `surprise_transform.py`'s calculation logic (a range needs a defined
    convention for what "the observed value" even means downstream —
    midpoint? Nearest bound to the prior guidance? Both bounds compared
    separately?).
  - (ii) Keep single-value fields, adopt a documented convention (e.g.
    "always use the midpoint of a stated range") for `observed_value`,
    and rely on `evidence_span` alone for the true range. Simpler, no
    schema change, but a company that grows sensitivity around a
    guidance floor vs. ceiling loses that distinction entirely, and
    "midpoint" is itself a light form of the "do not infer or estimate"
    the extraction prompt explicitly warns against.
  - (iii) Leave as-is (both null on a range) and treat range-valued
    guidance events as **not yet quantitatively usable** by the
    statistical pipeline, discoverable only via `evidence_span` for now,
    revisited once there's a real decision. Zero implementation cost, but
    the report itself notes this "will recur constantly" once more
    companies are added, so events with no usable magnitude may become
    the majority of `guidance_revision` rows.

  I'd lean toward (i) if the downstream statistical model can define a
  sane convention for a range, but this is exactly the kind of "how do we
  turn messy real disclosure language into a clean number" judgment call
  this project has repeatedly needed a second, adversarial opinion on
  before locking in — which is why it's here rather than something I
  decided unilaterally.

---

## Part 6 — Other things flagged in the dry run worth a quick opinion

- `edgar_ingest_worker.py` has no per-company filing-volume cap by
  design; scoping to 4 companies via `--only-ciks` still pulled in 411
  documents / 235 catalysts (each company's full historical 8-K
  backlog). Correct for a real production poller, but means a repeat of
  this kind of small dry run would benefit from a `--since` or
  `--max-filings-per-company` cutoff. Worth adding now, or defer until
  it's actually annoying?
- A cover page's contentless "see the exhibit" stub genuinely does not
  canonicalize with its own exhibit's real event (their `surprise_type`/
  `observed_value`/`reference_value` differ, and those fields are part of
  the canonicalization fingerprint) — so a typical earnings 8-K now
  produces **two** `earnings_surprise` canonical events per catalyst, not
  one. Is a contentless stub event worth extracting as an event at all,
  versus just noting "no surprise data on this exhibit" and moving on?
