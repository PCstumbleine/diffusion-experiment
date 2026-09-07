# Historical Replay — Phase 0 Implementation Spec, FINAL

This document is self-contained and authoritative. It supersedes every
earlier draft (v1–v7) discussed during review; if anything you find
elsewhere (chat history, an earlier draft, `historical-replay-roadmap.md`'s
own sketch of Phase 0) conflicts with this document, this document
controls. Every design decision below — the physical-database-separation
architecture, `database_metadata`, the forward-purpose guards, the
`schema_migrations` manifest and checksum discipline, the fresh/legacy/
tracked state machine and its atomic transactions, the literal
per-migration legacy-adoption checklist, and the one-time human-performed
rollout sequence — went through seven rounds of independent review
(ChatGPT plus direct verification against the real repository and, where
claims were checkable, empirical tests against a real local Postgres
server) before being frozen here. Nothing in this document is a
placeholder or an open question left for the implementer to resolve.

*Phase 0 of `historical-replay-roadmap.md`. Builds the database-safety
infrastructure the whole historical-replay effort depends on: one shared
connection-config point, a real migration-tracking table, a
database-identity check wired into every write entry point, and a
bootstrap/verify script that makes the same expected migration lineage an
executable, checksummed fact instead of a manually-repeated README
procedure.*

Revised after round 4 of review: two more genuine blockers fixed, both
verified directly rather than accepted on description alone. First, the
fresh-vs-legacy state machine had a real gap — a database that exists but
crashed before step "001" was recorded would have been indistinguishable
from the real legacy database and wrongly refused; fixed with an explicit
state check (fresh-empty / legacy-untracked / tracked) and atomic
per-step transactions, which also proves one seemingly-possible fourth
state can't actually occur. Second, the `CREATE DATABASE` connection
pattern from the previous draft was reproduced failing exactly as
described, against a real local Postgres 16 server with the actual
psycopg2 version installed for this project (2.9.12) — `with
psycopg2.connect(...) as conn:` starts an implicit transaction even with
`autocommit=True`, and the fix was verified to succeed on the same
server. Also fixed: `applied_at` renamed to `recorded_at` (it isn't
truthful for an adopted row), and migration "008" plus the
`database_metadata` purpose row are now one atomic transaction, so a
successfully recorded "008" guarantees the identity row exists too.

Revised after round 5 of review: the contiguous-prefix migration gap is
now fixed (Section 6) — "TRACKED -> apply any step not yet recorded"
previously never checked that the recorded set was actually a contiguous
prefix of the manifest, which would have let a database with a hole in
its history (e.g. `001,002,004,005`, missing `003`) silently apply `003`
out of order; this round adds the explicit validation and, while there,
softens last round's overclaim that a tracked-but-empty
`schema_migrations` table "cannot actually occur" — it's only unreachable
via this tooling's own valid operations, not impossible in general, and
is still treated as a fail-closed error rather than assumed away. Also
fixed: the legacy-adoption checklist's `"..."` placeholder is now a
literal, per-migration list of concrete structural checks for
`"002"`-`"007"` (extracted directly from each migration file, not
invented), so Claude Code isn't left to decide what counts as sufficient
evidence; the rollout sequence (Section 6) is reworded to say explicitly,
not just imply, that Claude Code's own testing never touches the real
`diffusion_experiment` database and never runs the existing full suite
against it — that step is reserved for Padraic, by hand, after he
completes adoption and `--verify`; and `--adopt-existing` is hardened to
accept only `--purpose forward`, since a historical-replay database is
always created fresh and never adopted from an existing legacy database.

Revised after round 6 of review: a real blocker in the TRACKED path,
found by tracing a concrete scenario rather than abstractly — a
fully-migrated TRACKED database (all eight steps recorded) with a
mismatched `--purpose` would have fallen straight through the
contiguous-prefix/checksum checks to a silent, purpose-blind success,
directly contradicting Section 10's own "re-running with a different
--purpose fails loudly" requirement. Fixed by running
`assert_database_purpose` explicitly whenever "008" is already recorded,
before reporting anything, plus a fail-closed check that
`database_metadata` can't already exist while "008" is still unrecorded.
Also fixed: the legacy-adoption checklist's "001" step claimed 25 tables
but listed 26 — re-verified directly against `schema.sql` (26 is
correct) and the prose count removed entirely so it can't drift from the
list again; `FRESH_EMPTY` now checks for any user-created relation
(views, materialized views, sequences, and foreign tables, not just
`pg_tables`' plain tables) so a database holding only a view or a
sequence is no longer misclassified as empty; and the Required-tests
item claiming the test suite could assert "Claude Code never runs the
real 287-test suite against diffusion_experiment" is replaced with an
enforceable version — a fixture that generates a disposable database
name and hard-fails if it ever resolves to `diffusion_experiment` — since
no test can observe which external command a human or an agent chooses
to type.

Revised after round 7 of review — two final, small, mechanical freezes,
both closing places where the spec described the right behavior in prose
but never pinned it down precisely enough to stop an implementer from
filling the gap differently. First, `--verify` (Section 8) listed what it
checks but never said it must never mutate anything — since
`bootstrap_database.py` is one script that also owns creation, migration,
and adoption, this is now explicit: `--verify` never creates the
database, never creates `schema_migrations`/`database_metadata`, never
applies a migration, never writes a row, never adopts, never repairs —
it only ever observes and exits non-zero on any problem. Second, the
checksum's input was described as "the exact bytes" without saying so
literally; it is now frozen as exactly
`hashlib.sha256(path.read_bytes()).hexdigest()`, no newline
normalization, no stripping, no re-encoding — so even a whitespace-only
edit to an already-recorded migration file is intentionally detected as
drift, matching the project's own "never edit an applied migration, add a
new one" rule. Both rounds of review (ChatGPT and independent
verification against the actual spec text) agree no further architectural
or methodological issue remains — this draft is ready to hand to Claude
Code.

