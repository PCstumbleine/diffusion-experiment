"""
Section 7f Implementation Spec, FINAL (specs/section7f-implementation-spec-final.md),
Section 10's experiment_catalysts tests: happy-path admission, the epoch-
consistency trigger, and the append-only enforcement (UPDATE/DELETE both
rejected).
"""
import uuid

import psycopg2
import pytest

from confirmatory_builder import get_confirmatory_catalyst_universe


def make_experiment(conn, scoring_epoch="epoch-1", cohort_type="confirmatory"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO experiments (name, scoring_epoch, cohort_type) VALUES (%s, %s, %s) "
            "RETURNING experiment_id",
            ("test experiment", scoring_epoch, cohort_type),
        )
        return cur.fetchone()[0]


def make_catalyst(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_documents (source_name, document_type, raw_content, content_hash) "
            "VALUES ('test', '8-K', 'test content', %s) RETURNING document_id",
            (str(uuid.uuid4()),),
        )
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO catalysts (originating_document_id) VALUES (%s) RETURNING catalyst_id",
            (doc_id,),
        )
        return cur.fetchone()[0]


def admit(conn, experiment_id, scoring_epoch, catalyst_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO experiment_catalysts (experiment_id, scoring_epoch, catalyst_id) VALUES (%s, %s, %s)",
            (experiment_id, scoring_epoch, catalyst_id),
        )


def test_happy_path_admission(conn):
    experiment_id = make_experiment(conn, scoring_epoch="epoch-1")
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT catalyst_id FROM experiment_catalysts WHERE experiment_id = %s",
            (experiment_id,),
        )
        rows = [r[0] for r in cur.fetchall()]
    assert rows == [catalyst_id]


def test_scoring_epoch_mismatch_against_the_experiments_own_value_is_rejected(conn):
    experiment_id = make_experiment(conn, scoring_epoch="epoch-1")
    catalyst_id = make_catalyst(conn)
    with pytest.raises(psycopg2.errors.RaiseException):
        admit(conn, experiment_id, "epoch-WRONG", catalyst_id)


def test_update_is_rejected(conn):
    experiment_id = make_experiment(conn, scoring_epoch="epoch-1")
    catalyst_1 = make_catalyst(conn)
    catalyst_2 = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_1)

    with conn.cursor() as cur, pytest.raises(psycopg2.errors.RaiseException):
        cur.execute(
            "UPDATE experiment_catalysts SET catalyst_id = %s WHERE experiment_id = %s AND catalyst_id = %s",
            (catalyst_2, experiment_id, catalyst_1),
        )


def test_delete_is_rejected(conn):
    experiment_id = make_experiment(conn, scoring_epoch="epoch-1")
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)

    with conn.cursor() as cur, pytest.raises(psycopg2.errors.RaiseException):
        cur.execute(
            "DELETE FROM experiment_catalysts WHERE experiment_id = %s AND catalyst_id = %s",
            (experiment_id, catalyst_id),
        )


# ---------------------------------------------------------------------------
# get_confirmatory_catalyst_universe
# ---------------------------------------------------------------------------

def test_get_confirmatory_catalyst_universe_returns_admitted_catalysts(conn):
    experiment_id = make_experiment(conn, scoring_epoch="epoch-1")
    c1, c2 = make_catalyst(conn), make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c1)
    admit(conn, experiment_id, "epoch-1", c2)

    result = get_confirmatory_catalyst_universe(conn, experiment_id, "epoch-1")
    assert set(result) == {c1, c2}


def test_get_confirmatory_catalyst_universe_rejects_mismatched_scoring_epoch(conn):
    experiment_id = make_experiment(conn, scoring_epoch="epoch-1")
    with pytest.raises(ValueError, match="does not match"):
        get_confirmatory_catalyst_universe(conn, experiment_id, "epoch-WRONG")


def test_get_confirmatory_catalyst_universe_rejects_nonexistent_experiment(conn):
    with pytest.raises(ValueError, match="does not exist"):
        get_confirmatory_catalyst_universe(conn, str(uuid.uuid4()), "epoch-1")
