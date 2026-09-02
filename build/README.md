# The Diffusion Experiment — first build

This is the first real code from the spec (v2.2.1), built with Sonnet 5 and
tested against a live Postgres database in this session — not just written
and hoped to work.

## What's here

- `schema.sql` — the original database structure. Ran successfully against
  Postgres 16. Includes a manual check (in the build log, not this folder)
  confirming the most important fix — a relationship valid since 2022 but
  not publicly known until 2026 correctly stays invisible to a 2024 query.
  As of the extraction-runner bridge, this is no longer the *only* schema
  file — see `migrations/` below. Apply all four, in order, on a fresh
  database:
  ```
  createdb diffusion_experiment
  psql -d diffusion_experiment -f schema.sql
  psql -d diffusion_experiment -f migrations/002_extraction_runner.sql
  psql -d diffusion_experiment -f migrations/003_extraction_runner_fixes.sql
  psql -d diffusion_experiment -f migrations/004_range_valued_guidance.sql
  ```
  `schema.sql` itself is treated as migration "001" retroactively (it was
  never versioned before now); `migrations/002_extraction_runner.sql` is
  the first real, numbered migration, adding `entity_aliases`,
  `extraction_runs`, `unresolved_entity_mentions`, `watchlist_membership`,
  a nullable `extracted_events.surprise_type` (the prompt's own
  `"surprise": null` case was previously un-insertable), and a closed
  vocabulary + provenance FK on `entity_relationships`.
  `migrations/003_extraction_runner_fixes.sql` came from an adversarial
  code review of migration 002 + the runner before the first live dry
  run — see "A pre-dry-run code review" below.
  `migrations/004_range_valued_guidance.sql` adds `observed_value_low`/
  `_high` and `reference_value_low`/`_high` to `extracted_events`, found
  necessary by Dry Run 001 (`build/DRY_RUN_REPORT_001.md`) — see "A
  post-dry-run code review" below.
- `extraction_prompt_v1.md` — the prompt that reads a document and pulls out
  structured facts (event type, companies involved, raw numbers). Its JSON
  output schema was checked and parses correctly. It deliberately never
  computes statistics itself — see the file for why.
- `surprise_transform.py` — the small deterministic script that turns raw
  numbers into a standardized "surprise" value, replacing the formula that
  used to blow up near zero (e.g., EPS guidance moving from $0.01 to $0.05
  used to register as "+400%" — this fixes that).
- `edgar_ingest_worker.py` — the program that would poll SEC EDGAR for new
  filings and store them, including exhibits (an 8-K's cover page routinely
  just says "see Exhibit 99.1" for the actual earnings numbers). Its
  database-writing logic is tested; its own HTTP code has NOT been run
  against the live SEC API in this session (no outbound network access from
  this sandbox's Python process). The URL shapes and document-listing
  format it assumes WERE independently checked against live sec.gov pages
  (see "A second round of code review" below, which is exactly what caught
  a URL that 404s and a document-type field that never matched anything) —
  but that was done with a separate tool, not this worker's own code path.
  See the file's docstring for what to check before running it for real.
- `candidate_coverage.py` — a small but important check: before building
  Arm A vs. Arm G portfolios, confirms every eligible candidate actually
  has a decision from BOTH models, not just that they draw from the same pool.
- `tests/` — 52 automated tests, all passing (26 after the first review
  round, 36 after the second, 52 after the third — see below). Several
  specifically try to break exact bugs found by review rounds (the
  bitemporal leak, the surprise-formula blowup, the Arm A/G candidate-pool
  mixing, a `--dry-run` flag that used to permanently corrupt real data,
  exhibit documents being skipped, a live-SEC URL that 404s, a filing
  failure that used to take down a whole poll batch, a real filing with
  duplicate exhibit types that used to be permanently un-ingestable, and
  more — see "A round of code review", "A second round of code review",
  and "A third round of code review" below) rather than just checking
  things look right.

## A round of code review (after this build existed)

Once real code existed, it got a round of review from ChatGPT — a genuinely
different, and more valuable, exercise than reviewing the design document,
because it caught bugs that only exist once something is actually built.
Four were confirmed and fixed:

1. **The EDGAR worker only fetched the primary document, not exhibits.**
   An 8-K's cover page is often just "see Exhibit 99.1" — the actual
   earnings/guidance numbers live in the exhibit. Fixed: the worker now
   fetches the filing's index and pulls in relevant exhibits (EX-99.x),
   all linked to one shared catalyst.
2. **`--dry-run` was writing a placeholder into the real database and
   committing it**, which then permanently blocked the real filing from
   ever being ingested (the "already seen this" check keyed off the same
   identity either way). Confirmed as a real bug, fixed: dry-run now writes
   nothing at all.
3. **The timestamp for "when did this become public" trusted SEC's
   acceptance time**, but SEC's own documentation (checked directly)
   confirms filings submitted after 5:30pm ET disseminate the next
   business day — meaning the old code could be wrong by an entire weekend.
   Fixed: the pipeline's own observed time is used instead, which is
   conservative and honest rather than assuming precision the data doesn't
   support.
4. **`content_hash` was globally unique**, so two distinct disclosures that
   happened to share identical boilerplate text would silently collapse
   into one row and one would vanish. Fixed: identity is now the SEC
   accession number + which document within it; content hash is kept only
   to flag likely duplicates, never to drop them.

Several smaller but real issues were also fixed: `arm_entries` couldn't
represent four of the seven experimental arms (no field said which sector
ETF or matched security an entry actually bought); `candidate_signals`
allowed the same company to be registered twice for one event; a test's own
comment claimed something the schema didn't actually guarantee (fixed by
writing the actual completeness check, `candidate_coverage.py`, that the
comment assumed existed); the extraction prompt asked the model to judge
"transmission" partly from "your knowledge," directly contradicting its own
document-only extraction rule; and a few small schema inconsistencies
(a stale unused column, a missing constraint, an unused watchlist field).

