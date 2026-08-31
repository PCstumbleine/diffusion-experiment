# Extraction-Runner Bridge — Design v2

**Status:** design-reviewed, ready for implementation. v1 was reviewed by
ChatGPT; every substantive claim in that review was independently
re-verified against the actual schema and code below before being
accepted — none were fabricated or overstated, all thirteen points
checked out. This version incorporates the fixes. `EXTRACTION_RUNNER_DESIGN_V1.md`
is kept for provenance; this is the document to implement from.

## What changed from v1, and how each was verified

| # | v1 gap | Verified how | Resolution in v2 |
|---|---|---|---|
| 1 | Relationship writes: append-only vs. upsert undecided | `entity_relationships` has no `UNIQUE` on `(entity_id_a, entity_id_b, relationship_type)` — only the `entity_id_a <> entity_id_b` check and plain indexes (confirmed by reading `schema.sql`) | **Append-only.** See §4. |
| 2 | Alias storage undecided | No alias table or column exists on `entities` (confirmed) | **New `entity_aliases` table.** See §1. |
| 3 | `inferred_structured` eligibility | N/A — judgment call, not a fact to verify | **Ineligible in v1**, recorded not dropped. See §5. |
| 4 | `decision_at` never defined | `event_versions.decision_at` is a bare nullable `TIMESTAMPTZ` with nothing in schema or code that ever sets it (confirmed by reading the full `event_versions` definition and grepping the codebase) | **Defined explicitly.** See §5a. |
| 5 | Primary+exhibit documents could double-extract the same event | Confirmed directly in `ingest_filing()`: one catalyst gets a `primary` document plus N `exhibit` documents via `catalyst_documents` (`document_role IN ('primary','exhibit')`) — the same guidance figure routinely appears in both an 8-K cover page and its EX-99.1, and `schema.sql`'s own comments cite a real EDGAR filing (UDR Inc., 2026-04-29) with duplicate exhibits | **Catalyst-level canonicalization stage added.** See §2a. |
| 6 | `extracted_events.surprise_type TEXT NOT NULL` conflicts with the prompt's own `"surprise": null` case | Confirmed: `extracted_events` schema has `surprise_type TEXT NOT NULL`; `extraction_prompt_v1.md`'s JSON schema has `"surprise": {"type": ["object","null"], ...}` — a valid `acquisition_or_divestiture` or `capacity_change` extraction with no numeric surprise cannot be inserted as drafted. `surprise_transform.py` never assumes `surprise_type` is set at the DB level (confirmed by reading it), so relaxing the constraint breaks nothing downstream | **Schema fix: make the whole surprise block nullable on `extracted_events`.** See §2b. |
| 7 | v1 conflated prompt-reprocessing with `event_versions` | Confirmed: `event_versions` has no field referencing `extraction_prompt_version` anywhere, and its own fields (`event_effective_at`, `decision_at`, `first_executable_at`, `superseded_by`) are all about *when the economic fact changed*, not when the extractor re-ran — this was a genuine design-quality error in v1, not a code fact to check | **Separated.** Prompt/model reprocessing lives entirely in `extraction_runs` (§2c); `event_versions` bumps only on `explicit_correction=true` from the source. |
| 8 | `event_document_links` never written | Confirmed: v1 §2 lists `extracted_events`/`canonical_events`/`event_versions`/`event_entities` as the write set and omits this table entirely, despite the schema having it specifically for multi-document evidence | **Added to the write set.** See §2a. |
| 9 | `extraction_runs` (proposed in v1) tracked prompt version only | N/A — v1's own proposed table, not yet real; enhancement is additive | **Add extractor model/version, attempt count, atomicity.** See §2c. |
| 10 | Instrument/ticker seeding never mentioned | Confirmed: `arm_entries` has a trigger (`check_arm_entry_instrument`) that **requires** `instrument_id` for every arm except E, plus a second trigger requiring the instrument's `entity_id` match the candidate's `entity_id` — a candidate with no seeded instrument is a dead end at the very next pipeline stage | **Seed instruments/tickers alongside entities**, scoped as adjacent-but-separate from the bridge's critical path (candidate_signals itself never references instruments). See §1. |
| 11 | `relationship_type` has no controlled vocabulary or direction convention | Confirmed: unlike `source_authority`, `relationship_evidence`, and `shock_transmission_evidence` (all `CHECK (... IN (...))`), `relationship_type` is bare `TEXT NOT NULL` with only a comment | **Define a closed vocabulary + directionality convention before implementation.** See §4a. |
| 12 | Unresolved-name monitoring unspecified | N/A — extension of v1's own proposal | **Lightweight per-run summary + backfill rule on manual resolution.** See §3. |
| 13 | Watchlist ≠ candidate-entity-universe never decided | Logical consequence of v1's own resolver design (matches only against seeded entities) — not a code fact, a policy gap | **Decided: filer universe and candidate-entity universe are separate**, with a rule that prevents outcome-driven expansion. See §3a. |

