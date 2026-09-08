# Historical Replay — Phase 1B Implementation Spec, FINAL

This document is self-contained and authoritative. It supersedes every earlier draft (v1–v2) and the round 1-6 design-discussion messages that preceded it. Where anything below conflicts with an earlier draft or discussion message, this document wins.

## 0. Step 0 — mandatory real-payload capture, before any adapter is written

Before implementing §5's strict validators or §6's shard adapter, using the project's real `EdgarClient` and normal (unrestricted) SEC network access:

1. Fetch one real CIK's submissions JSON for a company whose `filings.files` is non-empty.
2. Save a minimal fixture containing: one real `filings.files` descriptor entry plus enough surrounding structure to know its exact keys/types; one real `acceptanceDateTime` value with its exact raw serialization.
3. Fetch the shard JSON that descriptor references; save a minimal representative shard fixture.
4. Only after those fixtures exist: freeze the exact `filings.files` descriptor keys/types, freeze the exact shard payload shape, freeze the exact supported `acceptanceDateTime` serialization(s) — then implement §5's validators and §6's adapter against those observed, real forms, not invented ones.
5. **If SEC network access is unavailable when this is attempted: STOP and report the blocked empirical prerequisite. Do not invent placeholder field names, key names, or timestamp formats to "keep moving."** A spec-compliant implementation cannot proceed past this point without the real fixtures — that's a feature of this design, not a gap in it.

This step's output (the fixtures) becomes the required regression-test data in §8 — there is no synthetic-placeholder test anywhere in this spec; every strict-adapter test is built from what Step 0 actually captured.

## 1. Scope

Acquire a historical SEC filing into the replay database as a complete filing package that a later extraction phase can consume exactly as it consumes a live-ingested filing, while carrying historical (not observation-time) public-time provenance.

Phase 1B is complete for one filing when all of the following exist and are correctly linked:

```text
historical SEC filing
        |
raw_documents (primary document + retained exhibits/components,
               each with correct historical public-time provenance)
        |
catalysts (one row, originating_document_id = the primary document)
        |
catalyst_documents (primary + every retained exhibit, linked)
```

Phase 1B explicitly does **not** proceed into: `extraction_runs`, `extracted_events`, `event_versions`, `entity_relationships`, `candidate_signals`, `model_candidate_decisions`, `experiment_catalysts`, `arm_entries`/`arm_outcomes`. No LLM extraction, no relationship generation, no historical eligibility, no `decision_at`, no Arm A/Arm G, no returns. Those are separate, later phases.

## 2. Historical public-time provenance: filing-day civil-day policy (frozen)

For a supported form (§4), SEC's own `filingDate` determines the public-availability **day**; Phase 1B makes no claim about the exact dissemination minute within that day. The historical public-time interval spans the entire `filingDate` civil day in America/New_York:

```python
from zoneinfo import ZoneInfo
from datetime import date, datetime, time, timedelta, timezone

SEC_TZ = ZoneInfo("America/New_York")

def historical_public_time_interval(filing_date: date) -> tuple[datetime, timedelta]:
    """Returns (canonical_first_public_at, first_public_timestamp_precision)
    for a supported-form historical filing, per the frozen filing-day
    civil-day v1 policy. NOT a claim that EDGAR was actually uncertain for
    ~24 hours -- it's a deliberately conservative methodological bound:
    SEC gives us the disclosure day with certainty (filingDate is already
    next-business-day-adjusted at the source for after-cutoff submissions),
    but no source -- not the Filer Manual, not the webmaster FAQ, not the
    dissemination spec -- commits to an exact, guaranteed minute within
    that day for either the ordinary or the after-cutoff case."""
    lower_local = datetime.combine(filing_date, time.min, tzinfo=SEC_TZ)
    upper_local = datetime.combine(filing_date + timedelta(days=1), time.min, tzinfo=SEC_TZ)
    lower_utc = lower_local.astimezone(timezone.utc)
    upper_utc = upper_local.astimezone(timezone.utc)
    return upper_utc, (upper_utc - lower_utc)

FIRST_PUBLIC_TIMESTAMP_SOURCE = "sec_filing_date_civil_day_v1"
```

`canonical_first_public_at` is the upper bound (`upper_utc`); `first_public_timestamp_precision` is `upper_utc - lower_utc`. Because both bounds come from converting real Eastern local midnights through `zoneinfo` rather than adding a hardcoded `timedelta(days=1)`, the resulting precision is honestly DST-aware: an ordinary filing day produces exactly 24 hours, the spring-forward transition day produces 23 hours, and the fall-back transition day produces 25 hours. **This is intentional, not a bug** — it's the width of the actual Eastern civil day mapped into UTC. Do not "fix" this into a hardcoded `timedelta(hours=24)`; a comment at the function should say so explicitly.

