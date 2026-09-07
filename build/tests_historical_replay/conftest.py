"""
Historical Replay Phase 0's OWN, deliberately isolated test conftest.

This directory is a SIBLING of build/tests/, not a subdirectory of it --
pytest's conftest.py collection is directory-hierarchy-based (a conftest
applies to its own directory and subdirectories, never to siblings), so
running `pytest tests_historical_replay/` never loads build/tests/conftest.py
and its autouse `clean_db` fixture, which unconditionally truncates
`dbname=diffusion_experiment` before every test in that OTHER directory.
Verified directly (not assumed from pytest docs) before this suite was
built: a throwaway sentinel fixture placed in one directory's conftest.py
was confirmed to never fire when only the sibling directory was selected.

This file defines NO autouse fixture, and DB_DSN/`diffusion_experiment`
appear nowhere in it. Every database this suite touches is created,
migrated, adopted, verified, and dropped by the tests themselves, through
the `disposable_db_name` fixture below -- never a hardcoded or
passed-through name. Per the spec's own frozen rollout sequence (Section
6, step B), this test suite must never touch the real diffusion_experiment
database, under any circumstance.
"""
import os
import sys
import uuid

import psycopg2
import psycopg2.extensions
import pytest
from psycopg2 import sql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db_config  # noqa: E402


def _maintenance_conn():
    params = psycopg2.extensions.parse_dsn(db_config.get_db_dsn())
    params["dbname"] = "postgres"
    conn = psycopg2.connect(**params)
    conn.autocommit = True
    return conn


def drop_database_if_exists(name: str) -> None:
    """Force-disconnects any lingering session on `name` first (a test's
    own connection to the disposable database, if not already closed by
    the test) so DROP DATABASE doesn't fail with 'database is being
    accessed by other users'."""
    conn = _maintenance_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    finally:
        conn.close()


@pytest.fixture()
def disposable_db_name():
    """Generates its own throwaway database name and hard-fails if that
    name ever resolves to diffusion_experiment -- every destructive or
    adoption-related test in this suite must obtain its target
    EXCLUSIVELY through this fixture, never a hardcoded or passed-through
    name. Does NOT create the database itself (some tests need it to not
    exist yet, e.g. testing CREATE DATABASE / FRESH_EMPTY-from-scratch;
    others create/seed it explicitly). Drops it at teardown if it exists,
    regardless of what state the test left it in."""
    name = f"phase0_disposable_{uuid.uuid4().hex}"
    if name == "diffusion_experiment":
        raise RuntimeError(
            "disposable_db_name generated the literal name 'diffusion_experiment' -- refusing to "
            "proceed. This should be statistically impossible; treat this as a serious bug, not "
            "a fluke to retry past."
        )
    yield name
    drop_database_if_exists(name)