One v1 claim to flag as *not* purely a code fact: whether `entities` should
gain a `UNIQUE (cik) WHERE cik IS NOT NULL` constraint. Confirmed the gap is
real (only a non-unique index exists today) — adopted, see §1.

## Scope for v1 implementation (unchanged from v1's framing, adjusted for the above)

**In scope:** CIK lookup + entity/alias/instrument seeding for the 108-company
watchlist; the extraction runner with catalyst-level canonicalization;
entity resolution; append-only relationship writes; candidate generation
with an explicit eligibility policy; idempotency via `extraction_runs`.

**Still explicitly out of scope, not silently dropped:**
- The per-sector coverage sanity check and pre-drawn backup list
  (`CANDIDATE_UNIVERSE_V1.md` items 2–3) — a research-design decision
  requiring a threshold committed *before* looking at results, not a
  mechanical task this bridge should absorb.
- The infrastructure fork (hosting, LLM budget, monitoring for silent
  failure) — still open, still a separate decision.
- Fuzzy/ML entity matching, calibrated relationship probabilities,
  transmission-effect modeling — same reasons as v1.

## 1. Entity, alias, and instrument seeding

- **CIK lookup** for all 108 tickers against SEC's own
  `company_tickers.json` / EDGAR full-text search (not inferred from
  memory) — this was flagged as not-yet-done in `CANDIDATE_UNIVERSE_V1.md`
  itself and is a mechanical prerequisite, folded into this same
  implementation step since it requires no judgment call. The per-sector
  *coverage* check remains separately out of scope (see above) — looking
  up a CIK is not the same as validating that company's filing volume.
- Insert one row per company into `entities` (`legal_name`, `cik`,
  `entity_status = 'active'`).
- **Schema addition:** `UNIQUE (cik) WHERE cik IS NOT NULL` on `entities`
  — the current schema only indexes `cik`, it doesn't prevent two entity
  rows from claiming the same issuer.
- **Schema addition:** `entity_aliases` table:
  ```
  entity_aliases(alias_id, entity_id, alias_text, normalized_alias,
                 alias_source, created_at)
  ```
  chosen over a static config file so aliases are auditable, addable
  without a code deploy, and the resolver can detect ambiguous aliases.
- **Watchlist wiring:** do *not* hand-copy generated `entity_id` UUIDs into
  `edgar_ingest_worker.py`'s hardcoded `WATCHLIST` list as v1 assumed.
  Instead have the worker resolve `cik -> entity_id` from the `entities`
  table at startup (or via a tiny watchlist-membership table). Generated
  database IDs should never need manual sync into source code.
- **Instrument/ticker seeding**, done at the same time as entity seeding
  (same 108-company scope, proportionate effort): for each entity, seed one
  `instruments` row (common equity) and one current `instrument_identifiers`
  row (`identifier_type='ticker'`). This isn't required for `candidate_signals`
  itself, but `arm_entries`'s own trigger makes it a hard requirement one
  step later — better seeded once, now, than discovered as a second missing
  bridge after this one ships.

## 2. Extraction runner

