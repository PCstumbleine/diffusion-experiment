"""bootstrap_database.py -- Historical Replay Phase 0
(specs/historical-replay-phase0-implementation-spec-final.md), Sections
4-8. Creates/migrates/adopts/verifies a Postgres database against the
one shared migration manifest, recording an honest, checksummed history
in `schema_migrations` and a `database_metadata` singleton row saying what
the database is for.

Usage:
  python3 bootstrap_database.py --database <name> --purpose {forward,historical_replay}
      Creates <name> if it doesn't exist (FRESH_EMPTY path), or migrates
      an existing tracked database forward, or refuses (with the exact
      corrective command) if <name> exists but is untracked legacy data.

  python3 bootstrap_database.py --adopt-existing --database <name> --purpose forward
      One-time adoption of an existing, untracked, already-migrated-by-hand
      legacy database. Accepts ONLY --purpose forward. Runs the full
      legacy-adoption checklist read-only before opening any transaction;
      refuses entirely (no partial adoption) if any single check fails.

  python3 bootstrap_database.py --verify --database <name> --purpose {forward,historical_replay}
      Strictly read-only. Never creates, migrates, adopts, or repairs
      anything -- only observes and exits non-zero on any problem.

See the spec for the full frozen state machine, atomic transaction
boundaries, and the literal per-migration legacy-adoption checklist.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg2
import psycopg2.extensions
from psycopg2 import sql

import db_config
from db_config import assert_database_purpose, DatabasePurposeError

BUILD_DIR = Path(__file__).resolve().parent

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

FRESH_EMPTY = "FRESH_EMPTY"
LEGACY_UNTRACKED = "LEGACY_UNTRACKED"
TRACKED = "TRACKED"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE schema_migrations (
    version          TEXT PRIMARY KEY,
    checksum_sha256  TEXT NOT NULL,
    record_origin    TEXT NOT NULL CHECK (record_origin IN ('applied', 'legacy_adopted')),
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MigrationStateError(Exception):
    """A frozen state-machine invariant (Section 6) was violated: a
    non-contiguous or out-of-manifest recorded history, an unrecognized
    database state, a legacy-adoption checklist failure, or an attempt to
    adopt/bootstrap a database in a state that operation doesn't apply to.
    Fail-closed -- never guessed at or silently repaired."""


# ---------------------------------------------------------------------------
# Section 7: bootstrap's own connection mechanics
# ---------------------------------------------------------------------------

def _migration_path(relative_path: str) -> Path:
    return BUILD_DIR / relative_path


def _checksum_of(relative_path: str) -> str:
    return hashlib.sha256(_migration_path(relative_path).read_bytes()).hexdigest()


def _manifest_path_for(version: str) -> str:
    for v, path in MIGRATION_STEPS_IN_ORDER:
        if v == version:
            return path
    raise MigrationStateError(f"version {version!r} is not in MIGRATION_STEPS_IN_ORDER")


def _base_connection_params() -> dict:
    return psycopg2.extensions.parse_dsn(db_config.get_db_dsn())


def _maintenance_connection_params() -> dict:
    return {**_base_connection_params(), "dbname": "postgres"}


def _target_connection_params(database_name: str) -> dict:
    return {**_base_connection_params(), "dbname": database_name}


def database_exists(database_name: str) -> bool:
    conn = psycopg2.connect(**_maintenance_connection_params())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def create_database(database_name: str) -> None:
    """CREATE DATABASE, using the frozen connection pattern (Section 7):
    a plain connect(), never a `with` block on the connection object
    itself (that starts an implicit transaction even with autocommit=True,
    and CREATE DATABASE cannot run inside one -- empirically reproduced
    and fixed against a real local Postgres 16 with this project's actual
    psycopg2 2.9.12). sql.Identifier, never string interpolation, for the
    operator-supplied database name."""
    conn = psycopg2.connect(**_maintenance_connection_params())
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    finally:
        conn.close()


def connect_to_target(database_name: str):
    return psycopg2.connect(**_target_connection_params(database_name))


# ---------------------------------------------------------------------------
# Section 6: fresh / legacy / tracked state check
# ---------------------------------------------------------------------------

def _schema_migrations_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        exists = cur.fetchone()[0]
    conn.commit()
    return exists


def database_metadata_table_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.database_metadata') IS NOT NULL")
        exists = cur.fetchone()[0]
    conn.commit()
    return exists


def check_database_state(conn) -> str:
    """FRESH_EMPTY / LEGACY_UNTRACKED / TRACKED -- run before any mutation.
    Distinguishes "empty" from "legacy" by looking for ANY user-created
    relation (not just plain tables -- a bare pg_tables count misses
    views, materialized views, sequences, and foreign tables, any one of
    which would wrongly let a non-empty database through as fresh)."""
    if _schema_migrations_exists(conn):
        return TRACKED
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','p','v','m','S','f')
              AND n.nspname NOT IN ('pg_catalog','information_schema')
              AND n.nspname !~ '^pg_toast'
            """
        )
        count = cur.fetchone()[0]
    conn.commit()
    return FRESH_EMPTY if count == 0 else LEGACY_UNTRACKED


