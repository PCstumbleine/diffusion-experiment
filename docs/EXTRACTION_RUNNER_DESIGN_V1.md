# Extraction-Runner Bridge — Design v1

**Status:** draft, pre-implementation. Written for external review before any
code is written. Nothing in this document has been implemented yet.

## Why this document exists

`CANDIDATE_UNIVERSE_V1.md` identified the single largest gap in the live
pipeline: `entity_relationships` and `candidate_signals` are only ever
written to inside test fixtures (`build/tests/conftest.py` and friends).
Confirmed directly by grep, not inferred from the design docs:

```
grep -rl "entity_relationships\|candidate_signals" --include="*.py" build/ | grep -v test
# -> only candidate_coverage.py, and only via SELECT
```

No production code calls `extraction_prompt_v1.md` against a real LLM,
resolves an extracted entity name to a stable `entity_id`, or writes a row
to `entity_relationships` or `candidate_signals`. `edgar_ingest_worker.py`
stops at inserting `raw_documents` / `catalysts` / `catalyst_documents` —
confirmed by reading `ingest_filing()` end to end; it never touches
extraction at all.

This document scopes the bridge that closes that gap: **raw filing → LLM
extraction → entity resolution → relationship write → candidate
generation.** It is meant to be reviewed the same way `PREREGISTRATION_v7.md`
and `CANDIDATE_UNIVERSE_V1.md` were: written down before code, checked for
gaps, then handed to implementation.

## Also newly confirmed while writing this: entity seeding doesn't exist either

`edgar_ingest_worker.py`'s own comment is explicit: *"entity_id must already
exist in the entities table — this worker does not create entities, to keep
entity-identity management deliberate."* Confirmed by grep: the only
`INSERT INTO entities` anywhere in the codebase is in a test fixture. There
is no script that turns `CANDIDATE_UNIVERSE_V1.md`'s 108-company table into
rows in the `entities` table with real `entity_id` UUIDs.

That means **entity seeding is a prerequisite for this bridge, not a detail
inside it.** Section 1 below scopes it explicitly, because without it there
is nothing for entity resolution (Section 3) to resolve against.

## Scope for v1

**In scope:**
1. Seed `entities` from the 108-company watchlist (name + CIK + a small
   hand-maintained alias list per company).
2. A runner that finds unprocessed `raw_documents` rows, calls the
   extraction prompt, validates the output, and writes `extracted_events` /
   `canonical_events` / `event_versions` / `event_entities`.
3. Entity resolution: matching each extracted `entity_name` string against
   the seeded entity+alias set — exact/normalized match only, no fuzzy or
   ML matching in v1.
4. Writing `entity_relationships` rows for resolved relationship extractions.
5. A minimal, explicit candidate-eligibility policy that turns resolved
   relationships into `candidate_signals` rows.
6. Idempotency: safe to re-run without duplicating work, and a defined path
   for reprocessing when `extraction_prompt_version` bumps.

**Explicitly out of scope for v1** (flagging so it's a decision, not a
silent gap):
- Fuzzy/ML entity matching, or auto-creating new entities for unresolved
  names. Per the ingest worker's own stated design principle, entity
  identity stays a deliberate, reviewed action — not something the pipeline
  does automatically. Unresolved mentions get logged for manual review
  instead (Section 3).
- `calibrated_p_relationship` and `expected_transmission_effect` — both
  require models that don't exist yet and are explicitly out of the
  extraction prompt's job per its own "hard boundary" section.
- 6-K (foreign private issuer) filings — already excluded from the v1
  watchlist in `CANDIDATE_UNIVERSE_V1.md`.
- Any change to the promotion-gate logic itself (spec v2.1 §9-10) — this
  document is purely about getting real data flowing into the existing
  schema, not about the trading decision built on top of it.

## 1. Entity seeding (prerequisite)

- A one-time script reads the 108-company table from
  `CANDIDATE_UNIVERSE_V1.md` (or a checked-in CSV derived from it — see open
  question below) and inserts one row per company into `entities`
  (`legal_name`, `cik`, `entity_status = 'active'`), capturing the returned
  `entity_id` UUIDs into `edgar_ingest_worker.py`'s `WATCHLIST` list.