### 2a. Catalyst-level canonicalization (new in v2)

Extraction runs per-document (as v1 had it), but **canonical events are not
written until every document belonging to a catalyst has reached a
terminal extraction state** (success, or a terminal failure recorded in
`extraction_runs`). At that point, a deterministic within-catalyst merge
step compares extracted events across that catalyst's documents on a
fingerprint of `(event_category, resolved entity/role set, surprise_type +
period, observed/reference values when present)`. Matches collapse into
one `canonical_events` row; each contributing document gets an
`event_document_links` row (`canonical_event_id`, `document_id`,
`relationship_type`, and the verified `evidence_span_start`/`_end` — see
§2b) rather than one row per document producing its own duplicate event.
Ambiguous (non-matching but suspicious) cases stay separate rather than
being force-merged — the point is that duplicate handling is explicit, not
that every duplicate is caught.

### 2b. Per-document extraction

- Calls the LLM with `extraction_prompt_v1.md`'s system + task prompt
  against `raw_documents.raw_content`.
- **Validates before writing anything:** output parses and matches the
  documented JSON schema; every `evidence_span` is an exact substring of
  `raw_content` (dropped + logged if not — never kept as a paraphrase);
  `extraction_prompt_version` in the output matches what was requested.
- **Schema fix:** `extracted_events.surprise_type` and the rest of the
  surprise block (`observed_value`, `reference_value`, `reference_source`,
  `reference_timestamp`, `unit`, `period`) become nullable, matching the
  prompt's own `"surprise": null` case for non-numeric events
  (acquisitions, capacity announcements, etc.). The deterministic
  transform in `surprise_transform.py` simply doesn't run when
  `surprise_type IS NULL` — confirmed safe, since that module never
  assumes the column is set.
- Writes `extracted_events` (full `raw_llm_output` retained regardless of
  whether a surprise block exists — the audit trail doesn't depend on
  the numeric case), pending the catalyst-level merge in §2a for
  `canonical_events` / `event_versions` / `event_entities` /
  `event_document_links`.

### 2c. `extraction_runs` (idempotency + provenance)

```
extraction_runs(document_id, extraction_prompt_version, extractor_model_id,
                extractor_model_version, status, attempt_count,
                raw_llm_output, error, started_at, completed_at)
```

Idempotency keys on **document + prompt version + model configuration**,
not prompt version alone — silently swapping the underlying model under an
unchanged prompt version is a different extractor and must be tracked as
one. Reprocessing (a prompt-version bump, or a model change) creates new
`extraction_runs` rows and new downstream event data; it never overwrites
old rows, consistent with `extraction_prompt_v1.md`'s own versioning
requirement. **This is fully separate from `event_versions`**, which bumps
only when the source itself issues `explicit_correction=true` — an
extraction re-run over an unchanged document is not a new economic event
state, it's a new observation of the same one.

All writes for one successful catalyst-level processing batch
(canonicalization, entity resolution, relationship writes, candidate
generation) commit atomically — partial failure leaves nothing.

## 3. Entity resolution

- Normalize (lowercase, strip legal suffixes, strip punctuation) and match
  against `entities.legal_name` and `entity_aliases.normalized_alias`.
- **Match found:** use that `entity_id`.
- **No match:** do not create an entity or write the relationship. Log to
  `unresolved_entity_mentions`:
  ```
  unresolved_entity_mentions(mention_id, raw_name, normalized_name,
    document_id, extraction_run_id, first_seen_at, status,
    resolved_entity_id, resolved_at)
  ```
- **Backfill rule on manual resolution:** when an unresolved mention is
  later resolved by a human, the relationship it belongs to is written
  *then*, with `system_observed_at` set to the actual resolution
  timestamp — never backdated to the original filing/extraction time. A
  relationship the system didn't yet know how to attribute wasn't
  actually usable at the earlier time; backdating it would inject
  hindsight into candidate generation, exactly the kind of lookahead bias
  this project has been careful to avoid everywhere else (the extraction
  prompt's own "never use knowledge from training about what happened
  after this document's publication date" rule is the same principle
  applied one layer up).