## 0. Scope and non-goals

- Does not change `classify_relationship_eligibility`, `decision_at`
  capture, or any other live extraction/eligibility/decision semantics.
- Does not implement historical eligibility, `L_decision`, or
  `evidence_public_time_precision` semantics — that's Phase 1/2.
- Does not create a second, populated historical-replay database with
  real data in it — this phase only makes it possible to safely create
  one.
- **Does add a fail-closed database-purpose guard to forward write entry
  points**, and **does require one explicit, human-performed adoption
  step against the real existing database** (Section 6) before those
  guards are live against it. Both are intentional, called out here, not
  discovered mid-implementation.

## 1. `db_config.py` — one shared connection-config point, with an honest default

Verified: `DB_DSN` is currently a separately hardcoded literal in three
files, already inconsistent —

```python
# edgar_ingest_worker.py:263
DB_DSN = "dbname=diffusion_experiment"
# extraction_runner.py:138 and seed_entities.py:55
DB_DSN = "dbname=diffusion_experiment user=postgres"
```

— and `manual_resolve.py` imports `DB_DSN` from `extraction_runner`
rather than declaring its own.

Verified which default is actually already load-bearing, rather than
guessing: `extraction_runner.py`, `seed_entities.py`, and the entire test
suite (`tests/conftest.py`'s `DB_DSN`, and every individual test file
that declares its own, e.g. `test_edgar_ingest_worker.py`) all already
hardcode `"dbname=diffusion_experiment user=postgres"` — the value 287
real, currently-passing tests already run against. `edgar_ingest_worker.py`'s
bare variant is the outlier nothing currently tests. Standardizing on
`user=postgres` matches what's already empirically working — but it is a
real, acknowledged behavior change for `edgar_ingest_worker.py`
specifically, not claimed as a non-change.

```python
"""db_config.py -- the one place a database connection string is decided.

The DSN is routing information ONLY. It is never treated as proof of
which database (forward vs. historical_replay) a connection actually
reaches -- that is database_metadata's job (Section 2), checked from
inside the database itself."""

import os

_DEFAULT_DSN = "dbname=diffusion_experiment user=postgres"  # standardized:
# matches extraction_runner.py, seed_entities.py, and the entire existing
# test suite -- NOT edgar_ingest_worker.py's previous bare-DSN default,
# which nothing currently tests. Deliberate, acknowledged behavior change
# for edgar_ingest_worker.py.


def get_db_dsn() -> str:
    """DIFFUSION_DB_DSN environment variable if set, otherwise the
    standardized default above. A historical-replay run MUST set
    DIFFUSION_DB_DSN explicitly -- there is no separate backtest default."""
    return os.environ.get("DIFFUSION_DB_DSN", _DEFAULT_DSN)
```

`edgar_ingest_worker.py`, `extraction_runner.py`, and `seed_entities.py`
each replace their hardcoded literal with `import db_config; DB_DSN =
db_config.get_db_dsn()`, keeping the module-level name `DB_DSN` unchanged
so `manual_resolve.py`'s `from extraction_runner import DB_DSN` keeps
working with no change to that file.

## 2. `database_metadata` — a shared, migration-created singleton table

```sql
CREATE TABLE database_metadata (
    singleton_key     TEXT PRIMARY KEY DEFAULT 'singleton',
    database_purpose  TEXT NOT NULL CHECK (database_purpose IN ('forward', 'historical_replay')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT database_metadata_is_singleton CHECK (singleton_key = 'singleton')
);
```

Created by migration `"008"` (Section 6), applied identically to every
database; only the one row's value differs. `db_config.py`:

```python
class DatabasePurposeError(Exception):
    """Missing table, zero rows, more than one row, an unrecognized
    value, or a query error -- every case fails closed, never falls back
    to trusting the DSN string used to reach this connection."""


def assert_database_purpose(conn, expected_purpose: str) -> None:
    if expected_purpose not in ("forward", "historical_replay"):
        raise ValueError(f"assert_database_purpose: unknown expected_purpose={expected_purpose!r}")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT database_purpose FROM database_metadata")
            rows = cur.fetchall()
    except Exception as exc:
        raise DatabasePurposeError(
            f"could not read database_metadata to confirm this connection is a "
            f"{expected_purpose!r} database -- failing closed: {exc}"
        ) from exc
    if len(rows) != 1:
        raise DatabasePurposeError(
            f"database_metadata has {len(rows)} row(s), expected exactly 1."
        )
    actual_purpose = rows[0][0]
    if actual_purpose != expected_purpose:
        raise DatabasePurposeError(
            f"this connection's database_metadata.database_purpose={actual_purpose!r}, "
            f"expected {expected_purpose!r} -- refusing to proceed."
        )
```

