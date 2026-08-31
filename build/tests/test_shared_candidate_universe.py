"""
Targets the v2.1 fix that made Arm A vs Arm G a meaningful comparison in
the first place: both must rank/select from the IDENTICAL candidate pool.

A code review round caught two real problems with the ORIGINAL version of
this test file, both fixed here:
  1. Its comment claimed the schema made it "structurally impossible for
     one arm to see a candidate the other didn't" -- false. The schema
     guarantees a shared POOL, not that both models actually decided on
     every member of it. candidate_coverage.py is the real completeness
     check that claim was missing, and this file now tests it.
  2. Its own fixture created two candidates (c1, c2) for the SAME entity on
     one event -- exactly the duplicate-candidate bug a review round
     separately flagged as unconstrained in the schema. candidate_signals
     now has UNIQUE(event_version_id, entity_id); this file uses two
     distinct entities, and a new test confirms the duplicate is rejected.
"""
import uuid
import psycopg2
import pytest
import sys
import os
from conftest import make_entity

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from candidate_coverage import find_incomplete_coverage, assert_full_coverage


def make_event_version(conn):
    with conn.cursor() as cur:
        # catalysts requires an originating_document_id; make a minimal document first.
        cur.execute(
            """
            INSERT INTO raw_documents (source_name, document_type, raw_content, content_hash)
            VALUES ('test', '8-K', 'test content', %s) RETURNING document_id
            """,
            (str(uuid.uuid4()),),
        )
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO catalysts (originating_document_id) VALUES (%s) RETURNING catalyst_id",
            (doc_id,),
        )
        catalyst_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO canonical_events (catalyst_id, event_category) VALUES (%s, 'guidance_revision') RETURNING canonical_event_id",
            (catalyst_id,),
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO event_versions (canonical_event_id, version_number, decision_at) VALUES (%s, 1, now()) RETURNING event_version_id",
            (event_id,),
        )
        return cur.fetchone()[0]


def make_candidate(conn, event_version_id, entity_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO candidate_signals (event_version_id, entity_id, eligibility_status, policy_version, decision_timestamp)
            VALUES (%s, %s, 'eligible', 'v1', now())
            RETURNING candidate_id
            """,
            (event_version_id, entity_id),
        )
        return cur.fetchone()[0]


def record_decision(conn, candidate_id, model_id, selected, score, model_version="v1"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_candidate_decisions
                (candidate_id, model_id, model_version, score, selected, abstained, decision_at)
            VALUES (%s, %s, %s, %s, %s, false, now())
            """,
            (candidate_id, model_id, model_version, score, selected),
        )


def test_arm_a_and_arm_g_decisions_reference_the_same_candidate_pool(conn):
    entity_1, entity_2 = make_entity(conn, "Co1"), make_entity(conn, "Co2")
    event_version_id = make_event_version(conn)
    c1 = make_candidate(conn, event_version_id, entity_1)
    c2 = make_candidate(conn, event_version_id, entity_2)

    record_decision(conn, c1, "arm_a_llm", selected=True, score=0.9)
    record_decision(conn, c1, "arm_g_mechanical", selected=False, score=0.3)
    record_decision(conn, c2, "arm_a_llm", selected=False, score=0.2)
    record_decision(conn, c2, "arm_g_mechanical", selected=True, score=0.8)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT model_id FROM model_candidate_decisions
            WHERE candidate_id IN (
                SELECT candidate_id FROM candidate_signals WHERE event_version_id = %s
            )
            """,
            (event_version_id,),
        )
        models_seen = {row[0] for row in cur.fetchall()}
    assert models_seen == {"arm_a_llm", "arm_g_mechanical"}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM candidate_signals WHERE event_version_id = %s",
            (event_version_id,),
        )
        candidate_count = cur.fetchone()[0]
    assert candidate_count == 2

    # The actual completeness check (not just "same pool exists"):
    assert find_incomplete_coverage(conn, event_version_id, ["arm_a_llm", "arm_g_mechanical"], "v1") == []
    assert_full_coverage(conn, event_version_id, ["arm_a_llm", "arm_g_mechanical"], "v1")  # should not raise


def test_incomplete_coverage_is_detected_not_silently_accepted(conn):
    """The exact gap the original test's 'structurally impossible' comment
    was wrong about: nothing in the schema stops Arm G from deciding on a
    candidate that Arm A never got a decision recorded for."""
    entity_1, entity_2 = make_entity(conn, "Co3"), make_entity(conn, "Co4")
    event_version_id = make_event_version(conn)
    c1 = make_candidate(conn, event_version_id, entity_1)
    c2 = make_candidate(conn, event_version_id, entity_2)

    record_decision(conn, c1, "arm_a_llm", selected=True, score=0.9)
    record_decision(conn, c1, "arm_g_mechanical", selected=False, score=0.3)
    # c2 only ever gets a G decision -- A silently never weighed in.
    record_decision(conn, c2, "arm_g_mechanical", selected=True, score=0.8)

    incomplete = find_incomplete_coverage(conn, event_version_id, ["arm_a_llm", "arm_g_mechanical"], "v1")
    assert incomplete == [c2]

    with pytest.raises(ValueError, match="missing a"):
        assert_full_coverage(conn, event_version_id, ["arm_a_llm", "arm_g_mechanical"], "v1")


def test_candidate_signals_rejects_duplicate_entity_for_same_event(conn):
    """UNIQUE(event_version_id, entity_id) -- the fix for the duplicate-
    candidate gap. The ORIGINAL version of this test file's own fixture
    accidentally did exactly what this now confirms is rejected."""
    entity = make_entity(conn, "Co5")
    event_version_id = make_event_version(conn)
    make_candidate(conn, event_version_id, entity)

    with pytest.raises(psycopg2.errors.UniqueViolation):
        make_candidate(conn, event_version_id, entity)


def test_a_model_cannot_record_two_decisions_on_the_same_candidate(conn):
    """UNIQUE (candidate_id, model_id, model_version) enforces one decision
    per model per candidate — otherwise 'which decision counts' becomes
    ambiguous and the A-vs-G comparison stops being reproducible."""
    entity = make_entity(conn, "Co6")
    event_version_id = make_event_version(conn)
    c1 = make_candidate(conn, event_version_id, entity)
    record_decision(conn, c1, "arm_a_llm", selected=True, score=0.9)

    with pytest.raises(psycopg2.errors.UniqueViolation):
        record_decision(conn, c1, "arm_a_llm", selected=False, score=0.1)


def test_a_model_cannot_both_select_and_abstain(conn):
    """New CHECK constraint -- another confirmed gap."""
    entity = make_entity(conn, "Co7")
    event_version_id = make_event_version(conn)
    c1 = make_candidate(conn, event_version_id, entity)
    with conn.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO model_candidate_decisions
                (candidate_id, model_id, model_version, score, selected, abstained, decision_at)
            VALUES (%s, 'arm_a_llm', 'v1', 0.5, true, true, now())
            """,
            (c1,),
        )