- **Monitoring, kept deliberately lightweight for a hobby-scale project:**
  every runner invocation logs a one-line summary — documents processed,
  events extracted, entity mentions seen/resolved/unresolved, eligible
  candidates produced, failed extractions, running total of pending
  unresolved mentions. No dashboard needed; a human reviews the unresolved
  queue periodically (weekly is enough at this scale).

### 3a. Watchlist universe vs. candidate-entity universe (decided)

The resolver as designed only matches against the 108 seeded companies,
which means a named counterparty outside that set produces an unresolved
log entry and no candidate — the relationship graph would never actually
extend past the pre-selected watchlist on its own. Two policies were
possible: keep the candidate universe frozen to exactly the 108 filers, or
let the entity master grow independently of the filer/polling list. The
second is the right call for the actual diffusion hypothesis — a filing
from a watched company can legitimately name an economically relevant
counterparty that isn't itself a filer we poll, and that counterparty is
exactly the kind of second-order candidate this project is built to catch.

**Decision:** the polling watchlist (who gets watched for new filings) and
the candidate-entity universe (who can appear as a candidate) are
separate. A named counterparty outside the 108 can be manually resolved
into `entities` without becoming a polled filer itself. To keep this
mechanical rather than outcome-driven — the same discipline
`CANDIDATE_UNIVERSE_V1.md` already applies to backup-list swaps — the rule
is fixed now, before any such resolution happens: *any explicitly- or
quantifiably-named, U.S.-tradable counterparty may be manually resolved
into the entity master, without regard to its subsequent price
performance, and becomes usable only from its actual resolution timestamp
onward* (per the backfill rule in §3 — no separate "known since" field is
needed; the relationship row's own `system_observed_at` already carries
this).

## 4. Relationship writes (append-only, decided)

Each `entity_relationships` row is an **evidence assertion**, not a
deduplicated relationship master — it carries source-document provenance,
an evidence tier, a raw model score, public-availability and
system-observation timestamps, and supersession state. If a supplier
relationship is disclosed in January and again in July, those are two
independently observable occurrences; upserting on
`(entity_id_a, entity_id_b, relationship_type)` would destroy exactly the
point-in-time history this schema was built to preserve, and nothing in
the schema forces deduplication (confirmed — no such `UNIQUE` constraint
exists).

**v1 for real:** insert one row per validated disclosure occurrence,
carrying a provenance FK to the `extraction_runs` row that produced it (so
idempotency is enforceable at the relationship level too, not just at the
document level). Candidate generation (§5) asks whether *at least one
qualifying relationship-evidence row existed as of decision time* —
multiple qualifying rows link to the same candidate via
`candidate_supporting_relationships`, and the candidate itself stays
unique because of `candidate_signals`'s own
`UNIQUE (event_version_id, entity_id)` constraint. If this becomes
unwieldy at much larger scale, a later split into `relationships` +
`relationship_evidence` tables is available — not needed for v1.

`shock_transmission_evidence` is set to the conservative default
`'new_or_unobserved'` per `extraction_prompt_v1.md`'s own "what happens
after this prompt runs" section, never derived directly from
`document_explicitly_states_transmission_history`.

### 4a. Relationship-type vocabulary (new in v2)

Unlike `source_authority`, `relationship_evidence`, and
`shock_transmission_evidence`, `relationship_type` has no `CHECK`
constraint — free text from the LLM would fragment into
`supplier`/`supplier_to`/`vendor`/`key supplier` variants that Arm G can't
mechanically compare. Before implementation, fix a small closed vocabulary
(e.g. `supplier`, `customer`, `competitor`, `partner`, `acquirer_target`)
and an explicit directionality convention — `entity_id_a` plays a fixed
role relative to `entity_id_b` for each type (e.g. "a supplies b"),
consistently in every row. This is a **schema-level `CHECK` addition**,
not just a documentation note, so a bad value fails at write time.

## 5. Candidate generation