def _recorded_versions_in_manifest_order(conn) -> list:
    """Raw recorded version strings from schema_migrations, reordered:
    values that appear in MIGRATION_STEPS_IN_ORDER come first, in manifest
    order; any recorded value NOT in the manifest is preserved at the end
    (in recorded_at order) rather than silently dropped -- the caller's
    out-of-manifest check (Section 6) relies on it still being present
    here to detect, and the contiguous-prefix check relies on a genuine
    gap (e.g. missing "003") showing up as a mismatch against
    expected[:len(recorded)]."""
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY recorded_at, version")
        raw = [row[0] for row in cur.fetchall()]
    conn.commit()
    expected = [v for v, _p in MIGRATION_STEPS_IN_ORDER]
    in_manifest = [v for v in expected if v in raw]
    out_of_manifest = [v for v in raw if v not in expected]
    return in_manifest + out_of_manifest


def verify_checksums_for(conn, versions: list) -> None:
    """Raises MigrationStateError on the first checksum mismatch among the
    given (already validated to be in-manifest) versions."""
    if not versions:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, checksum_sha256 FROM schema_migrations WHERE version = ANY(%s)",
            (versions,),
        )
        recorded_checksums = dict(cur.fetchall())
    conn.commit()
    for version in versions:
        path = _manifest_path_for(version)
        expected_checksum = _checksum_of(path)
        actual_checksum = recorded_checksums.get(version)
        if actual_checksum != expected_checksum:
            raise MigrationStateError(
                f'schema_migrations version "{version}": recorded checksum {actual_checksum!r} does '
                f"not match the current file's sha256 {expected_checksum!r} -- {path} was edited "
                "after being recorded."
            )


def _validate_tracked_and_get_pending(conn, purpose: str):
    """Section 6's frozen contiguous-prefix validation. Returns the list
    of not-yet-applied versions to apply next, or None if "008" is already
    recorded (nothing to apply) -- in which case assert_database_purpose
    has ALREADY been run and passed before returning None."""
    expected = [v for v, _p in MIGRATION_STEPS_IN_ORDER]
    recorded = _recorded_versions_in_manifest_order(conn)

    if not recorded:
        raise MigrationStateError('schema_migrations exists but is empty -- refusing to guess')

    if any(version not in expected for version in recorded):
        raise MigrationStateError(f"schema_migrations contains a version not in the manifest: {recorded}")

    if recorded != expected[: len(recorded)]:
        raise MigrationStateError(
            f"recorded migration history {recorded} is not a contiguous prefix of {expected} -- "
            "refusing to apply anything until this is understood, not guessing at an order."
        )

    verify_checksums_for(conn, recorded)

    if "008" in recorded:
        # Every migration is already applied -- run the SAME check
        # Section 3's forward-purpose guards use, here, before reporting
        # anything, so a fully-migrated TRACKED database with a
        # MISMATCHED --purpose never falls through to a silent,
        # purpose-blind success.
        assert_database_purpose(conn, purpose)
        return None

    # "008" not yet recorded. Through the valid atomic path, database_metadata
    # is created ONLY together with "008" being applied and recorded -- so
    # database_metadata existing here is impossible via this tooling's own
    # operations and therefore a corruption/out-of-band signal.
    if database_metadata_table_exists(conn):
        raise MigrationStateError(
            'database_metadata already exists but "008" is not yet recorded in schema_migrations -- '
            "inconsistent state, refusing to guess which is right"
        )
    return expected[len(recorded):]


