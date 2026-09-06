# Dry Run 002 — Live EDGAR Poll and Extraction Test, Fixes Confirmed Against Real Data

**Date:** 2026-09-06
**Database:** `diffusion_experiment_dryrun` (disposable, recreated fresh for this write-up — see "A note on database provenance" below)
**Extractor:** Claude (a prior session), reading `extraction_prompt_v1.md` (prompt version 1.2.0) and each document's real `raw_content` directly, exactly as `AnthropicExtractionClient` would send them — no Anthropic API call was made. Extraction JSON for all 10 documents is checked in at `build/dry_run_002_extractions/*.json`.

## Purpose

Per `chatgpt_prompt_next_step.md`'s three-part plan (filing-volume safety valve → independent recall check → Dry Run 002), this round exists to confirm — against fresh, real EDGAR filings, not the same 4 documents re-read — that the fixes from Dry Run 001 (Eli Lilly `&`/`and` alias, nullable `reference_source`, range-valued guidance columns, cover-page-stub prompt fix) and from Recall Check 001 (bare-name "Lilly" alias, evidence-span escape-repair, relationship-type synonyms) and the subsequent Round 2 canonicalization fix (actor-signature identity + merge-witness requirement) actually hold up on genuinely new companies and genuinely new filings, not just the original test cases.

## tl;dr