For each `canonical_event` (via its issuer, resolved through `catalysts`),
find qualifying `entity_relationships` rows connecting the issuer to other
resolved entities, and insert one `candidate_signals` row per
(`event_version_id`, counterparty `entity_id`), respecting the existing
`UNIQUE` constraint. Link supporting relationship(s) via
`candidate_supporting_relationships`.

**Eligibility policy v1 (`policy_version = "candidate_eligibility_v1"`):**

- `eligible` only if a qualifying relationship row has
  `relationship_evidence IN ('explicit_named', 'quantified_named')`
  **and** clears both timing checks in §5a below.
- `inferred_structured` evidence stays **ineligible** in v1
  (`eligibility_reason = "relationship_evidence=inferred_structured"`,
  never dropped) — matches v1's own conservative draft; don't loosen it
  until a future policy version is explicitly frozen. Recording rather
  than discarding gives real empirical data on whether this rule is
  costing usable candidates later.
- Also `ineligible`, with reason, when: the relevant relationship row(s)
  are outside their validity window (see §5a), or when timing fails.

### 5a. `decision_at` — defined (the v1 blocker)

`event_versions.decision_at` was never given an operational meaning in v1.
Defined now: **`decision_at` is the timestamp when the complete candidate
pool for an event is frozen and handed to Arm A/G — i.e. once every
document belonging to that event's catalyst has reached a terminal
extraction state (§2a) and entity resolution has run.**
`first_executable_at` is the first permitted execution timestamp after
that, under the spec's existing regular-session rule — kept distinct so
actual pipeline latency (extraction + resolution time) is preserved rather
than assumed away.

Eligibility must check **both** clocks, not just public availability:

```
evidence_publicly_available_at <= decision_at
AND
system_observed_at <= decision_at
```

The second check matters specifically because the schema separates *when
the market could have known* from *when this pipeline actually learned
it* — if a 2024 filing is reprocessed in 2026 and reveals a previously
unresolved relationship, its 2024 public timestamp cannot retroactively
make that relationship available to a hypothetical 2025 decision; the
system genuinely didn't know it yet. Additionally exclude relationships
already superseded or outside their validity window as of `decision_at`,
where those fields are populated:
`relationship_valid_from IS NULL OR <= decision_at`,
`relationship_valid_to IS NULL OR > decision_at`,
`record_superseded_at IS NULL OR > decision_at`.

## 6. Testing strategy

- Fixture-based extraction tests against a stubbed (not live) LLM call,
  same style as `test_edgar_ingest_worker.py`.
- **Duplicate-event test:** a synthetic catalyst with a primary + exhibit
  document both describing the same guidance figure must canonicalize to
  one `canonical_events` row with two `event_document_links` rows, not two
  events.
- **Nullable-surprise test:** an extraction with `"surprise": null` (e.g.
  `acquisition_or_divestiture`) must insert successfully.
- Resolver tests: exact match, alias match, no-match → logged not
  fabricated, and an explicit assertion that an unresolved name never
  produces a new `entities` row.
- Evidence-span verification test: a non-substring span drops its
  event/relationship and is logged, never silently kept.
- **Timing test:** a relationship with `system_observed_at` after
  `decision_at` must be excluded from eligibility even when
  `evidence_publicly_available_at` is well before it.
- Idempotency test: re-running the full pipeline twice over the same
  documents at the same prompt+model configuration produces zero
  duplicate rows anywhere in the chain (`extraction_runs` through
  `candidate_signals`).
- Atomicity test: a forced failure partway through one catalyst's
  processing batch leaves no partial rows from that batch.

## Open items still not resolved by this document (unchanged from v1, tracked, not silently dropped)

- The per-sector coverage sanity check and pre-drawn backup list
  (`CANDIDATE_UNIVERSE_V1.md`).
- The infrastructure fork (hosting, LLM budget, monitoring).

ChatGPT's own assessment, after this round: no further conceptual design
review is needed before implementation — the next useful adversarial
review is against the actual runner code and its tests, not another round
of design text. Agreed.
