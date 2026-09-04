Implement the extraction-runner bridge for this project: raw filing → LLM
extraction → entity resolution → relationship write → candidate
generation. This is the top-priority gap in the live pipeline — right now
`entity_relationships` and `candidate_signals` are only ever written to
inside test fixtures, and nothing connects real EDGAR filings to them.

**Read first, in this order:** `docs/EXTRACTION_RUNNER_DESIGN_V2.md` (the
design to implement — it also explains what changed from v1 and why, so
you have the reasoning, not just the conclusions), `docs/CANDIDATE_UNIVERSE_V1.md`
(the 108-company watchlist), `schema.sql`, `build/extraction_prompt_v1.md`,
`build/edgar_ingest_worker.py`, `build/candidate_coverage.py`, and
`build/tests/conftest.py` (the existing test-DB pattern — a local Postgres,
`dbname=diffusion_experiment user=postgres`, truncated between tests; keep
using this pattern, don't introduce a different test-DB setup).

Do not deviate from the design doc's decisions without flagging it back to
me first — several of them (append-only relationships, the `decision_at`
definition, the watchlist-vs-candidate-universe split) were made
deliberately after a full review round, not left open for reinterpretation.
If you hit a real ambiguity the doc doesn't resolve, stop and ask rather
than guessing.

## Implementation order

**1. Schema migration** (new file, e.g. `build/migrations/002_extraction_runner.sql` —
check whether `schema.sql` is treated as the single source of truth or
whether migrations are meant to be separate; if unclear, ask):
- `entities`: add `UNIQUE (cik) WHERE cik IS NOT NULL`.
- New `entity_aliases` table (§1 of the design doc).
- New `extraction_runs` table (§2c) — document + prompt version + model
  identity, not prompt version alone.
- New `unresolved_entity_mentions` table (§3).
- `extracted_events`: make `surprise_type` and the rest of the surprise
  block nullable (§2b) — this is a real bug fix, not a style choice; a
  valid extraction with `"surprise": null` currently cannot be inserted.
- `entity_relationships`: add a `CHECK` constraint on `relationship_type`
  against a small closed vocabulary, and add a provenance FK to
  `extraction_runs` (§4a, §4). Pick the vocabulary from the design doc's
  suggestion (`supplier`, `customer`, `competitor`, `partner`,
  `acquirer_target`) unless you find a reason to adjust it — say so if you
  do.
- Update `build/README.md`'s schema section to note the migration exists.

**2. Entity, alias, and instrument seeding** (§1):
- CIK lookup for all 108 tickers in `CANDIDATE_UNIVERSE_V1.md` against
  SEC's own `company_tickers.json` (`https://www.sec.gov/files/company_tickers.json`)
  or the EDGAR full-text search API — not from memory or training data.
  Write the resulting ticker→CIK mapping to a checked-in file (e.g.
  `build/seed_data/watchlist_ciks.csv`) so it's reviewable and versioned,
  not just applied silently.
- A seeding script that inserts the 108 companies into `entities`, a
  starting alias for each (at minimum the legal name as written and the
  bare company name without corporate suffix), one `instruments` row
  (common equity) and one `instrument_identifiers` row (ticker) per entity.
- Update `edgar_ingest_worker.py`'s `WATCHLIST` mechanism to resolve
  `cik -> entity_id` from the `entities` table at startup instead of a
  hardcoded list of UUIDs (per the design doc's explicit call-out — don't
  leave the hardcoded-UUID pattern in place).
- If any of the 108 tickers fail CIK lookup (delisted, renamed, ticker
  changed), log it clearly and stop rather than silently skipping — this
  needs a human decision, not a silent drop.

**3. Extraction runner** (§2a, §2b, §2c):
- Selects unprocessed `raw_documents` (per `extraction_runs`), calls the
  LLM against `extraction_prompt_v1.md`'s exact system+task prompt and
  JSON schema, validates the output (schema match, every `evidence_span`
  an exact substring of `raw_content`, `extraction_prompt_version` matches
  what was requested).
- **Catalyst-level canonicalization**: don't write final `canonical_events`
  until every document in a catalyst has reached a terminal extraction
  state. Merge matching extractions across a catalyst's documents on the
  fingerprint described in §2a; write `event_document_links` for every
  contributing document.
- Writes `extracted_events` (with the nullable surprise fix applied),
  `canonical_events`, `event_versions` (`version_number=1` unless the
  source itself flags `explicit_correction=true`), `event_entities`.
- All writes for one catalyst's successful processing batch commit
  atomically.
- For a stubbed LLM call in tests, use whatever mocking approach fits this
  codebase's existing test style — don't call a real LLM API in tests.

**4. Entity resolution** (§3, §3a):
- Normalize + match against `entities`/`entity_aliases`. No match → log to
  `unresolved_entity_mentions`, do not create an entity, do not write a
  relationship.
- Build a small manual-resolution path (can be a simple script/CLI, doesn't
  need a UI) for turning an `unresolved_entity_mentions` row into a
  resolution — either linking to an existing entity or creating a new one
  outside the 108-company watchlist (per §3a's decision that the filer
  universe and candidate-entity universe are separate). Apply the backfill
  rule exactly: the relationship gets written at resolution time, with
  `system_observed_at` set to the actual resolution timestamp, never
  backdated.
- Runner invocation ends with the one-line summary described in §3
  (documents processed, mentions resolved/unresolved, candidates produced,
  etc.) — log it, no dashboard needed.

**5. Relationship writes** (§4):
- Append-only insert per validated disclosure occurrence, with the
  `extraction_runs` provenance FK, `shock_transmission_evidence` defaulted
  to `'new_or_unobserved'`.

**6. Candidate generation** (§5, §5a):
- Implement `decision_at` exactly as defined in §5a (frozen once every
  document in the catalyst has reached a terminal state and resolution has
  run).
- Eligibility policy v1 exactly as drafted: `eligible` only for
  `explicit_named`/`quantified_named` evidence passing both the
  public-availability and system-observed-time checks against
  `decision_at`, plus the validity-window checks; everything else
  `ineligible` with a specific `eligibility_reason`, never dropped.
  `policy_version = "candidate_eligibility_v1"`.

## Tests

Implement every test in the design doc's §6, in the existing style
(`build/tests/test_edgar_ingest_worker.py` is the closest precedent —
match its rigor, not just its shape): duplicate-event canonicalization,
nullable-surprise insertion, resolver exact/alias/no-match behavior (with
an explicit assertion that a no-match never creates an entity), evidence-
span verification, the `decision_at` timing test, full-pipeline
idempotency, and atomicity under a forced mid-batch failure.

## What's explicitly not part of this task

Don't touch the per-sector coverage sanity check or backup list
(`CANDIDATE_UNIVERSE_V1.md`'s still-pending items 2-3) — that's a separate,
not-yet-scoped research-design task. Don't stand up any production
infrastructure (hosting, scheduled runs, an LLM API budget) — that's the
still-open infrastructure fork, a separate decision. Don't touch
`index.html`, `growth_chart.png`, or anything in `build/prototype/` — the
prototype and the live pipeline are separate tracks.

## When you're done

Report back: what you built, any deviations from the design doc and why,
the actual `relationship_type` vocabulary you landed on if you changed it,
test results, and anything you found while implementing that the design
doc got wrong or missed — I'll independently check anything load-bearing
before we call this round done, same as every other round on this project.
Commit with clear, incremental messages; push when it's in a reviewable
state.
