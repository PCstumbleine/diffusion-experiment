"""
A second code-review round (ChatGPT, against v3) flagged a real gap: every
individual foreign key on arm_entries (candidate_id, model_decision_id,
event_version_id, instrument_id, entry_quote_snapshot_id) was valid on its
own, but nothing enforced they were MUTUALLY consistent -- e.g. a row could
reference a candidate_id belonging to a different event_version_id than its
own, a model_decision_id belonging to a different candidate than its own
candidate_id, or an entry/exit quote snapshot for a different instrument
than the entry is actually about. Individually-valid-but-contradictory
references are exactly the kind of corruption that produces plausible-
looking, silently wrong research data rather than an obvious crash. Fixed
with trigger functions (check_arm_entry_candidate_consistency,
check_entry_quote_matches_instrument, check_exit_quote_matches_entry_instrument)
in schema.sql; this file confirms each one actually rejects the
inconsistent case rather than just existing.

A third review round found two more real gaps in the same area, both
fixed and tested here too:
  - check_arm_entry_candidate_consistency now also rejects a candidate for
    one entity being paired with a traded instrument_id for a DIFFERENT
    entity (e.g. an NVIDIA candidate entered against an Apple instrument)
    -- same class of bug, one level deeper.
  - check_arm_entry_instrument only enforced "every non-E arm needs an
    instrument", not the reverse ("arm E must NOT have one") -- so a
    cash-equivalent entry could still accidentally carry a security. Now
    enforces both halves of the stated invariant.
"""
import uuid
import psycopg2
import pytest
from conftest import make_entity


def make_event_version(conn, entity_id=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_documents (source_name, document_type, raw_content, content_hash)
            VALUES ('test', '8-K', 'test content', %s) RETURNING document_id
            """,
            (str(uuid.uuid4()),),
        )
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO catalysts (originating_document_id, issuer_entity_id) VALUES (%s, %s) RETURNING catalyst_id",
            (doc_id, entity_id),
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


def record_decision(conn, candidate_id, model_id="arm_a_llm", selected=True, score=0.9):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_candidate_decisions
                (candidate_id, model_id, model_version, score, selected, abstained, decision_at)
            VALUES (%s, %s, 'v1', %s, %s, false, now())
            RETURNING decision_id
            """,
            (candidate_id, model_id, score, selected),
        )
        return cur.fetchone()[0]


def make_instrument(conn, entity_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instruments (entity_id, exchange) VALUES (%s, 'TEST') RETURNING instrument_id",
            (entity_id,),
        )
        return cur.fetchone()[0]


def make_arm(conn, arm_code="A"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO experiments (name, scoring_epoch, cohort_type)
            VALUES ('test experiment', 'epoch-1', 'pilot') RETURNING experiment_id
            """
        )
        experiment_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO experiment_arms (experiment_id, arm_code, arm_label) VALUES (%s, %s, 'test arm') RETURNING arm_id",
            (experiment_id, arm_code),
        )
        return cur.fetchone()[0]


def make_quote(conn, instrument_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO quote_snapshots (instrument_id, quote_timestamp, data_provider, trading_session)
            VALUES (%s, now(), 'test_provider', 'regular')
            RETURNING quote_snapshot_id
            """,
            (instrument_id,),
        )
        return cur.fetchone()[0]


