Time for the first live, end-to-end test of the extraction-runner bridge:
real EDGAR filings, real extraction, a small enough scope to review by
hand. **No paid API calls** — the user doesn't have an Anthropic API
account and doesn't want to set one up or pay for one, so this run uses
you (Claude Code) as the extractor directly, instead of `llm_client.py`'s
`AnthropicExtractionClient`. You're already Claude, running under the
user's existing subscription — there's no separate cost to you reading a
filing and producing the extraction JSON yourself, the way you'd read any
other file and reason about it.

This is also not `edgar_ingest_worker.py`'s own `--dry-run` flag (that
writes nothing, so there'd be no raw_documents for extraction to work on)
— it's a real but deliberately small run against a disposable database.

## Setup

1. **Use a separate database, not the test one.** `diffusion_experiment`
   gets truncated automatically before every pytest run (`conftest.py`'s
   autouse `clean_db` fixture) — real dry-run data would get wiped the next
   time anyone runs the test suite. Create a new local database (e.g.
   `diffusion_experiment_dryrun`), apply `schema.sql` then migrations 002
   and 003 in order, and point every script at that DSN for this exercise.

2. **Seed it for real.** Run `seed_entities.py` against the new database —
   all 108 companies; this costs nothing (no LLM calls) so no reason to
   scope it down.

3. **Scope the actual EDGAR polling to a small handful of companies** for
   this first pass — not all 108. Add a lightweight way to limit which
   CIKs get polled in one invocation (a `--only-ciks` comma-separated flag
   or similar) rather than hacking `watchlist_membership` directly, since
   this is worth having as a reusable option going forward. Pick 3-4
   companies you'd expect to have at least one recent 8-K on file.

## The extraction step, done without an API call

Run the real EDGAR poll first — this populates `raw_documents` /
`catalysts` / `catalyst_documents` in the dry-run database, no LLM
involved yet.

Then, for each new document (cap this at something small and reviewable —
5-10 documents, not more):

1. Read `extraction_prompt_v1.md`'s system prompt and task instructions
   exactly as written (the same text `load_prompt_texts()` extracts) and
   apply them yourself to that document's real `raw_content` — you are
   acting as the extractor here, doing exactly what the prompt asks, not
   summarizing or taking a shortcut. Produce the JSON output matching the
   documented schema, including real `evidence_span` values that are
   genuinely exact substrings of the document (don't fabricate a span that
   merely sounds right — copy the actual text).
2. Save your output to a file, e.g.
   `build/dry_run_extractions/<document_id>.json`, so there's a durable,
   inspectable record of exactly what you produced for each document
   independent of the database.
3. Add a small stand-in LLM client in `llm_client.py` (e.g.
   `FileBackedExtractionClient` or similar — match the existing class's
   shape) whose `.extract()` reads the corresponding saved JSON file
   instead of calling the Anthropic API. Wire the extraction runner to use
   this client for this run. This keeps every downstream step — JSON
   Schema validation, `validate_extraction_output`'s per-claim checks,
   entity resolution, relationship-writing, candidate generation — running
   exactly as built and tested, with only the "call an API" step replaced.

Let the rest of the pipeline run for real against your saved extractions:
entity resolution, relationship-writing, candidate generation, no
shortcuts or mocking beyond the extraction-source swap above. If anything
throws partway through, stop and report the full error rather than
retrying blindly.

## What I need back

Not just aggregate counts — a report I (and then ChatGPT) can sanity-check
by eye:

- Which companies/CIKs were polled, how many filings were found, how many
  were genuinely new.
- For every document you extracted: your saved JSON next to a quote of the
  actual source text it came from, so correctness is directly checkable,
  not just "it parsed."
- Entity resolution stats: mentions matched vs. unresolved, with the
  unresolved raw strings listed specifically — that tells us right away
  whether `normalize_entity_name`'s known edge cases are actually costing
  real matches.
- Relationships written, and candidates generated broken down by
  `eligibility_status`/`eligibility_reason` — the real distribution, not
  just a total.
- Anything that looked surprising or wrong while you were doing the
  extraction yourself, even if nothing technically failed — you're in a
  good position to notice this firsthand, having actually read the filings.

Save this as a single markdown report file in the repo (e.g.
`build/DRY_RUN_REPORT_001.md`) — it's the input to the next review round,
so it needs to exist as a real artifact, not just terminal scrollback.

## What NOT to do

Don't touch the full 108-company watchlist yet, don't run this against the
shared test database, and don't call the real Anthropic API for this run
even if `ANTHROPIC_API_KEY` happens to be set in your environment for some
other reason — this specific run is meant to cost nothing. Don't treat "it
ran without crashing" as success on its own — the report is what actually
gets judged.
