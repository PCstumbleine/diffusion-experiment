"""db_config.py -- the one place a database connection string is decided.

The DSN is routing information ONLY. It is never treated as proof of
which database (forward vs. historical_replay) a connection actually
reaches -- that is database_metadata's job (Section 2 of
specs/historical-replay-phase0-implementation-spec-final.md), checked from
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
