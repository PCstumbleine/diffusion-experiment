"""
Historical Replay Phase 0 (specs/historical-replay-phase0-implementation-spec-final.md),
Section 10's bootstrap_database.py tests. Every database this file
touches comes exclusively from the `disposable_db_name` fixture
(conftest.py) -- never a hardcoded or passed-through name.
"""
import uuid

import psycopg2
import psycopg2.extensions
import pytest
from psycopg2 import sql

import bootstrap_database as bd
import db_config
from db_config import assert_database_purpose, DatabasePurposeError
from conftest import drop_database_if_exists

MODEL_STEPS = bd.MIGRATION_STEPS_IN_ORDER
ALL_VERSIONS = [v for v, _p in MODEL_STEPS]
LEGACY_VERSIONS = ALL_VERSIONS[:-1]  # "001".."007" -- "008" is Phase 0 itself


def seed_legacy_baseline(database_name: str):
    """Creates `database_name` and applies schema.sql + migrations
    002-007 directly (raw SQL, no schema_migrations bookkeeping) --
    simulates the real diffusion_experiment database's actual history:
    each migration applied by hand over time, before this Phase 0 tooling
    existed. Returns an open connection to it (LEGACY_UNTRACKED state)."""
    bd.create_database(database_name)
    conn = bd.connect_to_target(database_name)
    for version, path in MODEL_STEPS:
        if version == "008":
            break
        sql_text = (bd.BUILD_DIR / path).read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql_text)
    conn.commit()
    return conn


def get_recorded_rows(conn):
    """(version, checksum_sha256, record_origin) tuples, in recorded_at order."""
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum_sha256, record_origin FROM schema_migrations ORDER BY recorded_at")
        rows = cur.fetchall()
    conn.commit()
    return rows