- Real EDGAR poll, scoped to 8 companies (NVIDIA, Broadcom, Vertiv, Hewlett Packard Enterprise, BorgWarner, Albemarle, lululemon, AbbVie) via `--only-ciks`, `--since 2026-09-02`, `--max-new-filings-per-company 2`: **9 catalysts / 16 documents** ingested for real.
- **10 documents, across 6 of the 8 companies**, were manually extracted (Claude reading the real filing text directly, same discipline as Dry Run 001) — HPE and Vertiv's 6 documents were ingested but deliberately left unextracted to keep this round reviewable, the same pattern Dry Run 001 established.
- Full pipeline ran for real against these saved extractions: `extract_document` → `process_catalyst` (entity resolution, canonicalization, relationship writes, candidate generation).
- **No issuer failed to resolve against its own seeded entity this round** — a direct, positive confirmation of the Dry Run 001 / Recall Check 001 alias fixes on 6 companies that were never part of those original fixes. Only two out-of-watchlist acquisition targets (Apogee Therapeutics, Hugging Face) came back unresolved, exactly as expected.
- **The Round 2 canonicalization fix (merge-witness requirement) is confirmed working on live data**, in both directions: Broadcom's primary and exhibit both describing the same $0.65/share dividend correctly *merged* into one canonical event (matching quantified surprise = valid merge witness), while Albemarle's primary and exhibit both describing the same CEO-succession news correctly stayed *unmerged* (no quantified surprise, and `other_material_event` isn't an identity-defining category) — real proof the fix behaves the way its own code comment says it should, not just in the unit tests.
- **The relationship-deferral observability column (migration 005) is confirmed working on live data**: 3 real `processing_issues` log entries were written (2 for AbbVie→Apogee, 1 for NVIDIA→Hugging Face) recording exactly why those relationships were deferred.
- **A new, real validation-drop finding** from an independent second read ("Reader B") of Broadcom's earnings exhibit: all 6 of Reader B's events were silently dropped by `validate_extraction_output`, because every one of them reused an issuer evidence span with a double-escaped HTML entity (`&amp;#58;` instead of the document's real `&#58;`) — a different, more damaging class of span-fabrication bug than the one Recall Check 001 found and fixed, and not caught by the existing `_repair_escaped_punctuation` repair. Full breakdown below.
- 0 relationships written, 0 candidates generated this round — correctly, since the only relationships extracted (AbbVie→Apogee, NVIDIA→Hugging Face) each had one out-of-watchlist endpoint.

## A note on database provenance

The disposable `diffusion_experiment_dryrun` database this round's pipeline results live in did **not** survive between sessions (a machine restart dropped it — Postgres itself was still running, but the database was gone, confirmed by connecting and finding only `postgres`/`template0`/`template1`). Rather than fabricate or re-derive the original session's pipeline-level numbers from memory, this report's Steps on entity resolution, canonicalization, relationships, and candidates reflect a **same-day re-derivation**: a fresh database was created, `schema.sql` and all 5 migrations applied, all 108 entities re-seeded, and the real EDGAR poll re-run today (2026-09-06) with the identical scoping (`--only-ciks`, `--since 2026-09-02`, `--max-new-filings-per-company 2`) the original round used. The poll returned the same 9 catalysts across the same 8 companies. The 10 saved extraction JSON files are **unchanged** from the original round — only their `document_id` field was updated to match the freshly-assigned UUIDs from this fresh ingestion (`FileBackedExtractionClient` requires an exact `document_id` match, and `validate_extraction_output` hard-rejects any mismatch — see `extraction_runner.py` line 375), then replayed through the real pipeline exactly as before. The underlying filings and extractions are the same; the numbers below are freshly, independently reproduced, not recalled.

## 1. Companies polled, catalysts found

| Company | CIK | Catalysts | Documents ingested | Documents extracted | Accession(s) |
|---|---|---|---|---|---|
| NVIDIA Corporation | 0001045810 | 1 | 1 | 1 | `0001045810-26-000078` |
| Broadcom Inc. | 0001730168 | 1 | 2 | 2 | `0001730168-26-000076` |
| Vertiv Holdings Co | 0001674101 | 2 | 4 | 0 | `0001628280-26-059961`, `0001193125-26-379306` |
| Hewlett Packard Enterprise Co | 0001645590 | 1 | 2 | 0 | `0001645590-26-000078` |
| BorgWarner Inc. | 0000908255 | 1 | 1 | 1 | `0001104659-26-105519` |
| Albemarle Corp | 0000915913 | 1 | 2 | 2 | `0001140361-26-035623` |
| lululemon athletica inc. | 0001397187 | 1 | 2 | 2 | `0001397187-26-000126` |
| AbbVie Inc. | 0001551152 | 1 | 2 | 2 | `0001104659-26-104940` |
| **Total** | | **9** | **16** | **10** | |

Vertiv contributed 2 catalysts (2 separate real 8-Ks inside the poll window) to the other 7 companies' 1 each. HPE and Vertiv were ingested for real but not manually extracted this round — a deliberate scope choice (Dry Run 001's own "cap this at something small and reviewable" instruction), not a failure: their `extraction_runs` rows correctly show `status='failed'` with a `FileNotFoundError` from `FileBackedExtractionClient`, exactly the behavior that client is designed to produce when no saved JSON exists for a document.

## 2. Per-document extraction: source quote next to saved JSON

Every JSON file is saved at `build/dry_run_002_extractions/<document_id>.json` (filenames are the original session's document IDs; see the provenance note above for why they don't match this session's freshly re-ingested IDs).

---

### 2.1 AbbVie primary — cover page (`131cdf61-2af2-46b7-bb6a-50856f1e4523`)

**Source:** https://www.sec.gov/Archives/edgar/data/1551152/000110465926104940/tm2624674d1_8k.htm

> AbbVie Inc. (the "Company") issued a press release announcing the completion of its acquisition of Apogee Therapeutics, Inc.

**Extraction:** one `acquisition_or_divestiture` event, AbbVie (issuer) / Apogee Therapeutics, Inc. (target), `acquirer_target` relationship, `surprise: null` — the cover page states the fact but no deal figures.

---

### 2.2 AbbVie EX-99.1 — acquisition completion + guidance reaffirmation (`e8ce6aad-20e8-4ba7-a465-0c63e845f8b9`)

**Source:** https://www.sec.gov/Archives/edgar/data/1551152/000110465926104940/tm2624674d1_ex99-1.htm

> AbbVie (NYSE: ABBV) today announced that it has completed its acquisition of Apogee Therapeutics, Inc. (NASDAQ: APGE). Under the terms of the agreement, Apogee shareholders received $135.11 per share in cash, for a total equity value of approximately $10.9 billion.

**Extraction:** 3 events — the same `acquisition_or_divestiture` relationship, now with a real `acquisition_deal_value` surprise (`observed_value=10.9`, `USD_billions`), plus two `guidance_revision` events reaffirming full-year and Q3 2026 adjusted diluted EPS ranges ($13.87–$14.07 and $3.84–$3.88), each correctly using the range-valued `observed_value_low/high` + `reference_value_low/high` columns from the Dry Run 001 fix, with `reference_source` populated (AbbVie's own prior guidance) rather than left null.

---

### 2.3 Albemarle EX-99.1 — CEO succession press release (`065d1eab-45bd-4e41-bab8-196ce98367bd`)

**Source:** https://www.sec.gov/Archives/edgar/data/915913/000114036126035623/ef20081522_ex99-1.htm

> Albemarle Corporation (NYSE: ALB) ... today announced that Ragnar "Rag" Udd has been appointed President and Chief Executive Officer, effective February 1, 2027.

**Extraction:** one `other_material_event`, issuer = Albemarle Corporation, `surprise: null`.

---

### 2.4 Albemarle primary — Item 5.02 cover page (`7b992058-0c54-4e8b-9400-21e224db4091`)

**Source:** https://www.sec.gov/Archives/edgar/data/915913/000114036126035623/ef20081522_8k.htm

> On September 2, 2026, the Board of Directors of the Company ... approved a leadership succession plan for the Company by appointing Ragnar Udd to succeed J. Kent Masters, Jr. as the Company's President and Chief Executive Officer.

**Extraction:** one `other_material_event`, same underlying real-world fact as §2.3, `surprise: null`. **These two did NOT canonicalize into one event** (§4) — a real, checked confirmation of the Round 2 fix's merge-witness requirement, not a bug.

---

### 2.5 BorgWarner primary — Item 7.01 capacity reiteration (`8e006bea-d6bd-4b26-b599-24b0ac2c45fd`)

**Source:** https://www.sec.gov/Archives/edgar/data/908255/000110465926105519/tm2624838d1_8k.htm

> In response to questions it has received, BorgWarner Inc. ... reiterates its prior statements that the Company continues to expect turbine generator production to begin in Hendersonville, North Carolina in 2027, with an initial 2 GW of installed capacity.

**Extraction:** one `capacity_change` event, `observed_value=2`, `unit=GW`. A second quantified fact in the same passage ("sales expected to exceed $300 million in the first year") was deliberately not split into its own event — logged to `unsupported_claims_noted` as the same capacity plan being reiterated, not a distinct proposition. No exhibit exists for this filing (single-document catalyst).

---

### 2.6 Broadcom primary — dividend declaration cover page (`33572b0a-814b-419e-8b99-37af2ba2ac8e`)

**Source:** https://www.sec.gov/Archives/edgar/data/1730168/000173016826000076/avgo-20260902.htm

> the Company announced that the Board of Directors has declared a quarterly cash dividend on the Company's common stock of $0.65 per share.

**Extraction:** one `buyback_or_capital_return` event, `quarterly_dividend_per_share`, `observed_value=0.65`.

---

### 2.7 Broadcom EX-99.1 — Q3 FY2026 earnings release (`6144062a-286b-4a5c-9e6c-379693a54da3`)

**Source:** https://www.sec.gov/Archives/edgar/data/1730168/000173016826000076/avgo-08022026x8kxex99.htm

> Revenue of $29.6 billion for the third quarter, up 86 percent from the prior year period ... Q3 AI semiconductor revenue of $16.7 billion grew 221% year-over-year ... Fourth quarter fiscal year 2026 revenue guidance of approximately $34.8 billion.

**Extraction:** 8 events — revenue, non-GAAP diluted EPS, free cash flow (`earnings_surprise`); the same $0.65/share dividend as §2.6 (`buyback_or_capital_return` — this one **did** canonicalize with §2.6's, see §4); Q4 revenue guidance and Q4 non-GAAP operating-margin guidance (`guidance_revision`); AI semiconductor revenue, both actual and Q4 guidance (`other_material_event` / `guidance_revision`). This is the document independently re-read as "Reader B" — see §6.

---

### 2.8 lululemon EX-99.1 — Q2 FY2026 earnings + guidance (`b201e944-0d99-4bb4-a178-28f660dc5bab`)

**Source:** https://www.sec.gov/Archives/edgar/data/1397187/000139718726000126/lulu-20260802xex991.htm

> Net revenue decreased 4% to $2.4 billion ... The Company repurchased 2.7 million of its shares for a cost of $330.0 million ... For 2026, the Company now expects net revenue to be in the range of $10.350 billion to $10.500 billion.

**Extraction:** 8 events — revenue and diluted EPS (`earnings_surprise`), share buyback (`buyback_or_capital_return`), net-new-store count (`capacity_change`), and 4 separate `guidance_revision` events (Q3 revenue range, Q3 EPS range, and — correctly marked `explicit_correction: true` — the raised full-year revenue and EPS ranges). All range-valued guidance used the low/high columns correctly.

---

### 2.9 lululemon primary — cover page, zero events (`eccf4ace-2b1e-4ea1-8819-e7f8636ee2e7`)

**Source:** https://www.sec.gov/Archives/edgar/data/1397187/000139718726000126/lulu-20260903.htm

> Item 2.02. ... On September 3, 2026, lululemon athletica inc. (the "Company") issued a press release announcing its financial results ... A copy of the Company's press release is attached hereto as Exhibit 99.1 and is incorporated herein by reference.

**Extraction:** `events: []` — a real, live confirmation of the Dry Run 001 cover-page-stub prompt fix. A classic "see the exhibit" cover page now correctly produces an empty events array rather than a contentless stub event, on a document that fix was never tested against before.

---

### 2.10 NVIDIA primary — Hugging Face acquisition agreement (`c90adc6d-a71a-4507-8c6a-1a8316f392f0`)

**Source:** https://www.sec.gov/Archives/edgar/data/1045810/000104581026000078/nvda-20260902.htm

> On September 2, 2026, NVIDIA Corporation ("NVIDIA") entered into a definitive agreement to acquire Hugging Face, Inc. ("Hugging Face"). The transaction includes an approximately $11.9 billion purchase price payable to Hugging Face stockholders.

**Extraction:** one `acquisition_or_divestiture` event, NVIDIA (issuer) / Hugging Face, Inc. (target), `acquirer_target` relationship, `acquisition_purchase_price` surprise (`observed_value=11.9`). Single-document catalyst — no exhibit filed with this 8-K.

## 3. Entity resolution — the real numbers, re-derived

Real pipeline output (`extraction_runner.py`'s own logged summary): **28 entity mention(s) matched / 6 unresolved.**

| Raw name | Normalized | Occurrences | Why |
|---|---|---|---|
| `Apogee Therapeutics, Inc.` | `apogee therapeutics` | 4 | Correctly outside the 108-company watchlist. |
| `Hugging Face, Inc.` | `hugging face` | 2 | Correctly outside the 108-company watchlist. |

**The headline result: every issuer resolved against its own seeded entity this round — zero self-reference failures.** AbbVie, Albemarle, BorgWarner, Broadcom, lululemon, and NVIDIA all correctly matched, across 6 companies that were never part of the original Lilly `&`/`and` or bare-"Lilly" alias fixes. This is real, positive evidence that `normalize_entity_name` and `CURATED_ALIASES` generalize beyond the two specific companies those fixes were written for, not just that the two known bugs stayed fixed on the same two companies.

Only the two out-of-watchlist acquisition targets — genuinely private/newly-acquired companies not on the 108-company list — came back unresolved, exactly the expected, correct outcome (same pattern as Dry Run 001's Orna/Ajax/CoreWeave).

## 4. Canonicalization — the Round 2 merge-witness fix confirmed both ways

| Catalyst | Canonical events | Notable |
|---|---|---|
| AbbVie | 4 | The primary's contentless acquisition stub and the exhibit's fully-quantified acquisition event did **not** merge — different fingerprints (`surprise_type`/`observed_value` differ: `null` vs. `acquisition_deal_value=10.9`), same non-merge mechanism as Dry Run 001's CAT cover-page finding. |
| Albemarle | 2 | Primary and exhibit both describe the identical CEO-succession fact, same resolved issuer, but did **not** merge: `surprise: null` on both (no quantified surprise) and `other_material_event` is not in `_IDENTITY_DEFINING_ACTOR_ROLES_BY_CATEGORY` — so `_has_merge_witness()` correctly returns `False` and blocks the merge, exactly as the Round 2 fix's code comment describes ("two independent 8-K documents describing one real event will NOT auto-merge without a witness"). Two canonical events for one real underlying story, by design. |
| BorgWarner | 1 | — |
| Broadcom | **8** (from 9 raw events) | The primary's brief dividend-declared event and the exhibit's identically-valued dividend event (`quarterly_dividend_per_share`, `observed_value=0.65`, same period/unit) **did** merge into one canonical event with 2 `event_document_links` — a real, live confirmation that a matching quantified surprise value is exactly the merge witness the fix requires. |
| lululemon | 8 | Exhibit's 8 events, no merges needed (primary contributed 0). |
| NVIDIA | 1 | — |

This is the cleanest possible live test of the Round 2 fix: one real merge (matching numbers, correctly witnessed) and two real non-merges (no witness, by two different routes — differing fingerprint vs. missing merge-witness category) on the same day, across four different companies.

## 5. Relationships — 0 written, but the deferral log now shows why

**0 relationships written this round** — both extracted relationships (AbbVie→Apogee Therapeutics, NVIDIA→Hugging Face) had a resolved issuer but an unresolved, correctly-out-of-watchlist target, so `process_catalyst` correctly declined to write them (writing requires both sides resolved).

**Migration 005's `catalyst_processing_runs.processing_issues` column — confirmed working on live data**, with 3 real entries:

```json
{"reason": "relationship_endpoint_unresolved", "entity_a": "AbbVie Inc.", "entity_b": "Apogee Therapeutics, Inc.", "document_id": "2bf7fc92-...", "unresolved_endpoints": ["entity_b"]}
{"reason": "relationship_endpoint_unresolved", "entity_a": "AbbVie", "entity_b": "Apogee Therapeutics, Inc.", "document_id": "459d6e23-...", "unresolved_endpoints": ["entity_b"]}
{"reason": "relationship_endpoint_unresolved", "entity_a": "NVIDIA Corporation", "entity_b": "Hugging Face, Inc.", "document_id": "49b092a2-...", "unresolved_endpoints": ["entity_b"]}
```

Before migration 005, a relationship that failed to write left no direct trace beyond the bare unresolved-mention row; now there's an explicit, per-relationship record of exactly which assertion was deferred and why — this is the first time that column has been exercised against a real, non-test filing.

## 6. Candidates — 0/0, correctly vacuous

`candidate_signals` has 0 rows this round. With 0 relationships written, there is no counterparty relationship for `generate_candidates_for_event_version` to build a candidate from — the same mechanism Dry Run 001 documented (candidates require a *written* relationship, and none of this round's watchlist companies had one this time). Not a bug: an empty, honestly-reported result, not a hidden failure.

## 7. Independent spot-check: Reader A vs. Reader B on Broadcom's earnings release

To sanity-check the main extraction against an independent second read (same discipline as Recall Check 001), Broadcom's EX-99.1 (§2.7) was also read independently and saved as `build/dry_run_002_extractions/spot_check_reader_b.json` ("Reader B"; the original §2.7 extraction is "Reader A").

**What Reader B got right:** the same headline figures as Reader A — $29.6B revenue, $16.7B AI semiconductor revenue, $34.8B Q4 revenue guidance, $21.7B AI semiconductor revenue guidance, $0.65/share dividend — plus one fact Reader A didn't separately extract: the **prior** $0.65/share dividend actually paid June 30, 2026 (a real, distinct historical fact, correctly given its own event with `reference_timestamp`). Reader B's `unsupported_claims_noted` also gives an explicit, reasoned account of why GAAP EPS, operating income, and free cash flow were bundled into the revenue disclosure rather than split into separate events — a real, judgment-call disagreement with Reader A (who did split those out), not an oversight.

**The real finding — all 6 of Reader B's events were dropped by validation, not by any judgment call:**

Every one of Reader B's 6 events used the identical issuer evidence span `"Broadcom Inc. (Nasdaq&amp;#58; AVGO)"`. Running that JSON through the real `validate_extraction_output()` against the actual saved `raw_content` for this document confirms it is **not** an exact substring — the real document contains `&#58;` (single-escaped), while Reader B's span has `&amp;#58;` (double-escaped). Reader A's span for the same sentence, `"Broadcom Inc. (Nasdaq&#58; AVGO), a global technology leader"`, **is** an exact substring and passes. Because `validate_extraction_output` drops a whole event when no entity survives span verification, and Reader B reused this one malformed span as the issuer entity on all 6 events, **the entire independent read validates to zero events** — even though every underlying number was correct.

This is a different, and more damaging, class of bug than the one Recall Check 001 found and fixed: that round's spurious-backslash spans (`\<`, `\:`, `\/`) killed 3 of 62 events and are now repaired by `_repair_escaped_punctuation()`; that repair only targets a backslash immediately before `<`, `>`, `:`, or `/` and does **not** recognize a doubled HTML entity escape (`&amp;#58;`), so it would not have rescued this case even if applied. Because Reader B reused one templated span across every event instead of re-deriving it per event, a single formatting mistake silently erased 100% of an otherwise-correct independent extraction — worth fixing (either a broader span-repair pass, or flagging "identical evidence_span reused across N events" as a validation warning) before this class of extraction (one entity mention copy-pasted across many events in one document) is trusted at scale.

## 8. Observations

1. **The Round 2 canonicalization fix generalizes correctly, in both directions, on entirely new data** — see §4. This is the single most important confirmation from this round.
2. **The alias/normalization fixes generalize past the two companies they were written for** — see §3. Six new companies, zero issuer self-resolution failures.
3. **The relationship-deferral log (migration 005) works exactly as designed on a real filing** — see §5.
4. **A new span-fabrication bug class, worse than the one already fixed** — see §7. The existing repair is narrowly scoped (by design, per its own docstring) and correctly does not attempt to "rescue" this different failure mode; a real gap remains.
5. **The cover-page-stub prompt fix holds on a document it was never tested against** — lululemon's primary (§2.9) produced `events: []`, not a contentless stub, on live data.
6. **HPE and Vertiv were ingested but not extracted this round** — a deliberate scope choice (matches Dry Run 001's own precedent), not a bug; their `extraction_runs` rows correctly show `status='failed'` from `FileBackedExtractionClient`'s intentional `FileNotFoundError` when no saved JSON exists.
7. **The `document_id` provenance issue itself is worth flagging as a process note.** `FileBackedExtractionClient`/`validate_extraction_output` correctly hard-require an exact `document_id` match against the document actually sent — the right behavior for guarding against attributing output to the wrong document — but it means saved extraction JSON is tied to one specific database's UUIDs and isn't automatically replayable against a freshly recreated database without an explicit remap step, as done here. Worth knowing before assuming a saved `dry_run_*_extractions/` directory is trivially "replay anywhere."

## 9. What was NOT touched

- The main `diffusion_experiment` test database and pytest suite were not run or verified this round — out of scope for this specific re-derivation, which was scoped to the disposable dry-run database only.
- The full 108-company watchlist — only 8 companies' `watchlist_membership` rows were ever queried via `--only-ciks`; nothing was added, removed, or reordered.
- No `ANTHROPIC_API_KEY` was read or used. `llm_client.FileBackedExtractionClient` was the only client instantiated.
- The 10 saved extraction JSON files were not edited beyond the required `document_id` remap (see provenance note) — same facts, same evidence spans, same judgment calls as originally produced.

## Artifacts

- `build/dry_run_002_extractions/*.json` — the 10 extraction outputs (original document IDs as filenames) plus `spot_check_reader_b.json`, the independent Reader B read used in §7.
- `build/edgar_ingest_worker.py`'s `EDGAR_USER_AGENT` environment-variable fix — the checked-in placeholder User-Agent is deliberately left as a placeholder; a real contact email is read from `EDGAR_USER_AGENT` at runtime instead, so it never lands in git history. Required to run this round's live poll at all (`EdgarClient.__init__` refuses to start against the placeholder).