def insert_arm_entry(conn, arm_id, event_version_id, instrument_id, candidate_id=None,
                      model_decision_id=None, entry_quote_snapshot_id=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arm_entries
                (arm_id, event_version_id, instrument_id, candidate_id, model_decision_id,
                 entry_timestamp, entry_quote_snapshot_id, direction)
            VALUES (%s, %s, %s, %s, %s, now(), %s, 'long')
            RETURNING entry_id
            """,
            (arm_id, event_version_id, instrument_id, candidate_id, model_decision_id, entry_quote_snapshot_id),
        )
        return cur.fetchone()[0]


def test_arm_entry_with_consistent_candidate_and_decision_succeeds(conn):
    """The happy path must still work -- these triggers should reject only
    genuine mismatches, not correct data."""
    entity = make_entity(conn, "Co1")
    event_version_id = make_event_version(conn, entity)
    instrument_id = make_instrument(conn, entity)
    arm_id = make_arm(conn, "A")
    candidate_id = make_candidate(conn, event_version_id, entity)
    decision_id = record_decision(conn, candidate_id)

    entry_id = insert_arm_entry(conn, arm_id, event_version_id, instrument_id,
                                 candidate_id=candidate_id, model_decision_id=decision_id)
    assert entry_id is not None


def test_arm_entry_rejects_candidate_from_a_different_event_version(conn):
    """The exact corruption a review round flagged: candidate_id is
    individually a valid FK, but points at a candidate belonging to a
    DIFFERENT event_version_id than the entry's own."""
    entity = make_entity(conn, "Co2")
    event_version_1 = make_event_version(conn, entity)
    event_version_2 = make_event_version(conn, entity)
    instrument_id = make_instrument(conn, entity)
    arm_id = make_arm(conn, "A")
    candidate_on_event_1 = make_candidate(conn, event_version_1, entity)

    with pytest.raises(psycopg2.errors.RaiseException):
        insert_arm_entry(conn, arm_id, event_version_2, instrument_id, candidate_id=candidate_on_event_1)


def test_arm_entry_rejects_decision_belonging_to_a_different_candidate(conn):
    """model_decision_id is individually a valid FK, but the decision it
    points at belongs to a different candidate than the entry's own
    candidate_id."""
    entity_1, entity_2 = make_entity(conn, "Co3"), make_entity(conn, "Co4")
    event_version_id = make_event_version(conn, entity_1)
    instrument_id = make_instrument(conn, entity_1)
    arm_id = make_arm(conn, "A")
    candidate_1 = make_candidate(conn, event_version_id, entity_1)
    candidate_2 = make_candidate(conn, event_version_id, entity_2)
    decision_for_candidate_2 = record_decision(conn, candidate_2)

    with pytest.raises(psycopg2.errors.RaiseException):
        insert_arm_entry(conn, arm_id, event_version_id, instrument_id,
                          candidate_id=candidate_1, model_decision_id=decision_for_candidate_2)


def test_arm_entry_rejects_model_decision_id_with_no_candidate_id(conn):
    """A decision necessarily belongs to some candidate; an entry that sets
    model_decision_id but leaves candidate_id null is unrepresentable
    consistently and must be rejected, not silently accepted."""
    entity = make_entity(conn, "Co5")
    event_version_id = make_event_version(conn, entity)
    instrument_id = make_instrument(conn, entity)
    arm_id = make_arm(conn, "A")
    candidate_id = make_candidate(conn, event_version_id, entity)
    decision_id = record_decision(conn, candidate_id)

    with pytest.raises(psycopg2.errors.RaiseException):
        insert_arm_entry(conn, arm_id, event_version_id, instrument_id,
                          candidate_id=None, model_decision_id=decision_id)


def test_arm_entry_rejects_entry_quote_for_a_different_instrument(conn):
    """entry_quote_snapshot_id is individually a valid FK to quote_snapshots,
    but that quote is for a different instrument than the entry itself."""
    entity_1, entity_2 = make_entity(conn, "Co6"), make_entity(conn, "Co7")
    event_version_id = make_event_version(conn, entity_1)
    instrument_1 = make_instrument(conn, entity_1)
    instrument_2 = make_instrument(conn, entity_2)
    arm_id = make_arm(conn, "C")
    quote_for_instrument_2 = make_quote(conn, instrument_2)

    with pytest.raises(psycopg2.errors.RaiseException):
        insert_arm_entry(conn, arm_id, event_version_id, instrument_1,
                          entry_quote_snapshot_id=quote_for_instrument_2)


def test_arm_entry_accepts_entry_quote_for_the_matching_instrument(conn):
    entity = make_entity(conn, "Co8")
    event_version_id = make_event_version(conn, entity)
    instrument_id = make_instrument(conn, entity)
    arm_id = make_arm(conn, "C")
    quote_id = make_quote(conn, instrument_id)

    entry_id = insert_arm_entry(conn, arm_id, event_version_id, instrument_id,
                                 entry_quote_snapshot_id=quote_id)
    assert entry_id is not None


def test_arm_outcome_rejects_exit_quote_for_a_different_instrument_than_its_entry(conn):
    """Same class of bug, one hop further: arm_outcomes.exit_quote_snapshot_id
    is a valid FK on its own, but must match the INSTRUMENT of the
    arm_entries row it's exiting, not just be some valid quote."""
    entity_1, entity_2 = make_entity(conn, "Co9"), make_entity(conn, "Co10")
    event_version_id = make_event_version(conn, entity_1)
    instrument_1 = make_instrument(conn, entity_1)
    instrument_2 = make_instrument(conn, entity_2)
    arm_id = make_arm(conn, "C")
    entry_id = insert_arm_entry(conn, arm_id, event_version_id, instrument_1)
    quote_for_instrument_2 = make_quote(conn, instrument_2)

    with conn.cursor() as cur, pytest.raises(psycopg2.errors.RaiseException):
        cur.execute(
            """
            INSERT INTO arm_outcomes (entry_id, horizon_label, exit_timestamp, exit_quote_snapshot_id)
            VALUES (%s, '1day', now(), %s)
            """,
            (entry_id, quote_for_instrument_2),
        )


def test_arm_outcome_accepts_exit_quote_matching_its_entrys_instrument(conn):
    entity = make_entity(conn, "Co11")
    event_version_id = make_event_version(conn, entity)
    instrument_id = make_instrument(conn, entity)
    arm_id = make_arm(conn, "C")
    entry_id = insert_arm_entry(conn, arm_id, event_version_id, instrument_id)
    quote_id = make_quote(conn, instrument_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arm_outcomes (entry_id, horizon_label, exit_timestamp, exit_quote_snapshot_id)
            VALUES (%s, '1day', now(), %s) RETURNING outcome_id
            """,
            (entry_id, quote_id),
        )
        assert cur.fetchone()[0] is not None


def test_arm_entry_rejects_candidate_and_instrument_for_different_entities(conn):
    """New finding from a third review round: candidate_id and instrument_id
    were each individually valid, but nothing stopped them belonging to
    two DIFFERENT companies entirely -- e.g. a candidate about NVIDIA
    entered against an Apple instrument. Both foreign keys are valid; the
    combination is nonsensical research data."""
    nvidia = make_entity(conn, "NVIDIA-like Co")
    apple = make_entity(conn, "Apple-like Co")
    event_version_id = make_event_version(conn, nvidia)
    arm_id = make_arm(conn, "A")
    candidate_for_nvidia = make_candidate(conn, event_version_id, nvidia)
    apple_instrument = make_instrument(conn, apple)

    with pytest.raises(psycopg2.errors.RaiseException):
        insert_arm_entry(conn, arm_id, event_version_id, apple_instrument, candidate_id=candidate_for_nvidia)


def test_arm_entry_accepts_candidate_and_instrument_for_the_same_entity(conn):
    """The happy path must still work: a candidate and the instrument it's
    actually traded through, both belonging to the same company."""
    entity = make_entity(conn, "Co12")
    event_version_id = make_event_version(conn, entity)
    arm_id = make_arm(conn, "A")
    candidate_id = make_candidate(conn, event_version_id, entity)
    instrument_id = make_instrument(conn, entity)

    entry_id = insert_arm_entry(conn, arm_id, event_version_id, instrument_id, candidate_id=candidate_id)
    assert entry_id is not None


def test_arm_e_entry_rejects_an_instrument(conn):
    """check_arm_entry_instrument previously only enforced the non-E half
    of "instrument required for every arm except E" -- a cash-equivalent
    entry could still accidentally carry a security. Now enforces both
    halves of the stated invariant."""
    entity = make_entity(conn, "Co13")
    event_version_id = make_event_version(conn, entity)
    instrument_id = make_instrument(conn, entity)
    arm_e_id = make_arm(conn, "E")

    with pytest.raises(psycopg2.errors.RaiseException):
        insert_arm_entry(conn, arm_e_id, event_version_id, instrument_id)


def test_arm_e_entry_accepts_null_instrument(conn):
    entity = make_entity(conn, "Co14")
    event_version_id = make_event_version(conn, entity)
    arm_e_id = make_arm(conn, "E")

    entry_id = insert_arm_entry(conn, arm_e_id, event_version_id, instrument_id=None)
    assert entry_id is not None