- **Aliases.** The extraction prompt returns company names *as written in
  the document* ("Nvidia Corporation", "NVIDIA", "Nvidia Corp."), which
  will rarely match `entities.legal_name` exactly. `entities` has no alias
  column today. Two options, not yet decided (see open questions):
  - (a) add a new `entity_aliases` table (`entity_id`, `alias_text`,
    `alias_source`) as a small, additive schema migration; or
  - (b) keep aliases in a checked-in static config file (YAML/CSV) for v1,
    since the watchlist is small (108 names) and changes rarely, avoiding a
    schema migration until the alias list actually needs runtime
    maintenance (e.g. a UI for reviewers to add aliases).
- Seeding is manual/reviewed, run once per watchlist change — consistent
  with the ingest worker's existing "this worker does not create entities"
  boundary.

## 2. Extraction runner

- Selects `raw_documents` rows not yet processed at the current
  `extraction_prompt_version`. Processed-state needs a home: proposed a new
  `extraction_runs` table (`document_id`, `extraction_prompt_version`,
  `status`, `raw_llm_output`, `error`, `run_at`) rather than overloading
  `extracted_events` for this, since a failed or empty-events run still
  needs to be recorded as "done" so the runner doesn't retry it forever.
- Calls the LLM with `extraction_prompt_v1.md`'s system + task prompt
  against `raw_documents.raw_content`, requesting the documented JSON
  schema (structured output / JSON mode).
- **Validates before writing anything:**
  - Output parses and matches the JSON schema in `extraction_prompt_v1.md`.
  - Every `evidence_span` is an exact substring of `raw_content` — the
    prompt's own "must point to a supporting span... do not paraphrase and
    call it a span" rule is a checkable invariant, not just an instruction
    to the model. A span that doesn't verify gets the event/relationship
    dropped and logged, not silently kept.
  - `extraction_prompt_version` in the output matches what was requested.
- On success, writes one `canonical_events` row per extracted event
  (`event_category`), one `event_versions` row (`version_number = 1`
  initially — corrections/reprocessing bump this, not covered further
  here), `event_entities` rows for each entity+role, and `extracted_events`
  storing the full `raw_llm_output` for audit, exactly as
  `extraction_prompt_v1.md` specifies.
- Does **not** compute `surprise_transformed` — that stays the deterministic
  downstream step `surprise_transform.py` already owns, unchanged from
  today.

## 3. Entity resolution

- For every `entity_name` (from both the `entities` list and the
  `relationships` list in the extraction output), normalize
  (lowercase, strip legal suffixes — Inc./Corp./Corporation/Ltd/Co. —
  strip punctuation) and look up against the seeded entities + aliases.
- **Match found:** use that `entity_id`.
- **No match:** do not create an entity and do not write the relationship.
  Log the raw string, the source document, and the event to an
  `unresolved_entity_mentions` table (or equivalent) for periodic manual
  review. This is the direct consequence of keeping entity creation
  deliberate (Section 1) rather than automatic — flagged as an open
  question below, because it has a real failure mode: if resolution misses
  often, candidate generation silently starves without anyone noticing
  unless the unresolved queue is actually watched.
- No confidence-weighted or fuzzy matching in v1. A wrong match silently
  corrupts a relationship record that the entire causal-link evidence chain
  depends on (Section 3 of the spec) — better to under-resolve and log than
  to guess.

## 4. Relationship writes

- For each `relationships` entry where both `entity_a` and `entity_b`
  resolve, insert a row into `entity_relationships`: `relationship_type`,
  `source_authority`, `relationship_evidence`,
  `raw_llm_relationship_score` (stored as-is, never treated as calibrated —
  per the prompt doc), `shock_transmission_evidence` set to the
  conservative default `'new_or_unobserved'` (per the prompt doc's own
  "what happens after this prompt runs" section — this field is not set
  from `document_explicitly_states_transmission_history` directly),
  `evidence_publicly_available_at` (from the document's
  `canonical_first_public_at`), `system_observed_at = now()`, and
  `source_document_id`.
- **Open question, not yet decided:** is this an append-only insert every
  time evidence is seen (fully consistent with `raw_documents`' own
  "the occurrence of a disclosure is itself data" philosophy, and with
  bitemporal history being a first-class design goal per the schema
  comments), or should it dedupe/upsert on
  `(entity_id_a, entity_id_b, relationship_type)` with a superseding
  mechanism similar to `event_versions.superseded_by`? The schema currently
  has no uniqueness constraint on that triple, which reads as a deliberate
  choice for append-only — but nothing says so explicitly, and this
  materially affects both storage growth and how candidate generation
  (Section 5) should query "the current relationships for this entity."