**Flagged but deliberately not fixed yet, because it's bigger and more
delicate:** the statistical test currently treats every individual trade as
equally weighted evidence, when several trades from one catalyst should
arguably count as one; and it has no protection against different catalysts
whose outcome windows overlap in time (e.g. same-week events in one sector
sharing a market-wide shock). Both are real methodological gaps, but fixing
them well needs its own careful redesign and its own new null simulations —
not a rushed patch. Until that's done, this statistical test should not be
trusted as a real "promote to real money" decision, even though it's fine
for exploring pilot data.

## A second round of code review — this time it caught a live-API bug

A second review (again ChatGPT, this time against the fixed v3 code) made a
point of distinguishing what it could check statically from what it
couldn't run at all in its own sandbox (no `psycopg2`/Postgres there
either) — and specifically flagged that the EDGAR exhibit-fetching fix
(review round 1, fix #1) was tested only against a mocked filing-index
response, which could be hiding a real interface mismatch with the live
SEC API.

That claim was checked directly against sec.gov (the same standard applied
to the 5:30pm-ET dissemination claim in round 1) rather than taken on
faith, by fetching a real, live SEC filing's index pages. It was correct,
and worse than "imprecise" — **the exhibit-fetching code could never have
worked against the real API at all:**

1. **The URL the worker requested for a filing's index
   (`{accession}-index.json`) 404s on live sec.gov.** The real per-filing
   directory listing lives at plain `index.json` (no accession prefix) —
   confirmed by fetching both live. But that real endpoint's `"type"` field
   turned out to be a generic file-icon category (e.g. `"text.gif"`), not
   an EDGAR document type like `"EX-99.1"` — confirmed by comparing it
   against that same filing's `-index-headers.html`, which does carry the
   real `<TYPE>`/`<SEQUENCE>`/`<FILENAME>` triplet per document. So even
   pointed at the URL that actually works, the old exhibit-matching logic
   could never have matched anything. **Fixed:** the worker now fetches
   `-index-headers.html` and parses the real `<TYPE>`/`<FILENAME>` pairs
   directly. Net effect of the bug as shipped in v3: every real
   (non-dry-run) poll would raise an uncaught exception fetching the index
   for every filing — the worker could never have ingested anything from a
   live run.
2. **That exception wasn't isolated per filing.** It would propagate out of
   `poll_once`'s loop and silently skip every *other* new filing for that
   company too, until the next poll — much worse than the existing
   "one company's failure doesn't take down the whole poll" isolation.
   **Fixed:** each filing's ingestion is now caught and logged
   individually. Making that fix surfaced a related bug of its own: a
   failed statement left un-rolled-back on a shared connection poisons
   every later query on it ("current transaction is aborted") — the new
   per-filing handler now rolls back before continuing.
3. **The "is this filing new" filter used SEC's acceptance time**, which
   re-created the exact problem fix #3 (round 1) was written to solve, one
   level up: since after-5:30pm-ET filings can disseminate — i.e. become
   visible in the API at all — as late as the next business day, a filing
   accepted Friday evening but not visible until Monday could fall outside
   a tight lookback window and be silently dropped forever (this filter is
   the *only* place a filing is ever discovered; the database dedup check
   never gets a chance to run on something filtered out here). **Fixed:** a
   fixed multi-day buffer is now added on top of `--lookback-hours`
   specifically so this filter can never be the reason a newly-disseminated
   filing goes unprocessed — exact-once correctness still comes entirely
   from the database dedup check, never from this time window.
4. Two smaller timestamp mislabelings in the same area: `source_published_at`
   was silently being set to SEC's acceptance time (reintroducing the
   "acceptance == public" conflation fix #3 removed, one column over — now
   left `NULL`, since the submissions API gives no genuine source-stated
   publication time), and `first_public_timestamp_precision` was hardcoded
   to the steady-state 15-minute poll interval even on a wide backfill run
   that could discover a filing hours after it was actually accepted (now
   `max(poll interval, time actually elapsed since acceptance)`).

