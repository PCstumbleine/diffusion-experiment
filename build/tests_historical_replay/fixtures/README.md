# Step 0 fixtures — Historical Replay Phase 1B

Captured 2026-09-08 against SEC's real, live submissions API, per
`specs/historical-replay-phase1b-implementation-spec-final.md` Section 0
— **before** `historical_edgar_ingest.py`'s strict validators or shard
adapter were written, not after (Step 0 is the mandatory first step of
that spec, not a retrofit).

- **Company:** Apple Inc., CIK `0000320193` — chosen specifically because
  its `filings.files` array is non-empty (a company with a long enough
  filing history that SEC has split its older submissions out into
  separate shard files).
- **Source:** `https://data.sec.gov/submissions/CIK0000320193.json`
  (top-level submissions JSON) and
  `https://data.sec.gov/submissions/CIK0000320193-submissions-001.json`
  (the one shard `filings.files` referenced).

## Files

- `step0_apple_submissions_minimal.json` — a real `filings.files`
  descriptor entry (`{"name": "CIK0000320193-submissions-001.json",
  "filingCount": 1246, "filingFrom": "1994-01-26", "filingTo":
  "2015-07-20"}`) plus the first 10 real `filings.recent` entries (to
  know the exact `acceptanceDateTime`/`filingDate`/`form` shapes
  surrounding it).
- `step0_apple_shard_001_minimal.json` — the first 20 real entries of the
  shard JSON that descriptor references, plus one additional real `8-K/A`
  entry (accession `0000320193-96-000025`, index 1203 in the full shard)
  appended explicitly, since the first 20 entries alone happened to
  contain `8-K` rows but no `8-K/A` row. Confirmed: the shard has the
  IDENTICAL parallel-array structure as `filings.recent` — same key
  names, same per-index alignment.

## What was frozen against these fixtures

- **`acceptanceDateTime`**: every one of 2246 real values checked (the
  full `filings.recent` array plus the full shard, not just what's kept
  in these minimal fixtures) matched exactly one serialization —
  `YYYY-MM-DDTHH:MM:SS.mmmZ` (3-digit milliseconds, literal trailing
  `Z`) — with zero exceptions. `parse_acceptance_datetime_strict` in
  `historical_edgar_ingest.py` accepts only this exact form.
- **`filingDate`**: every value matched `YYYY-MM-DD`, zero exceptions.
- **`filings.files` descriptor**: exactly the four keys `name` (str),
  `filingCount` (int), `filingFrom` (str), `filingTo` (str).
- **Shard JSON**: a dict with (at minimum) `accessionNumber`,
  `filingDate`, `acceptanceDateTime`, `form`, `primaryDocument` as
  parallel lists of equal length — the only keys `historical_edgar_ingest.py`
  actually consumes, out of the shard's real, larger key set
  (`reportDate`, `act`, `fileNumber`, `filmNumber`, `items`, `core_type`,
  `size`, `isXBRL`, `isInlineXBRL`, `isXBRLNumeric`,
  `primaryDocDescription` also exist in the real payload but are unused).

No field name, key name, or timestamp format anywhere in
`historical_edgar_ingest.py` was invented — every one was read directly
from these captured payloads first.