## 5. Candidate generation

- For each `canonical_event` (via its `issuer_entity_id` on `catalysts`),
  find resolved `entity_relationships` rows connecting the issuer to other
  seeded entities, and insert one `candidate_signals` row per
  (`event_version_id`, counterparty `entity_id`), respecting the existing
  `UNIQUE (event_version_id, entity_id)` constraint.
- **Proposed v1 eligibility policy** (this is the first concrete version of
  it — nothing like it exists in code today):
  - `eligible` if `relationship_evidence` is `explicit_named` or
    `quantified_named`, **and** `evidence_publicly_available_at` is set and
    is at or before the event's `decision_at`.
  - `ineligible` otherwise, with `eligibility_reason` recording which
    condition failed (e.g. `"relationship_evidence=inferred_structured"`,
    `"evidence not yet public at decision time"`).
  - `policy_version = "candidate_eligibility_v1"` — this is exactly the
    kind of rule the schema comment (Section 3) says should be versioned,
    so a future change doesn't retroactively alter what an old backtest
    saw.
  - Open question: should `inferred_structured` evidence be disqualifying
    outright in v1, or eligible-but-flagged for a model to weigh later?
    Drafted as disqualifying here for conservatism, but this is exactly the
    kind of call worth an outside check before it's locked in.
- Writes `candidate_supporting_relationships` linking each candidate to the
  relationship(s) that justified it (a candidate can have more than one,
  per the schema's own comment about an entity being both a named supplier
  and a named competitor).

## 6. Idempotency & reprocessing

- Re-running the runner on the same `raw_documents` set at the same
  `extraction_prompt_version` must not duplicate rows — guarded by the
  `extraction_runs` table (Section 2) as the "already processed" check.
- When `extraction_prompt_version` bumps (as it already has once, 1.0.0 →
  1.1.0), documents are eligible for reprocessing under the new version.
  Reprocessing writes new rows rather than overwriting old ones — old
  extractions stay in place for audit and for any analysis frozen against
  an earlier prompt version, exactly as `extraction_prompt_v1.md`'s own
  versioning section requires ("never silently reprocess old documents
  under a new version number without recording which version produced
  which output").

## 7. Error handling & cost control

- LLM call failures (timeout, malformed JSON, schema-validation failure)
  are retried with backoff (mirroring `EdgarClient.get()`'s existing
  retry pattern) up to a small fixed limit, then recorded as a failed
  `extraction_runs` row rather than silently dropped or retried forever.
- A per-run document cap and a minimum interval between LLM calls, sized
  for hobby-project budget — exact numbers deferred to implementation, but
  flagged here so the number is chosen deliberately rather than left
  unbounded.

## 8. Testing strategy

- Fixture-based extraction tests: hand-authored short documents with known
  expected extraction output (same style as `test_edgar_ingest_worker.py`),
  run against a stubbed LLM call rather than a live one.
- Resolver unit tests: exact match, normalized/alias match, no-match →
  logged-not-fabricated, and (explicitly) a test asserting that an
  unresolved name never results in a new `entities` row.
- Evidence-span verification tests: a span that isn't an exact substring of
  the source document must cause that event/relationship to be dropped and
  logged, never silently kept.
- Idempotency test: running the runner twice over the same documents at the
  same prompt version produces no duplicate rows anywhere in the chain.

## Open questions for review

1. Append-only vs. dedupe/upsert for `entity_relationships` writes (Section
   4) — which, and why?
2. Alias storage: a new `entity_aliases` table vs. a static config file for
   v1 (Section 1)?
3. Is the proposed v1 candidate-eligibility policy (Section 5) reasonable,
   or too strict/lenient — particularly the call to disqualify
   `inferred_structured` evidence outright rather than admit it flagged?
4. The "log unresolved mentions, never auto-create" resolution policy
   (Section 3) is deliberately conservative, but has a silent-failure mode
   if the unresolved queue just grows unwatched. Is there a lightweight
   way to surface that risk from day one without over-building monitoring
   infrastructure for a hobby-scale project?
5. Anything else this document is missing that would bite later — sequencing
   mistakes, a step that should be split differently, or a downstream
   consumer (Sections 4/5/6/7/8 of the main spec) this design doesn't
   actually feed correctly.