## 3. Forward-purpose guards, wired now — an inventory of every write entry point

Rule applied per entry point:

```text
forward-only writer          -> require "forward"
historical-only writer       -> require "historical_replay" (none exist yet -- Phase 2)
environment-neutral utility  -> expected purpose must be passed explicitly by its caller
```

`edgar_ingest_worker.py` and `extraction_runner.py` call
`assert_database_purpose(conn, "forward")` once, immediately after
opening their connection, before any write. `seed_entities.py` and
`manual_resolve.py` are environment-neutral (verified: both already
accept `--dsn` as an explicit argument) — both gain a required `--purpose
{forward,historical_replay}` argument and call
`assert_database_purpose(conn, args.purpose)` themselves, rather than a
silent `"forward"` default that would block a legitimate future Phase 2
use of either utility against the replay database.

**These guards are only meaningful once the real forward database has
been through the one-time adoption in Section 6 — see the rollout
sequence there before treating this section as independently deployable.**

## 4. `schema_migrations` — with checksum and honest provenance

```sql
CREATE TABLE schema_migrations (
    version          TEXT PRIMARY KEY,
    checksum_sha256  TEXT NOT NULL,
    record_origin    TEXT NOT NULL CHECK (record_origin IN ('applied', 'legacy_adopted')),
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**`recorded_at`, not `applied_at`.** For a `'legacy_adopted'` row,
`now()` is when the tooling recorded the migration's already-existing
effects, not when that migration was actually, historically applied —
which is genuinely unknown and not claimed. `recorded_at` is semantically
correct for both origins: for `'applied'`, it's the same transaction that
executed the SQL; for `'legacy_adopted'`, it's honestly just adoption
time.

`version` stores the stable logical id from `MIGRATION_STEPS_IN_ORDER`
(Section 6 — `"001"` through `"008"`), never a filename or path, so
renaming a file on disk later can't silently orphan its history.

Each migration's application and its record are one atomic unit:

```text
BEGIN
    apply migration file's SQL
    INSERT INTO schema_migrations (version, checksum_sha256, record_origin)
    VALUES (version, sha256_of_file_about_to_be_executed, 'applied')