# ---------------------------------------------------------------------------
# Section 4/6: applying migrations, one atomic transaction per step
# ---------------------------------------------------------------------------

def _apply_step_atomically(conn, version: str, purpose: str | None) -> None:
    path = _manifest_path_for(version)
    raw_bytes = _migration_path(path).read_bytes()
    checksum = hashlib.sha256(raw_bytes).hexdigest()
    sql_text = raw_bytes.decode("utf-8")
    try:
        with conn.cursor() as cur:
            if version == "001":
                cur.execute(SCHEMA_MIGRATIONS_DDL)
            cur.execute(sql_text)
            if version == "008":
                cur.execute(
                    "INSERT INTO database_metadata (singleton_key, database_purpose) VALUES ('singleton', %s)",
                    (purpose,),
                )
            cur.execute(
                "INSERT INTO schema_migrations (version, checksum_sha256, record_origin) "
                "VALUES (%s, %s, 'applied')",
                (version, checksum),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def apply_in_order(conn, versions_to_apply: list, purpose: str) -> None:
    for version in versions_to_apply:
        _apply_step_atomically(conn, version, purpose if version == "008" else None)


# ---------------------------------------------------------------------------
# Section 6: literal, per-migration legacy-adoption checklist
# ---------------------------------------------------------------------------

def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table_name}",))
        exists = cur.fetchone()[0]
    conn.commit()
    return exists


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table_name, column_name),
        )
        exists = cur.fetchone() is not None
    conn.commit()
    return exists


def _column_not_null(conn, table_name: str, column_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table_name, column_name),
        )
        row = cur.fetchone()
    conn.commit()
    return row is not None and row[0] == "NO"


