"""
Historical Replay Phase 0 (specs/historical-replay-phase0-implementation-spec-final.md),
Section 10's db_config.py tests. Every database this file touches comes
exclusively from the `disposable_db_name` fixture (conftest.py) -- never a
hardcoded or passed-through name.
"""
import psycopg2
import pytest

import bootstrap_database as bd
import db_config
from db_config import assert_database_purpose, DatabasePurposeError
from conftest import drop_database_if_exists

DATABASE_METADATA_SQL = (bd.BUILD_DIR / "migrations" / "008_database_metadata.sql").read_text(encoding="utf-8")


def _make_bare_database_with_metadata_table(name: str):
    """Creates `name` and applies ONLY the database_metadata DDL (no full
    migration history) -- db_config tests need just this one table, not a
    fully bootstrapped database."""
    bd.create_database(name)
    conn = bd.connect_to_target(name)
    with conn.cursor() as cur:
        cur.execute(DATABASE_METADATA_SQL)
    conn.commit()
    return conn


def _insert_purpose_row(conn, purpose: str, singleton_key: str = "singleton"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO database_metadata (singleton_key, database_purpose) VALUES (%s, %s)",
            (singleton_key, purpose),
        )
    conn.commit()


# ===========================================================================
# get_db_dsn()
# ===========================================================================

def test_get_db_dsn_uses_env_var_when_set(monkeypatch):
    monkeypatch.setenv("DIFFUSION_DB_DSN", "dbname=some_other_db user=someone host=example.invalid")
    assert db_config.get_db_dsn() == "dbname=some_other_db user=someone host=example.invalid"


def test_get_db_dsn_uses_standardized_default_when_unset(monkeypatch):
    monkeypatch.delenv("DIFFUSION_DB_DSN", raising=False)
    assert db_config.get_db_dsn() == "dbname=diffusion_experiment user=postgres"


# ===========================================================================
# assert_database_purpose
# ===========================================================================

def test_assert_database_purpose_exact_match_passes(disposable_db_name):
    conn = _make_bare_database_with_metadata_table(disposable_db_name)
    try:
        _insert_purpose_row(conn, "forward")
        assert_database_purpose(conn, "forward")  # must not raise
    finally:
        conn.close()


def test_assert_database_purpose_mismatch_raises(disposable_db_name):
    conn = _make_bare_database_with_metadata_table(disposable_db_name)
    try:
        _insert_purpose_row(conn, "historical_replay")
        with pytest.raises(DatabasePurposeError):
            assert_database_purpose(conn, "forward")
    finally:
        conn.close()


def test_assert_database_purpose_zero_rows_raises(disposable_db_name):
    conn = _make_bare_database_with_metadata_table(disposable_db_name)
    try:
        with pytest.raises(DatabasePurposeError, match="0 row"):
            assert_database_purpose(conn, "forward")
    finally:
        conn.close()
        drop_database_if_exists(disposable_db_name)


def test_assert_database_purpose_more_than_one_row_raises(disposable_db_name):
    """The CHECK constraint (singleton_key = 'singleton') blocks a second
    row with the SAME key via the primary key alone, but a second row with
    a DIFFERENT explicit singleton_key would violate the dedicated
    database_metadata_is_singleton CHECK -- tested separately below. This
    test instead directly demonstrates assert_database_purpose's own >1
    guard by inserting into a table with that CHECK constraint dropped,
    since the real table structure makes >1 row otherwise unreachable via
    plain INSERT -- an independent test of the query-result-shape guard
    itself, not of the schema constraint (which has its own dedicated
    tests below)."""
    conn = _make_bare_database_with_metadata_table(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE database_metadata DROP CONSTRAINT database_metadata_is_singleton")
            cur.execute("ALTER TABLE database_metadata DROP CONSTRAINT database_metadata_pkey")
        conn.commit()
        _insert_purpose_row(conn, "forward", singleton_key="singleton")
        _insert_purpose_row(conn, "historical_replay", singleton_key="not_the_singleton_key")
        with pytest.raises(DatabasePurposeError, match="2 row"):
            assert_database_purpose(conn, "forward")
    finally:
        conn.close()


def test_assert_database_purpose_missing_table_raises(disposable_db_name):
    bd.create_database(disposable_db_name)
    conn = bd.connect_to_target(disposable_db_name)
    try:
        with pytest.raises(DatabasePurposeError):
            assert_database_purpose(conn, "forward")
    finally:
        conn.close()


def test_assert_database_purpose_query_error_raises(disposable_db_name):
    """A real query error genuinely distinct from 'table missing' (that
    case has its own dedicated test above) -- closing the connection
    first and then querying it produces a real driver-level
    InterfaceError ('connection already closed'), not a schema-level
    'relation does not exist' in disguise."""
    conn = _make_bare_database_with_metadata_table(disposable_db_name)
    conn.close()
    with pytest.raises(DatabasePurposeError, match="closed"):
        assert_database_purpose(conn, "forward")


def test_assert_database_purpose_reads_the_row_not_the_dsn_string(disposable_db_name):
    """Two disposable databases, both reached through DSNs that share the
    identical host/port/user (only dbname differs, and dbname itself is a
    purely random, purpose-uncorrelated string from disposable_db_name) --
    confirms the check's answer differs solely because of the ROW
    content, never because of anything in the connection string or
    database name."""
    conn_a = _make_bare_database_with_metadata_table(disposable_db_name)
    other_name = f"{disposable_db_name}_2"
    try:
        _insert_purpose_row(conn_a, "forward")

        conn_b = _make_bare_database_with_metadata_table(other_name)
        try:
            _insert_purpose_row(conn_b, "historical_replay")

            assert_database_purpose(conn_a, "forward")  # passes
            assert_database_purpose(conn_b, "historical_replay")  # passes
            with pytest.raises(DatabasePurposeError):
                assert_database_purpose(conn_a, "historical_replay")
            with pytest.raises(DatabasePurposeError):
                assert_database_purpose(conn_b, "forward")
        finally:
            conn_b.close()
            drop_database_if_exists(other_name)
    finally:
        conn_a.close()


def test_assert_database_purpose_rejects_unknown_expected_purpose(disposable_db_name):
    conn = _make_bare_database_with_metadata_table(disposable_db_name)
    try:
        _insert_purpose_row(conn, "forward")
        with pytest.raises(ValueError):
            assert_database_purpose(conn, "some_third_purpose")
    finally:
        conn.close()


# ===========================================================================
# database_metadata's CHECK constraints, against a real database
# ===========================================================================

def test_database_metadata_second_row_with_distinct_singleton_key_fails(disposable_db_name):
    conn = _make_bare_database_with_metadata_table(disposable_db_name)
    try:
        _insert_purpose_row(conn, "forward")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_purpose_row(conn, "forward", singleton_key="a_different_key")
    finally:
        conn.rollback()
        conn.close()


def test_database_metadata_unrecognized_purpose_value_fails(disposable_db_name):
    conn = _make_bare_database_with_metadata_table(disposable_db_name)
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_purpose_row(conn, "some_unrecognized_value")
    finally:
        conn.rollback()
        conn.close()