COMMIT
-- on any failure: ROLLBACK -- never leaves "applied but unrecorded" or
-- "recorded but not actually applied"
```

Verified safe to wrap every migration file (and `schema.sql`) in a single
transaction: grepped all of `schema.sql` and every `migrations/*.sql`
file for `CONCURRENTLY`, `VACUUM`, and `CREATE DATABASE` — zero matches.

**Checksum input, frozen literally, not left as "the exact bytes" for
Claude Code to interpret:**

```python
checksum_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
```

Exactly that — the raw bytes of the file on disk, nothing else. No
newline normalization (`\r\n` vs `\n`), no `.strip()`, no
encode/decode round-trip, no SQL parsing or comment stripping. A change
to only whitespace or line endings in an already-recorded migration file
is therefore intentionally detected as drift by `--verify`, exactly like
any other edit — consistent with the "never edit an applied migration
file, add a new numbered one instead" discipline this whole checksum
mechanism exists to enforce. This applies identically to both
`record_origin` values (Section 6): `'applied'` hashes the file about to
be executed; `'legacy_adopted'` hashes the file adopted as that version's
baseline at adoption time — same function, same rule, only the meaning of
the resulting number differs (already covered above).

**`record_origin = 'legacy_adopted'` is a different, weaker claim than
`'applied'`, and the spec must not blur them.** For a normal bootstrap,
`checksum_sha256` is computed from the exact bytes about to be executed —
a true, first-hand fact. For `--adopt-existing` (Section 6), there is no
way to prove the database's current structure was actually produced by
executing today's checked-out file, byte for byte, at some point in the
past — only that the database's observable structure is consistent with
having gone through it. So for an adopted row, `checksum_sha256` is
defined explicitly as *the checksum of the migration file adopted as this
version's baseline at adoption time* — not a claim of historical
execution. `--verify` still gets its useful property from this (a later
edit to that same file, compared against the checksum recorded at
adoption time, is still detected as drift), just without manufacturing
provenance the tooling can't actually have. The migration-specific
structural checks performed during adoption (Section 6) are what actually
justify accepting the database, not the checksum.

## 5. Migration manifest — includes `schema.sql` as `"001"`, by logical id, not by path

**Blocker from round 2, confirmed by direct inspection of this draft: the
prior manifest omitted `schema.sql` entirely while the bootstrap logic
elsewhere recorded it — meaning `schema_migrations` would end up
containing a `"schema.sql"` entry that Section 7's `--verify` would then
flag as "unexpected," failing on every single correctly bootstrapped
database, including a fresh one that just went through normal bootstrap
with zero manual steps.** Fixed by making `schema.sql` a first-class,
numbered step in the same manifest everything else uses:

```python
MIGRATION_STEPS_IN_ORDER = [
    ("001", "schema.sql"),
    ("002", "migrations/002_extraction_runner.sql"),
    ("003", "migrations/003_extraction_runner_fixes.sql"),
    ("004", "migrations/004_range_valued_guidance.sql"),
    ("005", "migrations/005_relationship_deferral_observability.sql"),
    ("006", "migrations/006_confirmatory_outcome_contract.sql"),
    ("007", "migrations/007_experiment_catalysts.sql"),
    ("008", "migrations/008_database_metadata.sql"),
]
```

Every operation — bootstrap, adoption, checksum computation, and
`--verify` — uses this one manifest and records/checks by logical id
(`"001"`, not `"schema.sql"` or a full path). This directly fixes the
blocker: `schema.sql` is now an expected, first-class entry, not an
extra one.

## 6. Database creation, the real state machine, and the one-time adoption of the real existing database

**Blocker, confirmed by inspection: the prior draft's "if not recorded,
apply" logic and its "missing/empty schema_migrations -> refuse, demand
--adopt-existing" rule collide on a real, non-exotic case.** If
`CREATE DATABASE` succeeds and the process then dies before step `"001"`
is applied and recorded, the next run finds a database that exists with
no `schema_migrations` table — indistinguishable, under the prior rule,
from the real legacy `diffusion_experiment` database, which also has no
`schema_migrations` table but is very much NOT empty. Refusing a
genuinely empty, just-created database and demanding `--adopt-existing`
against it would be wrong.

**Fixed with an explicit state check, run before any mutation, that
distinguishes "empty" from "legacy" by looking for actual content, not
just for the tracker table:**

```text
does schema_migrations exist?
    yes -> TRACKED. See the contiguous-prefix rule below before applying
           anything.
    no  -> does the database contain ANY user-created relation in a
           non-system schema -- not just tables, since a bare
           `SELECT count(*) FROM pg_tables` misses views, materialized
           views, sequences, and foreign tables, any one of which would
           wrongly let a non-empty database through as "fresh":

               SELECT count(*) FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE c.relkind IN ('r','p','v','m','S','f')  -- ordinary/
                     -- partitioned tables, views, materialized views,
                     -- sequences, foreign tables
                 AND n.nspname NOT IN ('pg_catalog','information_schema')
                 AND n.nspname !~ '^pg_toast'

        zero  -> FRESH_EMPTY. Proceed to bootstrap step "001" from zero
                 (see the atomic sequence below -- this is what actually
                 creates schema_migrations, so it does not exist as a
                 separate, earlier step or a fake "000" migration).
        >zero -> LEGACY_UNTRACKED. REFUSE normal bootstrap. Print the
                 exact corrective command: `--adopt-existing --database ...
                 --purpose ...`.
```

This is not a general forensic object inventory -- just enough relation
kinds that "no tables" can no longer be mistaken for "empty" when the
database actually holds a view or a sequence.

Through the valid atomic bootstrap path below, creating `schema_migrations`
and recording `"001"` happen in one transaction — verified empirically
(Postgres's DDL is transactional; a `CREATE TABLE` inside a transaction
that later rolls back leaves no trace of the table at all, confirmed
directly against a real server) — so `schema_migrations` existing is
proof `"001"` was recorded, *through that path*. **That does not make
"tracker exists but empty" impossible in general — only unreachable via
this tooling's own valid operations.** Manual/out-of-band table creation
or a corrupted database could still produce it, and this is a
fail-closed system: treat it as invalid, not as a case ruled out by
assumption.

**Blocker, confirmed by inspection: "TRACKED -> apply any step not yet
recorded" doesn't verify the recorded set is a contiguous prefix of the
manifest.** A database recording `001, 002, 004, 005` but missing `003`
would silently let bootstrap apply `003` after `004`/`005` already ran —
exactly the ordering violation the manifest exists to prevent. Frozen
validation, required before any further step is applied to a TRACKED
database:

```python
expected = [version for version, _path in MIGRATION_STEPS_IN_ORDER]
recorded = versions_from_schema_migrations_in_manifest_order()

if not recorded:
    # Table exists but has zero rows -- unreachable via this tooling's
    # own atomic operations (see above), so this means manual/out-of-band
    # interference or corruption. FAIL CLOSED, not "impossible."
    raise MigrationStateError("schema_migrations exists but is empty -- refusing to guess")

if any(version not in expected for version in recorded):
    raise MigrationStateError(f"schema_migrations contains a version not in the manifest: {recorded}")

if recorded != expected[: len(recorded)]:
    raise MigrationStateError(
        f"recorded migration history {recorded} is not a contiguous prefix of {expected} -- "
        "refusing to apply anything until this is understood, not guessing at an order."
    )

verify_checksums_for(recorded)  # Section 4/8's existing checksum check, run here too

if "008" in recorded:
    # Every migration is already applied -- there is nothing left to
    # apply_in_order below. Without this check, running normal bootstrap
    # with a MISMATCHED --purpose against a fully-migrated TRACKED
    # database (e.g. --purpose forward against a database whose
    # database_metadata already says historical_replay) would fall
    # straight through to a silent, purpose-blind success -- the exact
    # opposite of the "re-running with a different --purpose fails
    # loudly" requirement in Section 10. Run the SAME check Section 3's
    # forward-purpose guards use, here, before reporting anything.
    assert_database_purpose(conn, args.purpose)  # raises DatabasePurposeError on mismatch
else:
    # "008" not yet recorded. Through the valid atomic path, database_metadata
    # is created ONLY together with "008" being applied and recorded (see the
    # atomic sequence below) -- so database_metadata existing here, before
    # that transaction has ever run, is impossible via this tooling's own
    # operations and therefore a corruption/out-of-band signal, exactly like
    # the "recorded set is empty" case above. FAIL CLOSED, don't guess.
    if database_metadata_table_exists(conn):
        raise MigrationStateError(
            "database_metadata already exists but \"008\" is not yet recorded in "
            "schema_migrations -- inconsistent state, refusing to guess which is right"
        )

apply_in_order(expected[len(recorded):])
# When this reaches "008" (fresh case) or is a no-op (008 already recorded,
# purpose already checked above), the atomic sequence below is what actually
# creates database_metadata, inserts args.purpose, and records "008" --
# never this validation block.
```

Healthy tracked states are therefore only `["001"]`, `["001","002"]`, ...,
through the full eight-step list — never a set with a hole. And a TRACKED
database with "008" already recorded is never treated as a successful
no-op unless its `database_metadata.database_purpose` actually matches
`args.purpose` — verified, not assumed, on every run, not only at
creation time.

**Legacy-adoption checklist — literal, per-migration, no `"..."`.** Round 5
requirement: no migration `"001"`-`"007"` may be baselined solely because
an earlier or later sentinel object exists (e.g. finding `"007"`'s table
does not excuse checking `"004"`). Every version recorded with
`record_origin='legacy_adopted'` must have its own explicitly enumerated
structural check pass, run read-only, entirely before the adoption
transaction opens. This is not a general schema-fingerprinting framework —
just this fixed list, extracted directly from each migration file:

```text
"001" (schema.sql): every table in the following authoritative list
    exists (deliberately no count stated in prose here -- a prose count
    next to a literal list is exactly the kind of thing that silently
    drifts out of sync with the list itself, which is what happened in
    round 5's own draft; re-verified directly against schema.sql for this
    round: 26 tables, not 25) --
    raw_documents, catalysts, catalyst_documents, canonical_events,
    event_versions, event_document_links, surprise_transform_registry,
    extracted_events, entities, instruments, instrument_identifiers,
    corporate_actions, event_entities, entity_relationships,
    underreaction_estimates, candidate_signals,
    candidate_supporting_relationships, model_candidate_decisions,
    experiments, experiment_arms, arm_entries, arm_outcomes,
    quote_snapshots, market_data, document_embeddings, audit_log.

"002": entity_aliases, watchlist_membership, extraction_runs, and
    unresolved_entity_mentions tables all exist; entities has the unique
    index idx_entities_cik_unique (not just the old non-unique
    idx_entities_cik); entity_relationships.extraction_run_id column
    exists and is NOT NULL.

"003": extraction_runs.cleaned_llm_output and
    extraction_runs.validation_drop_log columns exist;
    extracted_events.extraction_run_id column exists and is NOT NULL;
    catalyst_processing_runs table exists (composite primary key on
    catalyst_id, extraction_prompt_version, extractor_model_id,
    extractor_model_version); catalysts.canonicalization_completed_at
    does NOT exist (migration 003 drops the column migration 002 added --
    its continued presence means 003 was never actually applied, not that
    it was).

"004": extracted_events.observed_value_low, observed_value_high,
    reference_value_low, and reference_value_high columns all exist.

"005": catalyst_processing_runs.processing_issues column exists.

"006": arm_outcomes has entry_price_source, exit_price_source, entry_fee,
    exit_fee, return_method_version, fee_method_version, and exit_reason
    columns, all NOT NULL.

"007": experiment_catalysts table exists (composite primary key on
    experiment_id, catalyst_id); both
    trg_check_experiment_catalyst_epoch_consistency and
    trg_forbid_experiment_catalyst_mutation triggers exist on it.
```

If any single one of these checks fails, adoption refuses entirely --
never a partial adoption that records some versions and skips others.

**Atomic sequences, frozen:**

```text
Fresh bootstrap of step "001" (only ever runs from FRESH_EMPTY):
BEGIN
    CREATE TABLE schema_migrations (...)
    apply schema.sql's SQL
    INSERT INTO schema_migrations VALUES ('001', checksum, 'applied', now())
COMMIT
-- schema_migrations existing after this point is proof "001" succeeded;
-- any failure leaves the database exactly as empty as it started.

Fresh bootstrap of step "008" (forward OR replay -- both are fresh here),
run after "001"-"007" are each individually applied-and-recorded:
BEGIN
    apply migration "008"'s SQL (creates database_metadata's TABLE only)
    INSERT INTO database_metadata (singleton_key, database_purpose)
      VALUES ('singleton', :purpose)
    INSERT INTO schema_migrations VALUES ('008', checksum, 'applied', now())
COMMIT
-- a successfully recorded "008" now GUARANTEES the purpose row exists
-- too -- no window where "008 recorded" and "database_metadata still
-- empty" can both be true.

Legacy adoption (only ever runs from LEGACY_UNTRACKED, after structural
verification of "001"-"007"'s expected effects has ALREADY succeeded,
outside any transaction, since it's read-only inspection):
BEGIN
    CREATE TABLE schema_migrations (...)
    INSERT seven rows for "001".."007", record_origin='legacy_adopted',
      checksum = each currently-checked-out file's sha256 (Section 4's
      honest definition -- not a claim of historical execution)
    apply migration "008"'s SQL
    INSERT INTO database_metadata (singleton_key, database_purpose)
      VALUES ('singleton', :purpose)
    INSERT INTO schema_migrations VALUES ('008', checksum, 'applied', now())
COMMIT
-- if verification (before this transaction) found the baseline didn't
-- match, this transaction never starts -- FAIL CLOSED, nothing mutated.
-- if anything inside this transaction fails, the legacy database is left
-- completely unchanged, never half-adopted.
```

```text
Usage:
  python3 bootstrap_database.py --database diffusion_experiment --purpose forward
      target does not exist  -> create it (Section 7's frozen connection
                                 mechanism), then FRESH_EMPTY path above
      target exists          -> run the state check above; TRACKED
                                 continues migrating forward, FRESH_EMPTY
                                 proceeds normally, LEGACY_UNTRACKED refuses
                                 with the exact --adopt-existing command

  python3 bootstrap_database.py --adopt-existing --database diffusion_experiment --purpose forward
      Valid ONLY when the state check finds LEGACY_UNTRACKED (refuses on
      TRACKED; refuses on FRESH_EMPTY -- an empty database is never
      "adopted," it's bootstrapped normally). Runs the full literal
      per-migration checklist above for "001"-"007" BEFORE the adoption
      transaction ever opens. If that verification can't confirm the
      expected baseline for even one step, FAIL CLOSED and mutate nothing.

      `--purpose` accepts ONLY `forward` here -- `--adopt-existing --purpose
      historical_replay` is rejected outright, before the state check even
      runs. A historical-replay database is always created fresh (see the
      third usage form below); adoption exists specifically to bring the
      one real, already-populated, currently-forward-purpose
      `diffusion_experiment` database under tracking, and nothing about a
      populated legacy database's history makes it a legitimate replay
      database -- this closes off relabeling an arbitrary populated
      database as `historical_replay` through the adoption path.

  python3 bootstrap_database.py --database diffusion_experiment_replay --purpose historical_replay
      Fresh database, doesn't exist yet -> FRESH_EMPTY path, every step
      record_origin = 'applied', tracked from birth. Never uses
      --adopt-existing.
```

**Frozen one-time rollout sequence — an operational order, not just an
implementation detail, precisely because the real, currently-in-use
`diffusion_experiment` database is what Section 3's guards would
otherwise immediately break against:**

```text
A. Claude Code implements db_config.py, the manifest, schema_migrations,
   migration "008", bootstrap_database.py (create / migrate / adopt /
   verify), and their tests -- entirely against disposable, throwaway
   Postgres databases created and destroyed by the test suite itself.

B. Before Padraic performs step C adoption, Claude Code's own testing is
   bounded exactly like this -- not "don't misuse --adopt-existing," the
   whole verification process for this phase:
     - Claude Code runs ONLY the new Phase 0 tests (Section 10, all of
       them against disposable databases the tests themselves create and
       tear down) to confirm its own work.
     - Claude Code MUST NOT run the existing full DB-backed test suite
       (the 287 tests already in the repo, or any subset of them) against
       the real diffusion_experiment database, at any point during
       implementation or its own verification.
     - Claude Code MUST NOT adopt, mutate, rename, recreate, or substitute
       the real diffusion_experiment database for any reason, including
       to make a test pass, including --adopt-existing itself -- that
       command is never invoked by a test, by a script Claude Code runs
       automatically, or by Claude Code's own verification pass, against
       that database or its DSN, under any circumstance.
     - If a test appears to require touching the real database to pass,
       that is a sign the test is wrong, not a reason to run it against
       diffusion_experiment -- stop and flag it rather than working
       around it.

C. Padraic performs the one-time adoption himself, explicitly, once
   Phase 0's code is reviewed and merged -- this is the ONLY point in the
   whole rollout where the real database is touched:
     1. back up / checkpoint the existing diffusion_experiment database
     2. python3 bootstrap_database.py --adopt-existing --database diffusion_experiment --purpose forward
     3. python3 bootstrap_database.py --verify --database diffusion_experiment --purpose forward

D. Only after step C succeeds are the Section 3 forward-purpose guards
   considered live/deployable against the real database.

E. After step C succeeds, Padraic (not Claude Code, and not as part of
   Claude Code's own implementation/verification pass in step A/B) runs
   the full existing test suite (287 tests and counting) against the
   now-adopted, now-tracked diffusion_experiment database, to confirm
   nothing regressed.
```

This sequence is deliberately more explicit than "use disposable databases
in tests" (Section 9/10 already required that): it draws the line at
exactly who is allowed to touch the real `diffusion_experiment` database
and when, so Claude Code cannot read step B as license to run the
existing suite against the real database itself, or to invoke
`--adopt-existing` as part of its own verification "just to check the
guards work end to end." Adoption of the *real* database is a deliberate,
human-performed action that happens once, by Padraic, outside of
automated testing, never as a side effect of implementing or testing this
spec.

## 7. Bootstrap's own database connection — frozen, not left to Claude Code to invent

**A remaining gap: "Python/psycopg2 against the postgres maintenance
database" doesn't say where that server even is.** `--database <name>`
only names a database, not a host/port/user/credential. Left unfrozen,
Claude Code could hardcode a second, separate connection template (e.g.
`psycopg2.connect("dbname=postgres user=postgres")`), silently
recreating the exact fragmented-configuration problem this phase exists
to eliminate.

**Frozen:** `db_config.get_db_dsn()` is the connection *template* — host,
port, user, and any other connection parameter come from it. Only
`dbname` is overridden, using psycopg2's own DSN parsing rather than
string surgery:

```python
import psycopg2.extensions

base_params = psycopg2.extensions.parse_dsn(db_config.get_db_dsn())

maintenance_params = {**base_params, "dbname": "postgres"}
target_params = {**base_params, "dbname": args.database}
```

`CREATE DATABASE` uses `psycopg2.sql.Identifier` for the database name —
never an f-string / `%`-interpolated literal — since `--database` is
operator-supplied input reaching raw DDL.

**Blocker, empirically verified rather than assumed: `with
psycopg2.connect(...) as conn:` is unsafe here, even with
`autocommit=True` set inside the block.** Tested directly against a real
local Postgres 16 server, with psycopg2 2.9.12 (the version actually
installed for this project): entering a connection as a context manager
starts an implicit transaction regardless of `autocommit`, and `CREATE
DATABASE` cannot run inside one —

```text
psycopg2.errors.ActiveSqlTransaction: CREATE DATABASE cannot run inside a transaction block
```

— reproduced exactly, on the first attempt, with the `with conn:` pattern
from the previous draft. The fix, also verified directly to succeed
against the same server: a plain `connect()`, no `with` block around the
connection object itself, `autocommit` set before any execute, closed
explicitly in `finally`:

```python
from psycopg2 import sql

conn = psycopg2.connect(**maintenance_params)
try:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.database)))
finally:
    conn.close()
```

This also gives historical replay a clean, one-variable setup: point
`DIFFUSION_DB_DSN` at the right server once, then `--database
diffusion_experiment_replay --purpose historical_replay` needs nothing
else. The DSN says where/how to connect; `--database` says which
database on that server; `database_metadata` says what that database is
allowed to be used for — three separate, non-overlapping
responsibilities.

## 8. `--verify` — precise about what it actually proves

```text
--verify requires ALL of:
    database_metadata exists, exactly one row, purpose == expected --purpose
      (delegates to assert_database_purpose itself)
    schema_migrations exists
    no step in MIGRATION_STEPS_IN_ORDER is missing from schema_migrations
    no version is recorded in schema_migrations that ISN'T in
      MIGRATION_STEPS_IN_ORDER -- reported explicitly as "this database is
      AHEAD of the code checkout running --verify," not generic failure
    every recorded checksum matches the current file's sha256 -- reports
      the specific step and says its file was edited after being
      recorded (applied or adopted), not "verification failed"
```

**Precise claim, not overclaimed:** this proves the same expected
migration lineage is present and its source files are unchanged since
being recorded — an executable, checksummed fact. It does not detect
manual out-of-band DDL performed outside this migration path entirely
(e.g. an ad hoc `ALTER TABLE ... DROP COLUMN` run by hand after the fact)
— that would require schema-fingerprinting this phase deliberately does
not build, consistent with keeping this proportionate to a hobby project
under a normal no-manual-DDL discipline, not a full migration framework.

**`--verify` is STRICTLY READ-ONLY — frozen explicitly, not left for
Claude Code to infer.** `bootstrap_database.py` is one script that also
owns creation, migration, and adoption, so nothing stops an implementer
from having `--verify` "helpfully" call the same shared setup machinery
those mutating paths use, to auto-repair whatever it finds wrong. That is
never permitted. `--verify` must never:

- create the target database if it does not exist;
- create `schema_migrations` or `database_metadata`;
- apply any migration;
- insert, update, or delete any row;
- adopt an existing database (i.e. never itself triggers
  `--adopt-existing` behavior);
- repair a missing migration or purpose row.

If the target database does not exist, cannot be connected to, is
untracked, incomplete, inconsistent, or fails any condition listed above,
`--verify` exits non-zero and changes nothing — full stop. Observation
and mutation are strictly separate commands: `bootstrap`/`--adopt-existing`
mutate, `--verify` only ever looks.

## 9. `README.md`'s setup instructions get replaced, not duplicated

The manual `createdb` / `psql -f schema.sql` / six-migration sequence is
replaced with the `bootstrap_database.py` invocations above for a fresh
database, plus a pointer to Section 6's frozen one-time adoption sequence
for anyone setting this up against the pre-existing real database.

## 10. Required tests

- `get_db_dsn()`: `DIFFUSION_DB_DSN` when set; standardized default when
  unset.
- `assert_database_purpose`: exact single matching row passes; zero rows,
  more than one row, an unexpected constraint-valid value, a missing
  table, and a real query error all raise `DatabasePurposeError` — and a
  test proving the check reads the database's own row, not the DSN
  string.
- `database_metadata`'s `CHECK` constraints, against a real database:
  second row (even with an explicit distinct `singleton_key`) fails;
  unrecognized `database_purpose` value fails.
- `edgar_ingest_worker.py` / `extraction_runner.py`: a fixture pointed at
  a database whose `database_metadata` says `historical_replay` causes
  the very first write attempt to raise, before any row is written.
- `seed_entities.py` / `manual_resolve.py`: `--purpose` is required (no
  silent default); calling either against a database of the other
  purpose raises before any write.
- `bootstrap_database.py`, against real throwaway Postgres databases
  only: fresh bootstrap produces every expected table, every step
  checksummed in `schema_migrations` with `record_origin='applied'`, one
  correct `database_metadata` row; re-running is a no-op; re-running with
  a different `--purpose` fails loudly and changes nothing; a simulated
  crash between a migration's apply and its record leaves neither applied
  nor recorded; editing an already-recorded migration file and
  re-verifying fails specifically on that step's checksum.
- **`schema.sql` itself passes `--verify`** on a freshly bootstrapped
  database — i.e. the specific manifest blocker from round 3, tested
  directly so it can't regress silently.
- **The fresh-vs-legacy state check, all three real branches**: an
  existing, genuinely empty database (created but nothing else done to
  it — e.g. simulating the exact crash-before-"001" scenario this round's
  blocker was about) proceeds through normal bootstrap, never refused;
  an existing database with real legacy tables and no `schema_migrations`
  is refused with the `--adopt-existing` message; an existing, already-
  tracked database migrates forward normally.
- **`CREATE DATABASE` actually succeeds** against a real Postgres server
  with the project's actual installed psycopg2 version — the specific
  regression this round's second blocker was about, tested directly
  rather than only reasoned about (a plain assertion that
  `bootstrap_database.py --database <fresh-name> --purpose forward`
  exits successfully and the database exists afterward).
- Migration `"008"` + the `database_metadata` purpose row as one atomic
  unit: a simulated failure between recording `"008"` and inserting the
  purpose row (or the reverse ordering, whichever the implementation
  picks, as long as they're one transaction) leaves neither present —
  never a database with `"008"` recorded but no purpose row, or vice
  versa.
- `recorded_at` (not `applied_at`) is the actual column name used
  everywhere — a plain regression test on the column name, since this
  round renamed it specifically because the old name was untruthful for
  adopted rows.
- `--adopt-existing`, against a real database seeded exactly like the
  actual current `diffusion_experiment` (`schema.sql` + migrations
  `002`-`007` applied by hand, no `schema_migrations`): succeeds, records
  `"001"`-`"007"` with `record_origin='legacy_adopted'`, applies `"008"`
  with `record_origin='applied'`, creates the `forward` row — and refuses
  against a database missing even one expected pre-Phase-0
  table/column, rather than partially adopting. Refuses on an
  already-tracked database. Normal (non-adopt) bootstrap refuses — with
  the exact corrective command printed — against an existing, untracked
  database.
- **The full legacy-adoption checklist, one migration at a time, not just
  in aggregate**: for each of `"001"`-`"007"`, a dedicated test that
  removes or falsifies only that one migration's specific structural
  check (e.g. drop `entity_aliases` for `"002"`; re-add
  `catalysts.canonicalization_completed_at` for `"003"`; drop
  `arm_outcomes.exit_reason` for `"006"`) against an otherwise-complete
  legacy baseline, and confirms adoption refuses specifically because of
  that missing/wrong piece — proving no migration is being waved through
  because an earlier or later one's evidence happened to be present.
- **`--adopt-existing --purpose historical_replay` is rejected outright**,
  before the state check even runs, against both a `LEGACY_UNTRACKED` and
  a `FRESH_EMPTY` target — confirming the round-5 hardening that adoption
  only ever produces a `forward`-purpose database.
- **The Phase 0 integration-test fixture that hands out disposable
  databases must generate its own throwaway database name and hard-fail
  before doing anything else if that resolved name equals
  `diffusion_experiment`** — every destructive or adoption-related
  integration test (anything that calls `--adopt-existing`, applies
  migrations, or otherwise mutates a target database) must obtain its
  target exclusively through this fixture, never a hardcoded or
  passed-through name. This is deliberately NOT the same claim as "the
  test suite never runs the real 287-test suite against
  diffusion_experiment" — that is a true requirement (Section 6, step B),
  but it is a statement about which external command Claude Code chooses
  to run, and no test inside the codebase can observe or enforce that. The
  fixture guard above is the enforceable version: it can't prevent someone
  from typing the wrong command, but it does guarantee that everything
  the Phase 0 test suite itself drives is provably disposable, never the
  real database, by construction rather than by discipline.
- Bootstrap's own connection: a test confirming the maintenance and
  target connections both derive their host/port/user from
  `db_config.get_db_dsn()` (e.g. by setting `DIFFUSION_DB_DSN` to a
  non-default value and confirming both connections use it), not a
  separately hardcoded template; a test confirming `--database` reaches
  `CREATE DATABASE` only through `sql.Identifier`, never raw
  interpolation (e.g. a database name containing a quote character is
  handled safely, not as a SQL-injection vector).
- `--verify`: passes against a correctly bootstrapped database (fresh
  *and* adopted); fails with a specific, correct message against one
  step short, an extra/unexpected recorded version, and a checksum
  mismatch — three distinct messages for three distinct problems.
- **`--verify` is strictly read-only**: run against a database that does
  not exist at all — fails, and the database is confirmed still not to
  exist afterward (never silently created); run against an existing,
  incomplete TRACKED database (a real suffix of steps missing) — fails,
  and `schema_migrations` is confirmed unchanged afterward (the missing
  suffix is never auto-applied); run against a LEGACY_UNTRACKED database
  — fails with the normal refusal, and confirmed it never performs
  adoption itself.
- **Checksum input is raw file bytes, not a normalized form**: a
  regression test that changes only whitespace or a line ending
  (`\n` -> `\r\n`) in an already-recorded migration file and confirms
  `--verify` reports that specific step's checksum as mismatched, not a
  false pass.