A smaller, genuinely valid catch from the same review: `relationship_evidence`
still allowed a value called `model_inferred` ("you are inferring this
relationship yourself, not reading it from the text"), which directly
contradicted this same extraction prompt's own "if you cannot point to a
supporting span, do not extract the claim" rule — the same class of
contradiction round 1 already caught and removed for the transmission
field. Removed here too (extraction prompt bumped to 1.1.0); nothing was
extracted for real yet, so there's no legacy data using it.

One more gap was checked and fixed even though it wasn't about a live API:
the review pointed out that `arm_entries.candidate_id`,
`.model_decision_id`, `.event_version_id`, and `.entry_quote_snapshot_id`
were each individually valid foreign keys, but nothing stopped them from
being *mutually* inconsistent — e.g. a candidate belonging to a different
event than the entry's own, or a quote snapshot for a different instrument
than the entry is actually about. That's exactly the kind of corruption
that produces plausible-looking, silently wrong research data instead of
an obvious crash. Fixed with three trigger functions on `arm_entries` /
`arm_outcomes` that check cross-field consistency, each with a test that
confirms it rejects the inconsistent case and accepts the consistent one.

**Reviewed and deliberately not changed yet**, because it needs a small
design decision rather than an obvious fix: `candidate_coverage.py`'s
coverage check requires every model being compared to share one
`model_version` string, which can't express Arm A and Arm G genuinely
running different model versions (a real interface limitation, not urgent
while both arms happen to share a version in practice).

The other two items flagged in this same paragraph in the previous version
of this README — the `document_component` identity collision, and missing
HTTP retry tests — turned out not to be safe to leave deferred. See the
next section.

## A third round of code review — a deferred item turned out to be real, live data

A third review (ChatGPT again, against v4) went further than reading code:
it fetched several more live SEC filings on its own initiative and found a
real bug the previous two rounds had missed, which upgraded the one item
this README had called "low real-world likelihood, safe to defer."

**The document_component collision is not theoretical.** The reviewer
found a real, live 8-K — UDR Inc., accession `0000074208-26-000045`, filed
2026-04-29 — that contains *two* `EX-99.1` documents (one `.htm`, one
`.pdf`) and the same duplicate pattern for `EX-99.2`. That was independently
re-confirmed by fetching that exact filing's own `-index-headers.html`
directly rather than taken on faith. Under the identity scheme at the time
(`sec_accession_number` + the type label `document_component`), the second
`EX-99.1` would hit a database uniqueness violation — and because the
collision is deterministic, that filing would fail identically on every
future poll, forever. **Fixed:** identity is now `sec_accession_number` +
SEC's own per-document `SEQUENCE` number, which SEC's Public Dissemination
Technical Specification requires to be unique within one filing package;
`document_component` is kept only as a descriptive label.

That same live example exposed a second real bug: one of the duplicate
`EX-99.1` copies is a `.pdf`, and the code was calling `response.text` on
it unconditionally — which does not extract PDF text, it silently decodes
binary bytes as if they were characters. **Fixed:** a relevant exhibit
filed as `.pdf` is now skipped with a clear log message rather than
ingested as corrupted text (in every live example seen so far, an
`.htm`/`.txt`/`.xml` copy of the same exhibit is filed alongside it
anyway). An exhibit filed *only* as a PDF, with no text-format copy, is a
known, deliberately-not-built limitation — this pipeline has no PDF-text
extraction step yet.

Also fixed from the same round: the exhibit-index parser assumed a
document's `TYPE` field never contains whitespace, which is false for some
real EDGAR forms (confirmed live) — not a problem for this project's
actual target forms (8-K / EX-99.x), but the parser was restructured to
read each `<DOCUMENT>` block independently anyway (needed for the sequence
fix above), which fixes this as a side effect; a filing whose index
doesn't parse into any `<DOCUMENT>` entries at all now raises an explicit
error instead of silently "succeeding" as primary-only, since SEC's spec
guarantees every real filing has at least one such block for its own
primary document, so zero matches means something broke, not that there
are no exhibits; a failed exhibit fetch used to let the filing commit
anyway with that exhibit permanently missing (flagged but not fixed in
round 2) — ingestion is now genuinely all-or-nothing per filing, so a
partial failure rolls back completely and the next poll retries it
cleanly; the previous round's lookback buffer was replaced with no
time-based filtering at all, since SEC's own submissions API already
bounds each company's recent-filings list generously and the database
dedup check (not any time window) has always been what makes re-ingestion
safe; per-document timestamps within one filing package no longer drift by
a few seconds from each other; and two more schema consistency gaps were
closed — a cash-equivalent arm entry could still accidentally carry a
traded instrument, and a candidate for one company could be paired with a
traded instrument belonging to a *different* company (e.g. an NVIDIA
candidate entered against an Apple instrument) — both now enforced by
trigger, both tested. The previously-missing HTTP retry unit tests
(timeout, 503, 429, 403 — none need a database) were added too.

A follow-up pass on this same v4→v5 change (ChatGPT again, fetching yet
more live filings to specifically try to break the new sequence-based
parser) found two more small real gaps, both fixed: a `<DOCUMENT>` block
missing one of TYPE/SEQUENCE/FILENAME was silently skipped instead of
failing the whole filing (inconsistent with the fail-visibly approach used
everywhere else in this function); and a relevant exhibit that exists
*only* as a PDF (SEC's own rules allow this for some filing types, e.g.
8-K Item 6.10 asset-backed filings) would have been silently dropped
instead of failing visibly. Both now raise. A duplicate-SEQUENCE guard was
also added defensively, since SEC's spec requires SEQUENCE but doesn't, in
so many words, promise it's unique — this pipeline now enforces that
assumption itself rather than trusting it silently.

**Still reviewed and deliberately left open**, because it needs an actual
design decision, not a bug fix: nothing currently stops an Arm A entry from
referencing a decision that was actually recorded under `arm_g_mechanical`
(or vice versa) — `model_candidate_decisions.model_id` isn't tied to
`experiment_arms.arm_code` anywhere. The reviewer called this "important
but not a raw-ingestion blocker." Fixing it properly means deciding how an
arm declares which model(s) it trusts (a new column on `experiment_arms`,
most likely) rather than bolting on a trigger against today's schema, so
it's flagged here rather than rushed.

## The extraction-runner bridge (raw filing → LLM extraction → entity resolution → relationship write → candidate generation)

Implements `docs/EXTRACTION_RUNNER_DESIGN_V2.md` end to end — previously
`entity_relationships` and `candidate_signals` were only ever written to
inside test fixtures; nothing connected a real filing to them. New files:

- `entity_resolution.py` — normalizes a raw entity name and matches it
  against `entities`/`entity_aliases`. No match (or an ambiguous one) is
  logged to `unresolved_entity_mentions`, never guessed at, and never
  creates an entity.
- `seed_entities.py` — seeds the 108-company watchlist
  (`seed_data/watchlist_ciks.csv`) into `entities`/`entity_aliases`/
  `instruments`/`instrument_identifiers`/`watchlist_membership`. CIKs were
  looked up against SEC's own `company_tickers.json` and cross-checked for
  all 108 against each CIK's live submissions API record — not from
  memory. Two real discrepancies turned up and were decided explicitly
  (see the CSV's `note` column), not silently resolved: GPS (Gap Inc.)
  renamed its ticker to GAP on NYSE; XOM (Exxon Mobil) moved to a brand
  new CIK in 2026 (a holding-company reorganization) that now files under
  the ticker.
- `llm_client.py` — loads the exact system/task prompt text and JSON
  schema straight out of `extraction_prompt_v1.md` (never hand-copied) and
  calls the model. The real Anthropic-backed client is not exercised by
  any test and is not wired to a live budget — standing up production LLM
  spend is the still-open infrastructure fork, out of scope here.
- `extraction_runner.py` — the two-phase pipeline: per-document extraction
  + validation (§2b), then catalyst-level canonicalization once every
  document in a catalyst reaches a terminal state (§2a), entity
  resolution, append-only relationship writes (§4), and candidate
  generation with the frozen `decision_at` timing rule (§5/§5a). See the
  module's own docstring for two things the design doc left unspecified
  that had to be resolved during implementation: how an LLM's free-text
  `relationship_type` maps onto the new closed vocabulary, and that
  `event_versions.version_number` cannot yet follow a real correction
  chain (no field anywhere identifies which prior disclosure a correction
  refers to).
- `manual_resolve.py` — a small CLI for clearing the
  `unresolved_entity_mentions` queue by hand (list / resolve to an
  existing entity / resolve by creating a new one). Applies the backfill
  rule from §3 exactly: the relationship is written at resolution time,
  with `system_observed_at` set to the real resolution timestamp, never
  backdated.
- `migrations/002_extraction_runner.sql`, `migrations/003_extraction_runner_fixes.sql`,
  `migrations/004_range_valued_guidance.sql` — see above.
- 27 new tests (`tests/test_entity_resolution.py`,
  `tests/test_extraction_runner.py`, `tests/test_manual_resolve.py`, plus
  one more added to `tests/test_edgar_ingest_worker.py` for the live bug
  below) covering duplicate-event canonicalization, nullable-surprise
  insertion, exact/alias/ambiguous/no-match resolution (with an explicit
  assertion that a no-match never creates an entity), evidence-span
  verification, a document_id mismatch, the `decision_at` timing rule (at
  both the unit and integration level, including an earlier event seeing a
  relationship a later event in the same catalyst discovers), retry after
  a failed extraction, a genuine two-connection concurrent-claim race,
  full-pipeline idempotency, and atomicity under a forced mid-batch
  failure — bringing the suite to 79 tests total, all passing. (Dry Run
  001 and its post-dry-run fix round added 15 more — see below — for
  94 total as of this writing; `build/tests/pytest_run_002.xml` /
  `pytest_run_002.out.txt` are real, captured output for that count, not
  inferred from `.pytest_cache`.)

**A live bug found while wiring this up, not in the design doc:** every
real `-index-headers.html` page `edgar_ingest_worker.py` fetches serves
its `<DOCUMENT>` blocks **HTML-entity-escaped** (literally
`&lt;DOCUMENT&gt;`) inside a `<PRE>` tag — confirmed by fetching two
unrelated live filings directly (NVIDIA and the new-CIK ExxonMobil
Holdings Corp). Every existing test fixture used literal, unescaped
`<DOCUMENT>` text, which is not what `requests`' `.text` actually returns
from this URL. As shipped through this file's fourth revision (four prior
review rounds, three of which fetched live filings), **every real,
non-dry-run poll would have raised `FilingPackageParseError` on every
single filing** — this worker could never have ingested anything from a
live run, despite passing every mocked test. Fixed by unescaping the
fetched text before parsing; verified against live NVIDIA filings going
back to 2020 (`--dry-run` correctly listed primary+exhibit documents for
all of them, and a real non-dry-run ingest of NVIDIA's most recent 8-K
round-tripped cleanly into `raw_documents` and was picked up by
`extraction_runner.select_unprocessed_documents`).

## A pre-dry-run code review — six real bugs, before the first live LLM call

Before pointing the runner at a real (paid, read-only-against-EDGAR) LLM
call for the first time, an adversarial review of the extraction-runner
bridge (not another design round — the design itself was already locked
in) found six real bugs, each independently traced against the actual
code before being accepted. `migrations/003_extraction_runner_fixes.sql`
carries the schema side of these; `extraction_runner.py`'s own module
docstring has the full narrative.

1. **`extraction_runs` retry was structurally broken.** The identity
   `UNIQUE` constraint allows exactly one row per (document, prompt
   version, model), but a retry of a `'failed'` row tried to `INSERT` a
   second one — a guaranteed uniqueness violation on every retry, not a
   rare edge case. Fixed: `claim_extraction_run()` makes the row a real
   claim-then-update state machine (`'pending'` → `'success'`/`'failed'`,
   with a `'failed'` or stale `'pending'` row reclaimable), so a retry
   `UPDATE`s the same row instead of inserting a new one.
2. **The same gap let two callers both pay for the same LLM call.** The
   old "does a successful row exist" check and the eventual `INSERT` were
   two separate statements with the (real-money) LLM call in between.
   Fixed by the same atomic claim as #1 — confirmed with a test using two
   genuinely separate database connections racing to claim the same
   document, not just a sequential rerun (which is all the existing
   idempotency test ever covered).
3. **`process_catalyst`'s idempotency flag wasn't config-aware.**
   `catalysts.canonicalization_completed_at` was one column with no
   memory of which prompt/model config produced it — reprocessing the
   same catalyst under a new config would see it already set and silently
   no-op, contradicting the design doc's own "reprocessing creates new
   downstream data" requirement. Fixed: replaced with
   `catalyst_processing_runs`, keyed by (catalyst, prompt version, model
   config), using the identical claim-then-update pattern as #1 — which
   also closes the equivalent race for two overlapping calls processing
   the same catalyst+config at once.
4. **`decision_at` was frozen too early, and candidates were generated
   too early.** It used to be captured once at the very top of
   `process_catalyst`, before any relationship had been written, with
   candidates generated for each event inside the same loop that was
   still writing LATER events' relationships — so an earlier event in a
   multi-event catalyst could not see a relationship a later event in the
   SAME catalyst discovered. This was a real completeness gap in
   candidate generation, not a timestamp technicality. Fixed:
   `_do_process_catalyst()` now writes every relationship for every event
   in the catalyst first, only then freezes `decision_at`, only then
   generates candidates for every event version.
5. **The real LLM call wasn't actually constrained by the JSON schema.**
   `llm_client.py` loaded the schema from `extraction_prompt_v1.md` but
   never gave it to the API call — enforcement was a text instruction
   ("Return ONLY valid JSON"), nothing more. Fixed: the Anthropic call now
   forces a tool call whose `input_schema` IS the loaded schema (Claude's
   actual structured-output mechanism), and `extraction_runner.py`
   additionally runs a real JSON Schema validator (`jsonschema.validate`)
   against the raw response before its own hand-rolled per-claim checks
   run as a second pass.
6. **Two smaller, real gaps in the same code:** `validate_extraction_
   output()` checked that `document_id` was *present* but never that it
   *matched* the document actually sent (the test helper hiding this,
   which hardcoded a placeholder for every call, is fixed too, with a new
   negative test); and `extraction_runs.raw_llm_output` was storing the
   *cleaned* (post-validation) output rather than the true raw provider
   response the schema's own comment calls for. Fixed: `raw_llm_output` is
   now the true raw response, `cleaned_llm_output` and `validation_drop_log`
   hold the validated result and what was dropped and why, and
   `extracted_events` now carries its own `extraction_run_id` so the full
   audit trail is reachable from an `extracted_events` row rather than
   just its own single event object. (`manual_resolve.py` was updated to
   read `cleaned_llm_output`, not `raw_llm_output`, for the same reason —
   backfilling a relationship from unvalidated raw text would resurrect
   exactly the kind of claim validation exists to drop.)

**Explicitly deferred, not fixed in this round:** relationship
directionality (the LLM's `entity_a`/`entity_b` order is taken as-is, with
no ordering contract enforced) needs a prompt change, not a quick patch,
and doesn't affect a first mechanics-only dry run; the §3a
U.S.-tradable admission rule in `manual_resolve.py`, alias-learning on
manual resolution, and `normalize_entity_name`'s legal-suffix edge cases
aren't exercised until manual resolution actually happens; and
`first_executable_at` staying `NULL` only matters at the trade-execution
stage.

## Dry Run 001 — the first real, no-cost extraction test

`build/DRY_RUN_REPORT_001.md` is the full write-up: real EDGAR filings
(NVIDIA, Caterpillar, Eli Lilly, JPMorgan Chase), 9 documents hand-extracted
by reading `extraction_prompt_v1.md` directly (no Anthropic API call — see
`llm_client.FileBackedExtractionClient`), run through the real pipeline.
It found one load-bearing bug (below) and several smaller real gaps,
fixed in the next round.

## A post-dry-run code review — four real fixes plus a hardening, from Dry Run 001's findings

An adversarial review of Dry Run 001's report (ChatGPT, independently
re-verified before being accepted, same discipline as every other round)
confirmed one load-bearing bug and three smaller real gaps, plus proposed
a hardening this project agreed was worth doing alongside them.
`migrations/004_range_valued_guidance.sql` and `extraction_prompt_v1.md`
v1.2.0 carry the schema/prompt side; `build/tests/pytest_run_002.xml` /
`pytest_run_002.out.txt` are the real, captured `pytest -v` output for
this round (94 passed, exit code 0) — not inferred from `.pytest_cache`,
which a review round correctly flagged as too weak a source for this
project's own "verified, not asserted" standard (`.pytest_cache/v/cache/
nodeids` only ever grows across runs; it proves tests are known to exist,
not that they passed in one specific invocation).

1. **The Eli Lilly `&`/`and` resolution gap (the dry run's single
   load-bearing finding) is fixed** — but not by changing
   `normalize_entity_name()`. `seed_entities.py` now seeds one additional
   alias per `"&"`-bearing legal name with the `&` spelled out as `and`
   (currently: Deere, Eli Lilly, Merck, JPMorgan Chase). A global
   `&`→`and` canonicalization in the normalizer itself was considered and
   rejected: it would silently change already-correct normalized forms
   for names where `&` isn't trailing (`Johnson & Johnson` normalizes to
   `johnson johnson` today; canonicalizing the `&` first would produce
   `johnson and johnson` instead, for no benefit, and would need every
   existing `entity_aliases` row recomputed and re-checked for new
   collisions across the whole 108-company set). The alias-only fix
   touches nothing that already worked.
2. **A deterministic issuer-identity shortcut**, a separate hardening
   proposed alongside the fix above: `process_catalyst` already knows a
   catalyst's issuer independently of anything extraction produces (from
   the filing's own CIK, via `watchlist_membership`) — an extracted
   entity with `role="issuer"` is now resolved directly to that known
   `entity_id`, never through name-based resolution, so a normalization
   mismatch can never orphan an issuer's own events again. Named
   counterparties, and an issuer mentioned in *another* company's filing,
   are unaffected — still resolved normally.
3. **`reference_source` can be `null`** (`extraction_prompt_v1.md` v1.2.0)
   — it was typed as a required string even though the prompt's own
   instructions say to use `null` when a field can't be determined; three
   of the dry run's own extractions had to route around this. Paired with
   a real provenance invariant `validate_extraction_output()` now
   enforces: a non-null reference value (point or, per #4, range) with no
   stated `reference_source` is dropped as an unexplained benchmark, not
   kept as a fact.
4. **Range-valued guidance has real fields now**
   (`observed_value_low`/`_high`, `reference_value_low`/`_high`, all
   nullable, on `extracted_events` and in the schema) instead of forcing
   every ranged figure ("$85.0 billion to $87.0 billion") into a single
   number or dropping it. Deliberately no computed midpoint anywhere, and
   `surprise_transform.py` is untouched — a range-aware transform is a
   separate design decision for later; `surprise_transformed` simply
   stays `NULL` for a range-valued event, same as it already does for a
   null `observed_value`. The canonicalization fingerprint was extended
   to include the new range fields too (not explicitly requested, but a
   direct, necessary consequence: without it, two different ranges
   sharing a `surprise_type`/`period` would silently collide into one
   canonical event, since `observed_value`/`reference_value` are both
   `None` for a range claim).
5. **Cover-page stubs are a prompt fix, not a canonicalization fix.** The
   dry run found that a contentless "we filed a press release, see
   Exhibit 99.1" cover page produces its own `null`-surprise stub event
   that never canonicalizes with the exhibit's real event (different
   fingerprints) — two canonical events per catalyst as the normal case
   for an ordinary earnings 8-K, not one. `extraction_prompt_v1.md` v1.2.0
   now instructs that a document which only announces or incorporates an
   exhibit by reference, with no substantive fact of its own, returns an
   empty `events` array. Canonicalization/fingerprinting itself was
   deliberately left alone — weakening the fingerprint match to merge a
   contentless and a substantive event risks merging things that aren't
   actually the same claim; fixing what counts as an event in the first
   place is the correct fix.

**Explicitly deferred, not fixed in this round:** a per-company
filing-volume cap on `edgar_ingest_worker.py` / a document-count cap on
`extraction_runner.py` (Dry Run 001 pulled in 411 documents scoping to
just 4 companies, since the worker has no volume control beyond *which*
companies); measuring real token counts on large documents (Dry Run 001's
largest exhibit was 1.1MB) before any real paid API call; and the
candidate-effective-N reporting distinction (multiple candidate rows from
one relationship aren't independent signals) — a downstream
analysis/reporting concern, not a pipeline bug. All tracked, not
forgotten.

## How to actually run this yourself

You'll need Postgres installed and running, and Python with `psycopg2-binary`,
`requests`, `numpy`, `scipy`, `jsonschema`, and (for tests) `pytest` installed
(`pip install psycopg2-binary requests numpy scipy jsonschema pytest`; add
`anthropic` too if you're going to run `extraction_runner.py` against a
real LLM).

```
createdb diffusion_experiment
psql -d diffusion_experiment -f schema.sql
psql -d diffusion_experiment -f migrations/002_extraction_runner.sql
psql -d diffusion_experiment -f migrations/003_extraction_runner_fixes.sql
psql -d diffusion_experiment -f migrations/004_range_valued_guidance.sql
python3 seed_entities.py
cd tests && python3 -m pytest -v
```

Before running `edgar_ingest_worker.py` for real: open it and replace the
placeholder email in `USER_AGENT` with your real contact info — SEC requires
this, it's not optional. The watchlist itself no longer needs hand-editing:
`seed_entities.py` (above) populates `watchlist_membership`, and the worker
resolves `cik -> entity_id` from the database at startup.

Before running `extraction_runner.py` for real: it needs `ANTHROPIC_API_KEY`
set and an actual LLM budget — standing that up is the still-open
infrastructure fork (see `docs/CANDIDATE_UNIVERSE_V1.md`'s "what's still
not decided"), not something this bridge does on its own.

## The statistical test (Section 8), and a real finding from testing it

`statistical_test.py` implements the actual pass/fail test: is Arm A's
advantage over Arm G bigger than delta, using a bootstrap that resamples
whole catalysts (not individual trades) so several trades from one news
event don't get counted as independent evidence.

Three simulations in `tests/test_statistical_null_simulations.py` check the
test itself before it's ever pointed at real data — feed it fake data with
a known right answer, see if it gets that answer:

- With no real effect at all, it should falsely say "there's an edge" about
  5% of the time (its stated confidence level), not much more.
- With a real, sizeable effect, it should actually detect it most of the
  time — 100% in this simulation.
- Ignoring the catalyst-clustering requirement should make the test
  noticeably easier to fool. Confirmed: under the same correlated data, the
  clustered test's false-positive rate stayed near 6%, while a naive version
  that ignores clustering was fooled **28% of the time** — a concrete
  demonstration of why Section 8 requires clustering, not just a
  theoretical concern.

**One genuine finding, not just confirmation:** with a modest number of
distinct catalysts (15–40, which is a realistic range early in Phase I-A),
the test runs slightly "hot" — a 7–9% false-positive rate against a 5%
target, rather than exactly 5%. This is a known property of this kind of
bootstrap with a limited number of clusters, not a bug in the code; it gets
closer to 5% as the number of distinct catalysts grows, but doesn't fully
disappear with the simple version implemented here. Practical takeaway:
the spec's existing sample-size requirement ("effective sample size in the
low hundreds") is about total observations — it should also specify a
minimum number of distinct catalysts (a reasonable starting target is 50+)
before trusting this test's result, since observation count alone doesn't
guarantee that.

## What's not built yet

The extraction runner (above) now sends a document's text through the
prompt, validates the response, and writes `extracted_events` through
`candidate_signals` — but it has never been pointed at a real, paid LLM
call (no API budget exists yet — see the infrastructure fork). Two
narrower gaps flagged during that build, not silently dropped:
`event_versions.version_number` doesn't yet follow a real correction
chain (see `extraction_runner.py`'s module docstring), and
`event_versions.first_executable_at` is left `NULL` — implementing the
spec's regular-session trading-time rule wasn't asked for as part of this
bridge. The underreaction estimator's math (Section 4) isn't implemented
as code yet — that comes after there's real data flowing through the
pipeline to test it against.