def _index_exists(conn, index_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s", (index_name,))
        exists = cur.fetchone() is not None
    conn.commit()
    return exists


def _trigger_exists(conn, table_name: str, trigger_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = %s AND t.tgname = %s",
            (table_name, trigger_name),
        )
        exists = cur.fetchone() is not None
    conn.commit()
    return exists


def _primary_key_columns(conn, table_name: str) -> set:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = %s::regclass AND i.indisprimary
            """,
            (table_name,),
        )
        cols = {row[0] for row in cur.fetchall()}
    conn.commit()
    return cols


_SCHEMA_001_TABLES = [
    "raw_documents", "catalysts", "catalyst_documents", "canonical_events",
    "event_versions", "event_document_links", "surprise_transform_registry",
    "extracted_events", "entities", "instruments", "instrument_identifiers",
    "corporate_actions", "event_entities", "entity_relationships",
    "underreaction_estimates", "candidate_signals",
    "candidate_supporting_relationships", "model_candidate_decisions",
    "experiments", "experiment_arms", "arm_entries", "arm_outcomes",
    "quote_snapshots", "market_data", "document_embeddings", "audit_log",
]


def _check_legacy_001(conn) -> None:
    missing = [t for t in _SCHEMA_001_TABLES if not _table_exists(conn, t)]
    if missing:
        raise MigrationStateError(f'legacy-adoption check for "001" failed: missing table(s) {missing}')


def _check_legacy_002(conn) -> None:
    for table in ("entity_aliases", "watchlist_membership", "extraction_runs", "unresolved_entity_mentions"):
        if not _table_exists(conn, table):
            raise MigrationStateError(f'legacy-adoption check for "002" failed: missing table {table!r}')
    if not _index_exists(conn, "idx_entities_cik_unique"):
        raise MigrationStateError(
            'legacy-adoption check for "002" failed: missing unique index idx_entities_cik_unique'
        )
    if not _column_exists(conn, "entity_relationships", "extraction_run_id"):
        raise MigrationStateError(
            'legacy-adoption check for "002" failed: entity_relationships.extraction_run_id missing'
        )
    if not _column_not_null(conn, "entity_relationships", "extraction_run_id"):
        raise MigrationStateError(
            'legacy-adoption check for "002" failed: entity_relationships.extraction_run_id is nullable'
        )


def _check_legacy_003(conn) -> None:
    if not _column_exists(conn, "extraction_runs", "cleaned_llm_output"):
        raise MigrationStateError(
            'legacy-adoption check for "003" failed: extraction_runs.cleaned_llm_output missing'
        )
    if not _column_exists(conn, "extraction_runs", "validation_drop_log"):
        raise MigrationStateError(
            'legacy-adoption check for "003" failed: extraction_runs.validation_drop_log missing'
        )
    if not _column_exists(conn, "extracted_events", "extraction_run_id"):
        raise MigrationStateError(
            'legacy-adoption check for "003" failed: extracted_events.extraction_run_id missing'
        )
    if not _column_not_null(conn, "extracted_events", "extraction_run_id"):
        raise MigrationStateError(
            'legacy-adoption check for "003" failed: extracted_events.extraction_run_id is nullable'
        )
    if not _table_exists(conn, "catalyst_processing_runs"):
        raise MigrationStateError('legacy-adoption check for "003" failed: catalyst_processing_runs missing')
    expected_pk = {"catalyst_id", "extraction_prompt_version", "extractor_model_id", "extractor_model_version"}
    actual_pk = _primary_key_columns(conn, "catalyst_processing_runs")
    if actual_pk != expected_pk:
        raise MigrationStateError(
            f'legacy-adoption check for "003" failed: catalyst_processing_runs primary key is '
            f"{actual_pk}, expected {expected_pk}"
        )
    if _column_exists(conn, "catalysts", "canonicalization_completed_at"):
        raise MigrationStateError(
            'legacy-adoption check for "003" failed: catalysts.canonicalization_completed_at still '
            "exists -- migration 003 drops this column; its continued presence means 003 was never "
            "actually applied"
        )


def _check_legacy_004(conn) -> None:
    for col in ("observed_value_low", "observed_value_high", "reference_value_low", "reference_value_high"):
        if not _column_exists(conn, "extracted_events", col):
            raise MigrationStateError(f'legacy-adoption check for "004" failed: extracted_events.{col} missing')


def _check_legacy_005(conn) -> None:
    if not _column_exists(conn, "catalyst_processing_runs", "processing_issues"):
        raise MigrationStateError(
            'legacy-adoption check for "005" failed: catalyst_processing_runs.processing_issues missing'
        )


def _check_legacy_006(conn) -> None:
    for col in ("entry_price_source", "exit_price_source", "entry_fee", "exit_fee",
                "return_method_version", "fee_method_version", "exit_reason"):
        if not _column_exists(conn, "arm_outcomes", col):
            raise MigrationStateError(f'legacy-adoption check for "006" failed: arm_outcomes.{col} missing')
        if not _column_not_null(conn, "arm_outcomes", col):
            raise MigrationStateError(f'legacy-adoption check for "006" failed: arm_outcomes.{col} is nullable')


def _check_legacy_007(conn) -> None:
    if not _table_exists(conn, "experiment_catalysts"):
        raise MigrationStateError('legacy-adoption check for "007" failed: experiment_catalysts missing')
    expected_pk = {"experiment_id", "catalyst_id"}
    actual_pk = _primary_key_columns(conn, "experiment_catalysts")
    if actual_pk != expected_pk:
        raise MigrationStateError(
            f'legacy-adoption check for "007" failed: experiment_catalysts primary key is {actual_pk}, '
            f"expected {expected_pk}"
        )
    for trigger in ("trg_check_experiment_catalyst_epoch_consistency", "trg_forbid_experiment_catalyst_mutation"):
        if not _trigger_exists(conn, "experiment_catalysts", trigger):
            raise MigrationStateError(
                f'legacy-adoption check for "007" failed: trigger {trigger!r} missing on experiment_catalysts'
            )


_LEGACY_CHECKLIST_FUNCTIONS = {
    "001": _check_legacy_001,
    "002": _check_legacy_002,
    "003": _check_legacy_003,
    "004": _check_legacy_004,
    "005": _check_legacy_005,
    "006": _check_legacy_006,
    "007": _check_legacy_007,
}


def run_legacy_adoption_checklist(conn) -> None:
    """Runs EVERY check for "001"-"007", read-only, entirely before the
    adoption transaction opens. No migration is baselined because an
    earlier or later one's evidence happened to be present -- each
    version's check is independent and specific to that version alone."""
    for version in ("001", "002", "003", "004", "005", "006", "007"):
        _LEGACY_CHECKLIST_FUNCTIONS[version](conn)


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------

def cmd_bootstrap(database_name: str, purpose: str) -> str:
    """Normal (non-adopt, non-verify) invocation. Returns a human-readable
    status message on success; raises MigrationStateError/DatabasePurposeError
    otherwise."""
    if not database_exists(database_name):
        create_database(database_name)
        conn = connect_to_target(database_name)
        try:
            apply_in_order(conn, [v for v, _p in MIGRATION_STEPS_IN_ORDER], purpose)
        finally:
            conn.close()
        return f'Bootstrapped {database_name!r} fresh, purpose={purpose!r}.'

    conn = connect_to_target(database_name)
    try:
        state = check_database_state(conn)
        if state == LEGACY_UNTRACKED:
            raise MigrationStateError(
                f"database {database_name!r} contains pre-existing, untracked objects -- refusing "
                "normal bootstrap. To bring an already-populated forward database under tracking, "
                f"run: python3 bootstrap_database.py --adopt-existing --database {database_name} "
                f"--purpose {purpose}"
            )
        if state == FRESH_EMPTY:
            apply_in_order(conn, [v for v, _p in MIGRATION_STEPS_IN_ORDER], purpose)
            return f'Bootstrapped {database_name!r} fresh, purpose={purpose!r}.'

        # TRACKED
        pending = _validate_tracked_and_get_pending(conn, purpose)
        if pending is None:
            return f'Database {database_name!r} already fully migrated, purpose={purpose!r} confirmed. Nothing to do.'
        apply_in_order(conn, pending, purpose)
        return f'Migrated {database_name!r} forward through "008", purpose={purpose!r}.'
    finally:
        conn.close()


def cmd_adopt_existing(database_name: str, purpose: str) -> str:
    if purpose != "forward":
        raise ValueError('--adopt-existing accepts only --purpose forward.')

    if not database_exists(database_name):
        raise MigrationStateError(
            f"database {database_name!r} does not exist -- --adopt-existing requires an existing database."
        )

    conn = connect_to_target(database_name)
    try:
        state = check_database_state(conn)
        if state == FRESH_EMPTY:
            raise MigrationStateError(
                f"database {database_name!r} is empty -- an empty database is bootstrapped normally, "
                "never adopted."
            )
        if state == TRACKED:
            raise MigrationStateError(
                f"database {database_name!r} is already tracked (schema_migrations exists) -- adoption "
                "is only for an untracked legacy database."
            )

        # LEGACY_UNTRACKED -- run the full checklist, read-only, entirely
        # before the adoption transaction opens.
        run_legacy_adoption_checklist(conn)

        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_MIGRATIONS_DDL)
                for version, path in MIGRATION_STEPS_IN_ORDER:
                    if version == "008":
                        break
                    checksum = _checksum_of(path)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, checksum_sha256, record_origin) "
                        "VALUES (%s, %s, 'legacy_adopted')",
                        (version, checksum),
                    )
                path_008 = _manifest_path_for("008")
                raw_bytes_008 = _migration_path(path_008).read_bytes()
                checksum_008 = hashlib.sha256(raw_bytes_008).hexdigest()
                cur.execute(raw_bytes_008.decode("utf-8"))
                cur.execute(
                    "INSERT INTO database_metadata (singleton_key, database_purpose) VALUES ('singleton', %s)",
                    (purpose,),
                )
                cur.execute(
                    "INSERT INTO schema_migrations (version, checksum_sha256, record_origin) "
                    "VALUES (%s, %s, 'applied')",
                    ("008", checksum_008),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return f'Adopted {database_name!r} as purpose={purpose!r}: "001"-"007" recorded as legacy_adopted, "008" applied.'
    finally:
        conn.close()


def cmd_verify(database_name: str, purpose: str) -> tuple:
    """Strictly read-only (Section 8) -- never creates the database, never
    creates schema_migrations/database_metadata, never applies a
    migration, never writes a row, never adopts, never repairs. Returns
    (ok: bool, message: str)."""
    if not database_exists(database_name):
        return False, f"database {database_name!r} does not exist."

    conn = connect_to_target(database_name)
    try:
        if not _schema_migrations_exists(conn):
            return False, f"database {database_name!r} has no schema_migrations table -- untracked."

        expected = [v for v, _p in MIGRATION_STEPS_IN_ORDER]
        recorded = _recorded_versions_in_manifest_order(conn)

        missing = [v for v in expected if v not in recorded]
        if missing:
            return False, f"missing migration step(s) from schema_migrations: {missing}"

        extra = [v for v in recorded if v not in expected]
        if extra:
            return False, (
                "this database is AHEAD of this code checkout -- schema_migrations records version(s) "
                f"not in MIGRATION_STEPS_IN_ORDER: {extra}"
            )

        for version in expected:
            path = _manifest_path_for(version)
            expected_checksum = _checksum_of(path)
            with conn.cursor() as cur:
                cur.execute("SELECT checksum_sha256 FROM schema_migrations WHERE version = %s", (version,))
                actual_checksum = cur.fetchone()[0]
            conn.commit()
            if actual_checksum != expected_checksum:
                return False, (
                    f'recorded checksum for "{version}" does not match the current file {path} -- '
                    "it was edited after being recorded."
                )

        try:
            assert_database_purpose(conn, purpose)
        except DatabasePurposeError as exc:
            return False, str(exc)

        return True, f'database {database_name!r} is fully migrated through "{expected[-1]}", purpose={purpose!r} confirmed.'
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, help="Which database, on the server db_config.get_db_dsn() names.")
    parser.add_argument("--purpose", required=True, choices=["forward", "historical_replay"])
    parser.add_argument("--adopt-existing", action="store_true",
                         help="One-time adoption of an existing, untracked legacy database. --purpose forward only.")
    parser.add_argument("--verify", action="store_true",
                         help="Strictly read-only: check the database's migration/purpose state and exit non-zero on any problem.")
    args = parser.parse_args()

    if args.adopt_existing and args.verify:
        print("Refusing: --adopt-existing and --verify are mutually exclusive.")
        sys.exit(1)

    try:
        if args.verify:
            ok, message = cmd_verify(args.database, args.purpose)
            print(("OK: " if ok else "FAIL: ") + message)
            sys.exit(0 if ok else 1)
        if args.adopt_existing:
            print(cmd_adopt_existing(args.database, args.purpose))
            return
        print(cmd_bootstrap(args.database, args.purpose))
    except (MigrationStateError, DatabasePurposeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
