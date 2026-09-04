Small, self-contained task while a separate recall-audit review is in
progress on the extraction side — this doesn't touch extraction logic at
all, just adds an operator control to `edgar_ingest_worker.py`.

## The problem

Scoping a poll to a handful of companies via `--only-ciks` still pulls
each company's entire historical 8-K backlog — Dry Run 001 asked for 4
companies and got 411 documents / 235 catalysts as a result, because the
worker has no per-company filing-volume limit by design (an earlier fix
deliberately removed lookback filtering — see its own module docstring's
fix #15, and don't reinstate that hidden filtering). That's correct
behavior for the real production poller, but it means a repeat of this
kind of small, reviewable exercise — onboarding one or two more companies
to a future dry run, say — repeats the same surprise every time.

## What to add

Two new optional CLI flags on `edgar_ingest_worker.py`, both defaulting
to unlimited so normal polling semantics don't change unless explicitly
asked for:

- `--since YYYY-MM-DD` — skip any filing whose SEC acceptance timestamp
  (`sec_acceptance_at`, not ingestion order — see the worker's own
  existing docstring note about why acceptance time is the correct clock
  here, not wall-clock ingestion time) is before this date.
- `--max-new-filings-per-company N` — stop ingesting new filings for a
  given CIK once N have been ingested in this invocation (already-
  ingested/deduplicated filings don't count against this; this caps how
  much *new* work one invocation does, not how many filings exist).

Both are explicit operator controls, not automatic heuristics — no
default cutoff, no "recent filings only" behavior unless one of these
flags is passed. Log a clear message when either limit causes a filing to
be skipped or a company's ingestion to stop early, so it's never a silent
truncation.

Add tests: `--since` correctly skips a filing before the cutoff and
includes one on/after it; `--max-new-filings-per-company` correctly stops
at N for one company while not affecting another company's ingestion in
the same invocation; both flags default to no limit when omitted (a
regression test against the existing "no cap" behavior, so this can't
silently change default behavior later).

## Explicitly not in scope for this

Nothing about the extraction step, `extraction_runner.py`, or
`llm_client.py` — this is purely the EDGAR polling stage. Don't touch
anything related to a filing-volume cap on the extraction side
(`--max-documents`/`--max-catalysts` on `extraction_runner.py`) — that's
a separate, later addition once there's an actual live extraction run to
scope, not needed for this ingestion-side fix.

## When you're done

Run the full suite, report the real pytest output (same discipline as
last round — actual captured output, not cache-file inference), and
confirm both flags behave as described with the new tests. I'll verify
against the actual code before we use these flags for anything.
