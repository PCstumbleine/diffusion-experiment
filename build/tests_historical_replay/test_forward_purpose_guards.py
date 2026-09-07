"""
Historical Replay Phase 0 (specs/historical-replay-phase0-implementation-spec-final.md),
Section 10's forward-purpose-guard tests for edgar_ingest_worker.py,
extraction_runner.py, seed_entities.py, and manual_resolve.py. Every
database this file touches comes exclusively from the `disposable_db_name`
fixture (conftest.py) -- never a hardcoded or passed-through name.
"""
import sys

import pytest

import bootstrap_database as bd
from db_config import DatabasePurposeError


def _row_count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        count = cur.fetchone()[0]
    conn.commit()
    return count


# ===========================================================================
# edgar_ingest_worker.py
# ===========================================================================

def test_edgar_ingest_worker_raises_before_any_write_against_a_historical_replay_database(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "historical_replay")

    import edgar_ingest_worker

    # USER_AGENT is bound once at module-import time from EDGAR_USER_AGENT
    # (os.environ.get(...) evaluated at import), so setting the env var
    # here would be a no-op on an already-imported (possibly cached)
    # module -- patch the module's own attribute directly instead, which
    # main()'s EdgarClient(USER_AGENT) call resolves at call time.
    monkeypatch.setattr(edgar_ingest_worker, "USER_AGENT", "Phase0Test test@example.com")
    monkeypatch.setattr(
        sys, "argv",
        ["edgar_ingest_worker.py", "--once", "--dsn", f"dbname={disposable_db_name} user=postgres"],
    )

    with pytest.raises(DatabasePurposeError):
        edgar_ingest_worker.main()

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert _row_count(conn, "raw_documents") == 0
        assert _row_count(conn, "catalysts") == 0
    finally:
        conn.close()


def test_edgar_ingest_worker_succeeds_past_the_guard_against_a_forward_database(disposable_db_name, monkeypatch):
    """Positive control: the SAME database, correctly purposed, must not
    be blocked by the guard -- it proceeds (and then legitimately exits
    non-zero/logs an error for the UNRELATED reason that the watchlist is
    empty, confirming the guard itself is not what's stopping it)."""
    bd.cmd_bootstrap(disposable_db_name, "forward")

    import edgar_ingest_worker

    monkeypatch.setattr(edgar_ingest_worker, "USER_AGENT", "Phase0Test test@example.com")
    monkeypatch.setattr(
        sys, "argv",
        ["edgar_ingest_worker.py", "--once", "--dsn", f"dbname={disposable_db_name} user=postgres"],
    )

    with pytest.raises(SystemExit) as exc_info:
        edgar_ingest_worker.main()
    # sys.exit(1) for "watchlist is empty" -- reached PAST the purpose guard.
    assert exc_info.value.code == 1


# ===========================================================================
# extraction_runner.py
# ===========================================================================

def test_extraction_runner_raises_before_any_write_against_a_historical_replay_database(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "historical_replay")

    import extraction_runner

    monkeypatch.setattr(
        sys, "argv",
        ["extraction_runner.py", "--dsn", f"dbname={disposable_db_name} user=postgres"],
    )

    with pytest.raises(DatabasePurposeError):
        extraction_runner.main()

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert _row_count(conn, "extraction_runs") == 0
    finally:
        conn.close()


# ===========================================================================
# seed_entities.py
# ===========================================================================

def test_seed_entities_purpose_argument_is_required(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "forward")

    import seed_entities

    monkeypatch.setattr(
        sys, "argv",
        ["seed_entities.py", "--dsn", f"dbname={disposable_db_name} user=postgres"],
    )
    with pytest.raises(SystemExit) as exc_info:
        seed_entities.main()
    assert exc_info.value.code != 0  # argparse's own required-argument enforcement


def test_seed_entities_raises_before_any_write_against_mismatched_purpose(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "historical_replay")

    import seed_entities

    monkeypatch.setattr(
        sys, "argv",
        ["seed_entities.py", "--dsn", f"dbname={disposable_db_name} user=postgres", "--purpose", "forward"],
    )
    with pytest.raises(DatabasePurposeError):
        seed_entities.main()

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert _row_count(conn, "entities") == 0
        assert _row_count(conn, "watchlist_membership") == 0
    finally:
        conn.close()


def test_seed_entities_succeeds_past_the_guard_with_matching_purpose(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "forward")

    import seed_entities

    monkeypatch.setattr(
        sys, "argv",
        ["seed_entities.py", "--dsn", f"dbname={disposable_db_name} user=postgres", "--purpose", "forward"],
    )
    seed_entities.main()  # must not raise

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert _row_count(conn, "entities") > 0  # the real 108-company watchlist got seeded
    finally:
        conn.close()


# ===========================================================================
# manual_resolve.py
# ===========================================================================

def test_manual_resolve_purpose_argument_is_required(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "forward")

    import manual_resolve

    monkeypatch.setattr(
        sys, "argv",
        ["manual_resolve.py", "--dsn", f"dbname={disposable_db_name} user=postgres", "list"],
    )
    with pytest.raises(SystemExit) as exc_info:
        manual_resolve.main()
    assert exc_info.value.code != 0


def test_manual_resolve_raises_before_any_write_against_mismatched_purpose(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "historical_replay")

    import manual_resolve

    monkeypatch.setattr(
        sys, "argv",
        ["manual_resolve.py", "--dsn", f"dbname={disposable_db_name} user=postgres", "--purpose", "forward", "list"],
    )
    with pytest.raises(DatabasePurposeError):
        manual_resolve.main()

    conn = bd.connect_to_target(disposable_db_name)
    try:
        assert _row_count(conn, "unresolved_entity_mentions") == 0
    finally:
        conn.close()


def test_manual_resolve_succeeds_past_the_guard_with_matching_purpose(disposable_db_name, monkeypatch, capsys):
    bd.cmd_bootstrap(disposable_db_name, "forward")

    import manual_resolve

    monkeypatch.setattr(
        sys, "argv",
        ["manual_resolve.py", "--dsn", f"dbname={disposable_db_name} user=postgres", "--purpose", "forward", "list"],
    )
    manual_resolve.main()  # must not raise (an empty pending-mentions list is a legitimate success)