def get_database_metadata_row(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT singleton_key, database_purpose FROM database_metadata")
        row = cur.fetchone()
    conn.commit()
    return row


# ===========================================================================
# Fresh bootstrap
# ===========================================================================

def test_fresh_bootstrap_produces_every_table_checksummed_applied_and_one_metadata_row(disposable_db_name):
    message = bd.cmd_bootstrap(disposable_db_name, "forward")
    assert "forward" in message

    conn = bd.connect_to_target(disposable_db_name)
    try:
        rows = get_recorded_rows(conn)
        assert [r[0] for r in rows] == ALL_VERSIONS
        for version, checksum, origin in rows:
            path = bd._manifest_path_for(version)
            assert checksum == bd._checksum_of(path)
            assert origin == "applied"

        metadata_row = get_database_metadata_row(conn)
        assert metadata_row == ("singleton", "forward")

        # Every table from the "001" checklist genuinely exists.
        for table in bd._SCHEMA_001_TABLES:
            assert bd._table_exists(conn, table)
        assert bd._table_exists(conn, "database_metadata")
    finally:
        conn.close()


def test_create_database_actually_succeeds_against_a_real_server(disposable_db_name):
    """The specific regression this spec's round-4 blocker was about --
    `with psycopg2.connect(...) as conn:` starting an implicit
    transaction even under autocommit=True, which CREATE DATABASE cannot
    run inside. Tested directly: a plain assertion that bootstrap exits
    successfully and the database exists afterward."""
    assert not bd.database_exists(disposable_db_name)
    bd.cmd_bootstrap(disposable_db_name, "forward")
    assert bd.database_exists(disposable_db_name)


def test_rerunning_fresh_bootstrap_is_a_noop(disposable_db_name):
    bd.cmd_bootstrap(disposable_db_name, "forward")
    conn = bd.connect_to_target(disposable_db_name)
    rows_before = get_recorded_rows(conn)
    conn.close()

    message = bd.cmd_bootstrap(disposable_db_name, "forward")
    assert "Nothing to do" in message

    conn = bd.connect_to_target(disposable_db_name)
    try:
        rows_after = get_recorded_rows(conn)
        assert rows_after == rows_before
    finally:
        conn.close()


def test_rerunning_with_different_purpose_fails_loudly_and_changes_nothing(disposable_db_name):
    bd.cmd_bootstrap(disposable_db_name, "forward")
    conn = bd.connect_to_target(disposable_db_name)
    rows_before = get_recorded_rows(conn)
    metadata_before = get_database_metadata_row(conn)
    conn.close()

    with pytest.raises(DatabasePurposeError):
        bd.cmd_bootstrap(disposable_db_name, "historical_replay")

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert get_recorded_rows(conn) == rows_before
        assert get_database_metadata_row(conn) == metadata_before
    finally:
        conn.close()


def test_recorded_at_is_the_actual_column_name(disposable_db_name):
    """Plain regression test on the column name -- renamed specifically
    because 'applied_at' was untruthful for adopted rows."""
    bd.cmd_bootstrap(disposable_db_name, "forward")
    conn = bd.connect_to_target(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'schema_migrations' ORDER BY ordinal_position"
            )
            columns = [row[0] for row in cur.fetchall()]
        conn.commit()
        assert "recorded_at" in columns
        assert "applied_at" not in columns
    finally:
        conn.close()


class _CrashAtCallCursor(psycopg2.extensions.cursor):
    """A cursor subclass that raises on the Nth .execute() call across the
    connection's lifetime (psycopg2.extensions.cursor is a C type and
    cannot be monkeypatched directly -- this is the real, supported seam:
    connection.cursor_factory)."""
    crash_at_call_number = None
    call_count = 0

    def execute(self, query, params=None):
        type(self).call_count += 1
        if type(self).crash_at_call_number is not None and type(self).call_count == type(self).crash_at_call_number:
            raise RuntimeError("simulated crash")
        return super().execute(query, params)


def test_a_simulated_crash_between_apply_and_record_leaves_neither(disposable_db_name):
    """Forces a failure INSIDE one step's atomic apply+record transaction
    (after the migration's own DDL has run, before the schema_migrations
    INSERT commits) and confirms the whole step rolled back -- neither
    applied nor recorded."""
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    try:
        # Apply "001" normally first (needed so "002" has something to build on).
        bd._apply_step_atomically(conn, "001", None)

        conn.cursor_factory = _CrashAtCallCursor
        _CrashAtCallCursor.call_count = 0
        # "002"'s own atomic step issues, in order: (1) apply migration
        # 002's SQL, (2) the schema_migrations INSERT that would record
        # it. Let (1) succeed for real, blow up on (2).
        _CrashAtCallCursor.crash_at_call_number = 2
        try:
            with pytest.raises(RuntimeError, match="simulated crash"):
                bd._apply_step_atomically(conn, "002", None)
        finally:
            _CrashAtCallCursor.crash_at_call_number = None
            conn.cursor_factory = None

        # Neither applied (entity_aliases from "002" must not exist) nor recorded.
        assert not bd._table_exists(conn, "entity_aliases")
        rows = get_recorded_rows(conn)
        assert [r[0] for r in rows] == ["001"]
    finally:
        conn.close()


def test_migration_008_and_purpose_row_are_one_atomic_unit(disposable_db_name):
    """A simulated failure between the database_metadata INSERT and the
    "008" schema_migrations record (or the reverse ordering -- this
    implementation inserts the purpose row, then the schema_migrations
    row, within "008"'s single atomic step) leaves NEITHER present."""
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    try:
        for version in ("001", "002", "003", "004", "005", "006", "007"):
            bd._apply_step_atomically(conn, version, None)

        conn.cursor_factory = _CrashAtCallCursor
        _CrashAtCallCursor.call_count = 0
        # "008"'s atomic step, in this implementation, issues: (1) the
        # migration's own CREATE TABLE database_metadata, (2) the purpose
        # INSERT, (3) the schema_migrations INSERT. Let (1) and (2)
        # succeed for real, blow up before (3).
        _CrashAtCallCursor.crash_at_call_number = 3
        try:
            with pytest.raises(RuntimeError, match="simulated crash"):
                bd._apply_step_atomically(conn, "008", "forward")
        finally:
            _CrashAtCallCursor.crash_at_call_number = None
            conn.cursor_factory = None

        assert not bd.database_metadata_table_exists(conn)
        rows = get_recorded_rows(conn)
        assert "008" not in [r[0] for r in rows]
    finally:
        conn.close()


def test_schema_sql_itself_passes_verify_on_a_fresh_database(disposable_db_name):
    """The specific manifest blocker from round 3 (schema.sql omitted from
    the manifest, so a fresh, correctly bootstrapped database with ZERO
    manual steps would have failed --verify) -- tested directly so it
    can't regress silently."""
    bd.cmd_bootstrap(disposable_db_name, "forward")
    ok, message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is True, message


# ===========================================================================
# Fresh / legacy / tracked state check -- all three real branches
# ===========================================================================

def test_existing_genuinely_empty_database_proceeds_through_normal_bootstrap(disposable_db_name):
    """Simulates the exact crash-before-'001' scenario the round-4 blocker
    was about: CREATE DATABASE succeeded, nothing else happened yet."""
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    state = bd.check_database_state(conn)
    conn.close()
    assert state == bd.FRESH_EMPTY

    message = bd.cmd_bootstrap(disposable_db_name, "forward")
    assert "forward" in message
    ok, _ = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is True


def test_existing_database_with_a_view_only_is_not_misclassified_as_empty(disposable_db_name):
    """FRESH_EMPTY's own state check must catch views/sequences/etc, not
    just plain tables -- a bare pg_tables count would misclassify this."""
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE VIEW a_lone_view AS SELECT 1 AS x")
        conn.commit()
        state = bd.check_database_state(conn)
        assert state == bd.LEGACY_UNTRACKED
    finally:
        conn.close()


def test_existing_database_with_only_a_sequence_is_not_misclassified_as_empty(disposable_db_name):
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SEQUENCE a_lone_sequence")
        conn.commit()
        state = bd.check_database_state(conn)
        assert state == bd.LEGACY_UNTRACKED
    finally:
        conn.close()


def test_existing_database_with_real_legacy_tables_is_refused_with_adopt_existing_message(disposable_db_name):
    conn = seed_legacy_baseline(disposable_db_name)
    conn.close()

    with pytest.raises(bd.MigrationStateError, match="--adopt-existing"):
        bd.cmd_bootstrap(disposable_db_name, "forward")

    # Confirmed refused, not partially bootstrapped.
    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert not bd._schema_migrations_exists(conn)
    finally:
        conn.close()


def test_existing_tracked_database_migrates_forward_normally(disposable_db_name):
    """Seed a database that's been through '001'-'005' only (tracked, via
    this tooling's own valid path), then confirm normal bootstrap picks up
    from '006' onward."""
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    try:
        for version in ("001", "002", "003", "004", "005"):
            bd._apply_step_atomically(conn, version, None)
        state = bd.check_database_state(conn)
        assert state == bd.TRACKED
    finally:
        conn.close()

    message = bd.cmd_bootstrap(disposable_db_name, "forward")
    assert "008" in message

    conn = bd.connect_to_target(disposable_db_name)
    try:
        rows = get_recorded_rows(conn)
        assert [r[0] for r in rows] == ALL_VERSIONS
        for _v, _c, origin in rows:
            assert origin == "applied"
    finally:
        conn.close()


# ===========================================================================
# Bootstrap's own connection mechanics
# ===========================================================================

def test_maintenance_and_target_connections_derive_from_get_db_dsn(monkeypatch):
    """Setting DIFFUSION_DB_DSN to a non-default value and confirming both
    connection-parameter dicts reflect it -- via a distinguishing
    parameter psycopg2's parse_dsn preserves, not a real second server."""
    monkeypatch.setenv(
        "DIFFUSION_DB_DSN",
        "dbname=irrelevant user=postgres host=localhost port=5432 application_name=phase0_marker",
    )
    maintenance_params = bd._maintenance_connection_params()
    target_params = bd._target_connection_params("some_target_db")

    assert maintenance_params["application_name"] == "phase0_marker"
    assert target_params["application_name"] == "phase0_marker"
    assert maintenance_params["dbname"] == "postgres"  # overridden, never the base template's dbname
    assert target_params["dbname"] == "some_target_db"  # overridden to --database, not the base template's


def test_database_name_reaches_create_database_only_through_sql_identifier(disposable_db_name):
    """A database name containing a character that would be dangerous as
    raw string interpolation (a double quote) is handled safely via
    sql.Identifier, not as a SQL-injection vector -- confirmed by actually
    creating a database whose name contains one, then cleaning it up."""
    name_with_quote = disposable_db_name + '_has"quote'
    try:
        bd.create_database(name_with_quote)
        assert bd.database_exists(name_with_quote)
    finally:
        drop_database_if_exists(name_with_quote)


# ===========================================================================
# --adopt-existing
# ===========================================================================

def test_adopt_existing_succeeds_on_a_real_legacy_baseline(disposable_db_name):
    conn = seed_legacy_baseline(disposable_db_name)
    conn.close()

    message = bd.cmd_adopt_existing(disposable_db_name, "forward")
    assert "forward" in message

    conn = bd.connect_to_target(disposable_db_name)
    try:
        rows = get_recorded_rows(conn)
        assert [r[0] for r in rows] == ALL_VERSIONS
        origins = {r[0]: r[2] for r in rows}
        for version in LEGACY_VERSIONS:
            assert origins[version] == "legacy_adopted"
        assert origins["008"] == "applied"
        assert get_database_metadata_row(conn) == ("singleton", "forward")
    finally:
        conn.close()


def test_adopt_existing_refuses_on_missing_table_and_mutates_nothing(disposable_db_name):
    conn = seed_legacy_baseline(disposable_db_name)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE audit_log")
    conn.commit()
    conn.close()

    with pytest.raises(bd.MigrationStateError, match='"001"'):
        bd.cmd_adopt_existing(disposable_db_name, "forward")

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert not bd._schema_migrations_exists(conn)
        assert not bd.database_metadata_table_exists(conn)
    finally:
        conn.close()


def test_adopt_existing_refuses_on_already_tracked_database(disposable_db_name):
    bd.cmd_bootstrap(disposable_db_name, "forward")
    with pytest.raises(bd.MigrationStateError, match="already tracked"):
        bd.cmd_adopt_existing(disposable_db_name, "forward")


def test_normal_bootstrap_refuses_with_exact_corrective_command_against_untracked(disposable_db_name):
    conn = seed_legacy_baseline(disposable_db_name)
    conn.close()

    with pytest.raises(
        bd.MigrationStateError,
        match=f"--adopt-existing --database {disposable_db_name} --purpose forward",
    ):
        bd.cmd_bootstrap(disposable_db_name, "forward")


# ===========================================================================
# Full legacy-adoption checklist, one migration at a time
# ===========================================================================

def _seed_and_falsify(database_name, falsify):
    conn = seed_legacy_baseline(database_name)
    falsify(conn)
    conn.close()


def test_adoption_checklist_catches_001_missing_table(disposable_db_name):
    _seed_and_falsify(disposable_db_name, lambda conn: _exec(conn, "DROP TABLE audit_log"))
    with pytest.raises(bd.MigrationStateError, match='"001"'):
        bd.cmd_adopt_existing(disposable_db_name, "forward")


def test_adoption_checklist_catches_002_missing_table(disposable_db_name):
    _seed_and_falsify(disposable_db_name, lambda conn: _exec(conn, "DROP TABLE entity_aliases"))
    with pytest.raises(bd.MigrationStateError, match='"002"'):
        bd.cmd_adopt_existing(disposable_db_name, "forward")


def test_adoption_checklist_catches_003_column_that_should_have_been_dropped(disposable_db_name):
    _seed_and_falsify(
        disposable_db_name,
        lambda conn: _exec(conn, "ALTER TABLE catalysts ADD COLUMN canonicalization_completed_at TIMESTAMPTZ"),
    )
    with pytest.raises(bd.MigrationStateError, match='"003"'):
        bd.cmd_adopt_existing(disposable_db_name, "forward")


def test_adoption_checklist_catches_004_missing_column(disposable_db_name):
    _seed_and_falsify(disposable_db_name, lambda conn: _exec(conn, "ALTER TABLE extracted_events DROP COLUMN observed_value_low"))
    with pytest.raises(bd.MigrationStateError, match='"004"'):
        bd.cmd_adopt_existing(disposable_db_name, "forward")


def test_adoption_checklist_catches_005_missing_column(disposable_db_name):
    _seed_and_falsify(disposable_db_name, lambda conn: _exec(conn, "ALTER TABLE catalyst_processing_runs DROP COLUMN processing_issues"))
    with pytest.raises(bd.MigrationStateError, match='"005"'):
        bd.cmd_adopt_existing(disposable_db_name, "forward")


def test_adoption_checklist_catches_006_missing_column(disposable_db_name):
    _seed_and_falsify(disposable_db_name, lambda conn: _exec(conn, "ALTER TABLE arm_outcomes DROP COLUMN exit_reason"))
    with pytest.raises(bd.MigrationStateError, match='"006"'):
        bd.cmd_adopt_existing(disposable_db_name, "forward")


def test_adoption_checklist_catches_007_missing_table(disposable_db_name):
    _seed_and_falsify(disposable_db_name, lambda conn: _exec(conn, "DROP TABLE experiment_catalysts"))
    with pytest.raises(bd.MigrationStateError, match='"007"'):
        bd.cmd_adopt_existing(disposable_db_name, "forward")


def _exec(conn, sql_text):
    with conn.cursor() as cur:
        cur.execute(sql_text)
    conn.commit()


# ===========================================================================
# --adopt-existing --purpose historical_replay rejected outright
# ===========================================================================

def test_adopt_existing_historical_replay_rejected_before_state_check_legacy_untracked(disposable_db_name):
    conn = seed_legacy_baseline(disposable_db_name)
    conn.close()
    with pytest.raises(ValueError, match="forward"):
        bd.cmd_adopt_existing(disposable_db_name, "historical_replay")
    # Confirm truly rejected before the state check ever ran -- nothing mutated.
    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert not bd._schema_migrations_exists(conn)
    finally:
        conn.close()


def test_adopt_existing_historical_replay_rejected_before_state_check_fresh_empty(disposable_db_name):
    bd.create_database(disposable_db_name)
    with pytest.raises(ValueError, match="forward"):
        bd.cmd_adopt_existing(disposable_db_name, "historical_replay")
    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert not bd._schema_migrations_exists(conn)
    finally:
        conn.close()


# ===========================================================================
# --verify
# ===========================================================================

def test_verify_passes_on_fresh_bootstrap(disposable_db_name):
    bd.cmd_bootstrap(disposable_db_name, "forward")
    ok, message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is True, message


def test_verify_passes_on_adopted_database(disposable_db_name):
    conn = seed_legacy_baseline(disposable_db_name)
    conn.close()
    bd.cmd_adopt_existing(disposable_db_name, "forward")
    ok, message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is True, message


def test_verify_fails_with_specific_message_on_missing_step(disposable_db_name):
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    for version in ("001", "002"):
        bd._apply_step_atomically(conn, version, None)
    conn.close()

    ok, message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is False
    assert "missing" in message.lower()
    assert "003" in message


def test_verify_fails_with_specific_message_on_extra_recorded_version(disposable_db_name):
    bd.cmd_bootstrap(disposable_db_name, "forward")
    conn = bd.connect_to_target(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (version, checksum_sha256, record_origin) "
                "VALUES ('009', 'deadbeef', 'applied')"
            )
        conn.commit()
    finally:
        conn.close()

    ok, message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is False
    assert "AHEAD" in message


def test_verify_fails_with_specific_message_on_checksum_mismatch(disposable_db_name):
    bd.cmd_bootstrap(disposable_db_name, "forward")
    conn = bd.connect_to_target(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE schema_migrations SET checksum_sha256 = 'not_the_real_checksum' WHERE version = '003'")
        conn.commit()
    finally:
        conn.close()

    ok, message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is False
    assert '"003"' in message
    assert "edited" in message


def test_verify_checksum_input_is_raw_bytes_not_normalized(
    disposable_db_name,
    tmp_path,
    monkeypatch,
):
    """Verification detects byte-only migration drift without ever mutating
    the real repository migration file."""
    bd.cmd_bootstrap(disposable_db_name, "forward")

    target = "migrations/004_range_valued_guidance.sql"
    real_path = bd._migration_path(target)
    original_bytes = real_path.read_bytes()

    mutated_bytes = original_bytes.replace(b"\n", b"\r\n")
    assert mutated_bytes != original_bytes

    copy_path = tmp_path / "004_range_valued_guidance.sql"
    copy_path.write_bytes(mutated_bytes)

    original_migration_path = bd._migration_path

    def _redirected(relative_path):
        if relative_path == target:
            return copy_path
        return original_migration_path(relative_path)

    monkeypatch.setattr(bd, "_migration_path", _redirected)

    ok, message = bd.cmd_verify(disposable_db_name, "forward")

    assert ok is False
    assert '"004"' in message
    assert real_path.read_bytes() == original_bytes


# ===========================================================================
# --verify is strictly read-only
# ===========================================================================

def test_verify_against_nonexistent_database_never_creates_it(disposable_db_name):
    assert not bd.database_exists(disposable_db_name)
    ok, message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is False
    assert not bd.database_exists(disposable_db_name)


def test_verify_against_incomplete_tracked_database_never_auto_applies_the_missing_suffix(disposable_db_name):
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    for version in ("001", "002", "003"):
        bd._apply_step_atomically(conn, version, None)
    rows_before = get_recorded_rows(conn)
    conn.close()

    ok, _message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is False

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert get_recorded_rows(conn) == rows_before
    finally:
        conn.close()


def test_verify_against_legacy_untracked_never_performs_adoption(disposable_db_name):
    conn = seed_legacy_baseline(disposable_db_name)
    conn.close()

    ok, message = bd.cmd_verify(disposable_db_name, "forward")
    assert ok is False
    assert "untracked" in message.lower()

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert not bd._schema_migrations_exists(conn)
        assert not bd.database_metadata_table_exists(conn)
    finally:
        conn.close()