`zoneinfo` is Python's stdlib (3.9+; this repo runs 3.12) — no new dependency, and no existing code in this repo uses timezone-aware Eastern-time conversion yet.

**Wording requirement for any documentation, log message, or comment describing this interval**: describe it as "the frozen conservative historical-replay uncertainty interval derived from SEC's authoritative filing day," never as "the exact period during which the filing became public." No SEC source states an exact historical first-public timestamp exists; this is a deliberate methodological choice to under-claim precision, not a report of genuine platform uncertainty.

## 3. Frozen invariants

1. **Accession-level provenance uniformity.** All SEC components belonging to one accession (the primary document and every retained exhibit) receive the *same* accession-level `(canonical_first_public_at, first_public_timestamp_precision, first_public_timestamp_source)` triple in Phase 1B v1 — each computed once from that accession's `filingDate`, then applied independently to each component's own `raw_documents` row. Do not synthesize a separate, different timestamp for an exhibit merely because it lives in its own row.
2. **Three clock classes stay distinct.** Raw SEC facts: `filing_date`, `sec_acceptance_at` (stored only if it passes strict validation — §5; NOT used to compute canonical/precision), `source_published_at` (remains NULL). Counterfactual public-time provenance: `canonical_first_public_at`, `first_public_timestamp_precision`, `first_public_timestamp_source = "sec_filing_date_civil_day_v1"` — per §2. Real pipeline clocks: `source_observed_at` and `ingested_at` remain semantically distinct fields, but Phase 1B v1 intentionally assigns both the **same single present-day observation instant for every document within one accession**, mirroring live `insert_raw_document`'s existing, already-reviewed fix #16 exactly (one shared instant per filing package, to prevent primary/exhibit timestamps drifting apart within the same discovery event). **Each separate call to `ingest_historical_filing` obtains a fresh instant** — so across different filings in one historical backfill run, these values naturally differ and grow later as the run progresses, with no special handling needed beyond calling the clock fresh per call. Never backdated to any historical date.
3. **Form gating is hard, and is checked before any state-dependent shortcut.** Only `"8-K"` and `"8-K/A"` are supported. `filing.form not in SUPPORTED_HISTORICAL_FORMS` raises `UnsupportedHistoricalSECFormError` as the **very first check** inside `ingest_historical_filing` — before the idempotence check (§3, invariant 6), before any provenance computation, before any persistence. **Unsupported form always raises, regardless of whether its accession is already present in the database.** A malformed/unsupported `Filing` object must not become silently acceptable merely because its accession happens to already exist — the function's input-validation contract cannot depend on database state. Form validation is local and costs nothing, so there is no efficiency argument for checking the database first.
4. **`filingDate` determines the disclosure day; `acceptanceDateTime`'s clock value never determines rollover.** SEC's cutoff rule keys off when transmission *begins*, not when EDGAR finishes accepting — a field the submissions API doesn't expose.
5. **Accession-level atomicity is required, and is reused, not reinvented — with rollback owned by the caller, exactly as today.** `ingest_historical_filing` performs all of its writes and calls `conn.commit()` exactly once, at the very end, after every write for that accession has succeeded. It does **not** call `conn.rollback()` itself on failure — mirroring `ingest_filing`'s existing contract exactly, where rollback-on-exception is the caller's responsibility (`poll_once`'s `try/except Exception: conn.rollback(); continue`). Whatever drives historical acquisition must wrap each `ingest_historical_filing` call in the same pattern. This is a single-commit-plus-caller-rollback contract, not "the function internally manages an entire transaction lifecycle" — do not have `ingest_historical_filing` catch its own exceptions and roll back internally.
6. **The idempotence guard is self-protecting, inside `ingest_historical_filing` itself**, checked immediately after the form gate (§3, invariant 3) — not merely in an outer caller loop, so a direct call to the function is idempotent on its own, matching its integration test (§8).
7. **Acquisition idempotence is accession/document identity, not date-range bookkeeping.** Do not assume `filings.recent` and `filings.files` shards are non-overlapping or contiguous.
8. **Content-hash duplicate flagging stays exactly as it is today.** `find_content_duplicate`/`duplicate_content_of_document_id` remains a global, informational cross-reference only (verified directly — never causes two distinct disclosure occurrences to share one row or one provenance pair).

## 4. Module architecture

```text
                       edgar_primitives.py
                      /                    \
                     /                      \
      edgar_ingest_worker.py          historical_edgar_ingest.py
             LIVE                            HISTORICAL
              |                                  |
   compute observed-time provenance   compute filing-day-civil-day provenance
              \                                  /
               \                                /
             insert_raw_document_record (in edgar_primitives.py)
```

### 4.1 New module: `edgar_primitives.py`

Moved from `edgar_ingest_worker.py`, verbatim (a "boring" refactor — move the implementation and update imports; do not opportunistically redesign signatures unrelated to Phase 1B):

- `EdgarClient` (whole class), `FilingPackageParseError`.
- `Filing`, `FilingDocument` dataclasses.
- `list_recent_target_filings` — both orchestrators need it, and it has zero live-vs-historical semantics (pure parsing of the already-fetched `filings.recent` structure). `edgar_ingest_worker.py` imports/re-exports the name so nothing else in that file needs to change.
- `list_filing_documents`, `source_url_for`, `find_existing_document`, `accession_already_ingested`, `find_content_duplicate`.
- `load_watchlist`, `filter_watchlist`, `link_to_catalyst`.
- `insert_raw_document_record(conn, cik, filing, doc, raw_text, sec_acceptance_at, canonical_first_public_at, first_public_timestamp_source, first_public_timestamp_precision, source_observed_at, ingested_at) -> str` — the neutral persistence primitive (today's `insert_raw_document`, split per Phase 1A's now-established pattern; no `historical: bool` mode flag).

**Staying in `edgar_ingest_worker.py`, untouched:** `parse_acceptance_datetime` (live's existing lenient parser; historical gets its own strict one, §5) and a thin live-specific wrapper computing `observed_at`-based provenance before calling `insert_raw_document_record`. `ingest_filing` and `poll_once` are otherwise unchanged.

### 4.2 New module: `historical_edgar_ingest.py`

- **Historical filing enumeration**: `list_recent_target_filings` (imported from `edgar_primitives.py`) plus a new shard-walker for `filings.files` (§6, built only after Step 0's fixtures exist).
- **`parse_acceptance_datetime_strict`**: the two-stage strict validator (§5), used only to populate the audited `sec_acceptance_at` field — never to compute `canonical_first_public_at`.
- **`utc_now()`**: a trivial, deliberate seam — `return datetime.now(timezone.utc)` — used everywhere this module needs the real current instant, so tests can `monkeypatch` it for deterministic timestamp assertions (§8) instead of relying on real wall-clock timing.
- **Historical wrapper**: computes the civil-day provenance triple, then calls `edgar_primitives.insert_raw_document_record(...)`.
- **`ingest_historical_filing(conn, client, cik, filing, entity_id=None) -> str | None`**:

  ```python
  def ingest_historical_filing(conn, client, cik, filing, entity_id=None) -> str | None:
      if filing.form not in SUPPORTED_HISTORICAL_FORMS:
          raise UnsupportedHistoricalSECFormError(...)  # ALWAYS checked first --
          # never short-circuited by an existing accession (invariant 3).

      if accession_already_ingested(conn, filing.accession_number):
          return None  # already complete -- no fetch, no write, matches
                       # ingest_filing's own return convention.

      historical_observed_at = utc_now()  # ONE instant, shared by every document
          # in THIS filing's package (mirrors live fix #16) -- a later call for a
          # later filing in the same backfill run naturally gets a later instant,
          # since utc_now() is called fresh per call, not once for the whole run.

      # fetch index, parse documents, insert primary via the historical wrapper,
      # create the catalyst, link primary, insert+link each exhibit --
      # mirroring ingest_filing's shape exactly.
      ...
      conn.commit()  # the ONLY commit; no internal rollback on failure (invariant 5)
                     # -- that is the CALLER's responsibility, exactly like
                     # poll_once's try/except conn.rollback() around ingest_filing.
      return catalyst_id
  ```

- The caller (a historical-equivalent driver, however Padraic wants to invoke this per-company) wraps each call in `try/except Exception: conn.rollback(); continue`, exactly matching `poll_once`'s existing pattern. `ingest_historical_filing` never calls `conn.rollback()` itself.

## 5. Runtime validation (strict, fail-closed, no fuzzy parsing, no invented formats)

Two-stage contract: (1) does the raw value match one of the explicitly supported serializations (established from Step 0's real fixtures, never invented)? If not, raise with the observed value, its type, and the expected forms. (2) If it matches, does that serialization explicitly carry the semantic information needed (e.g., timezone/offset)? If ambiguous, do NOT silently assume UTC/EST/America-New_York — raise instead. No `dateutil`-style best-effort parsing anywhere in this phase.

- **`acceptanceDateTime`**: `parse_acceptance_datetime_strict` raises `UnsupportedSECTimestampFormatError` (with `value`, `type(value)`, expected forms, accession number) for anything not matching the exact serialization(s) Step 0 actually captured. Cannot be finalized until Step 0 has run.
- **`filingDate`**: must parse as a valid ISO date; an unparseable value raises with the same detail.
- **`filings.files` shard descriptors** (§6): validated against the exact shape Step 0 captured; any mismatch raises `UnsupportedSECSubmissionsShapeError` with observed keys/types and CIK.
- **Form**: exactly `"8-K"` or `"8-K/A"`; anything else raises `UnsupportedHistoricalSECFormError` (§3, invariant 3 — checked first, unconditionally).

## 6. `filings.files` — older-history shard access

SEC's own documentation (verified directly): `filings.recent` holds at least one year or 1,000 filings; `filings.files` is an array of references to additional JSON files, each covering a stated date range. The exact object keys are established only from Step 0's real fixture, never guessed. Implementation must: fetch each shard via `EdgarClient`; validate its shape against the strict adapter built from Step 0's fixture before extracting anything; not assume non-overlapping date ranges (§3, invariant 7) — rely on `accession_already_ingested` for correctness.

## 7. Non-goals

```text
✗ extraction_runs / extracted_events / event_versions
✗ entity_relationships / candidate_signals / model_candidate_decisions
✗ experiment_catalysts / arm_entries / arm_outcomes
✗ any LLM extraction call against a historical document
✗ historical eligibility, decision_at, Arm A/Arm G scoring, returns
✗ minute-level historical public-time precision (§2 is filing-day only)
✗ any change to ingest_filing/poll_once's existing live behavior
✗ a general "any SEC form" historical timestamp helper
✗ inventing acceptanceDateTime formats or filings.files key names when
  Step 0's real-payload capture is unavailable -- STOP and report instead
✗ ingest_historical_filing catching its own exceptions / rolling back
  internally -- rollback is the caller's job (invariant 5)
```

## 8. Required tests

- `historical_public_time_interval`: ordinary day → 24h precision; a real spring-forward date → 23h; a real fall-back date → 25h; `canonical_first_public_at` always the upper bound.
- Form gate, unconditional: `"8-K"`/`"8-K/A"` accepted; `"10-K"`, `"4"`, trailing-space/lowercase variants, `None` all raise `UnsupportedHistoricalSECFormError` before any DB read or write. **Required variant**: an unsupported-form `Filing` whose accession is *already present* in the database (e.g., ingested once with a supported form, or pre-seeded directly) still raises on a second call with an unsupported form — proving the form gate is never short-circuited by `accession_already_ingested` returning true first.
- `parse_acceptance_datetime_strict` and the `filings.files` adapter: built from Step 0's real captured fixture (valid case) plus deliberately malformed derivatives of that same real fixture (missing key, wrong type, no offset) — no synthetic-from-scratch/placeholder-key payloads.
- **Accession-level atomicity, with the caller's rollback made explicit in the test itself**:

  ```python
  with pytest.raises(SimulatedFailure):
      ingest_historical_filing(conn, client, cik, filing)

  conn.rollback()  # mirrors the production caller's contract (poll_once's own
                    # except-block rollback) -- ingest_historical_filing does NOT
                    # roll back internally (invariant 5), so the test must, exactly
                    # like a real caller would, before issuing further queries on
                    # this same connection (an aborted Postgres transaction
                    # rejects further statements until rolled back).

  # now assert nothing survived
  assert count of raw_documents/catalysts/catalyst_documents for this accession == 0

  # remove the simulated failure condition
  catalyst_id = ingest_historical_filing(conn, client, cik, filing)
  assert catalyst_id is not None
  ```

- Accession-level provenance uniformity: a package with a primary + two exhibits — all three rows share the identical provenance triple.
- Full-package linkage: one `catalysts` row, `catalyst_documents` has one row per retained document with correct `document_role`.
- Idempotence: calling `ingest_historical_filing` directly, twice, for the same accession — exactly one package results, second call returns `None` without fetching or writing anything.
- **`source_observed_at`/`ingested_at`, deterministic (no real-clock reliance)**: `monkeypatch` `utc_now()` to return a fixed value for the first `ingest_historical_filing` call and a later fixed value for the second; assert every document in package 1 has the first fixed instant, every document in package 2 has the second fixed instant, and the second is strictly greater than the first — proving a fresh instant is acquired per call (not one instant reused across the whole backfill run), without depending on real wall-clock timing or sleeps.
- All database-touching tests use Phase 0's `disposable_db_name` fixture; none touch the real `diffusion_experiment` database.
