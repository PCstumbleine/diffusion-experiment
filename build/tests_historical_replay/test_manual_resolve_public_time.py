"""
Historical Replay Phase 1A (specs/historical-replay-phase1a-implementation-spec-final.md),
Section 6's manual_resolve.py integration tests. Every database this file
touches comes exclusively from the `disposable_db_name` fixture
(conftest.py) -- never a hardcoded or passed-through name.
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2.extras
import pytest

import bootstrap_database as bd
import entity_resolution
from public_time_provenance import HistoricalPublicTimeError


def make_entity(conn, name):
    entity_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_id, legal_name) VALUES (%s, %s)", (entity_id, name))
    return entity_id


def make_raw_document(conn, raw_content, canonical_first_public_at=None, first_public_timestamp_precision=None):
    doc_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_documents (document_id, source_name, document_type, raw_content, content_hash, "
            "canonical_first_public_at, first_public_timestamp_precision) "
            "VALUES (%s, 'test', '8-K', %s, 'test-hash', %s, %s)",
            (doc_id, raw_content, canonical_first_public_at, first_public_timestamp_precision),
        )
    return doc_id


def make_extraction_run(conn, document_id, cleaned_llm_output):
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extraction_runs (extraction_run_id, document_id, extraction_prompt_version, "
            "extractor_model_id, extractor_model_version, status, raw_llm_output, cleaned_llm_output, completed_at) "
            "VALUES (%s, %s, '1.2.0', 'test-model', 'test-version', 'success', %s, %s, now())",
            (run_id, document_id, psycopg2.extras.Json(cleaned_llm_output), psycopg2.extras.Json(cleaned_llm_output)),
        )
    return run_id


def log_relationship_mention(conn, issuer_id, counterparty_raw_name, document_id, extraction_run_id):
    """Builds a minimal event/relationship naming counterparty_raw_name
    (unresolved) and one already-resolved issuer, and updates the
    extraction_run's cleaned_llm_output to hold it, then logs the
    counterparty as an unresolved mention via the real resolver -- exactly
    what process_catalyst would have done during real ingestion."""
    raw_output = {
        "document_id": document_id, "extraction_prompt_version": "1.2.0",
        "events": [{
            "event_category": "supply_agreement", "catalyst_description": "x",
            "entities": [{"entity_name": "Issuer Co", "role": "issuer", "evidence_span": "Issuer Co"}],
            "relationships": [{
                "entity_a": "Issuer Co", "entity_b": counterparty_raw_name, "relationship_type": "supplier",
                "relationship_evidence": "explicit_named", "source_authority": "company",
                "document_explicitly_states_transmission_history": False,
                "evidence_span": f"Issuer Co supplies {counterparty_raw_name}",
                "raw_llm_relationship_score": 0.9,
            }],
            "surprise": None, "explicit_correction": False,
        }],
    }
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE extraction_runs SET raw_llm_output = %s, cleaned_llm_output = %s WHERE extraction_run_id = %s",
            (psycopg2.extras.Json(raw_output), psycopg2.extras.Json(raw_output), extraction_run_id),
        )
    entity_resolution.resolve_entity_name(conn, counterparty_raw_name, document_id, extraction_run_id)


def get_mention_id(conn, document_id):
    with conn.cursor() as cur:
        cur.execute("SELECT mention_id FROM unresolved_entity_mentions WHERE document_id = %s", (document_id,))
        return cur.fetchone()[0]


def make_bare_unresolved_mention(conn, document_id, extraction_run_id, raw_name="Some Counterparty Inc"):
    """A minimal unresolved_entity_mentions row with no need for a real
    relationship in cleaned_llm_output -- sufficient for the two
    historical_replay failure tests below, since
    resolve_relationship_public_time is called (and raises) BEFORE
    _write_backfilled_relationships ever inspects cleaned_llm_output's
    events/relationships."""
    mention_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO unresolved_entity_mentions (mention_id, raw_name, normalized_name, document_id, "
            "extraction_run_id) VALUES (%s, %s, %s, %s, %s)",
            (mention_id, raw_name, entity_resolution.normalize_entity_name(raw_name), document_id, extraction_run_id),
        )
    return mention_id


def run_manual_resolve_main(monkeypatch, argv):
    import manual_resolve
    monkeypatch.setattr(sys, "argv", argv)
    manual_resolve.main()


# ===========================================================================
# Normal propagation + equality (invariant 5)
# ===========================================================================

def test_resolving_a_mention_propagates_both_fields_and_they_equal_the_source(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "forward")
    conn = bd.connect_to_target(disposable_db_name)
    try:
        canonical = datetime(2026, 2, 1, 9, 30, tzinfo=timezone.utc)
        precision = timedelta(minutes=3)
        issuer_id = make_entity(conn, "Issuer Co")
        document_id = make_raw_document(
            conn, "Issuer Co supplies Formerly Unknown Corp.",
            canonical_first_public_at=canonical, first_public_timestamp_precision=precision,
        )
        extraction_run_id = make_extraction_run(conn, document_id, {})
        log_relationship_mention(conn, issuer_id, "Formerly Unknown Corp", document_id, extraction_run_id)
        mention_id = get_mention_id(conn, document_id)
        conn.commit()
    finally:
        conn.close()

    dsn = f"dbname={disposable_db_name} user=postgres"
    counterparty_entity_id = None
    # Use --new-entity so we don't need to pre-create the counterparty --
    # create_entity_and_resolve() is itself a real production code path.
    run_manual_resolve_main(monkeypatch, [
        "manual_resolve.py", "--dsn", dsn, "--purpose", "forward",
        "resolve", "--mention-id", str(mention_id), "--new-entity", "Formerly Unknown Corporation",
    ])

    conn = bd.connect_to_target(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT evidence_publicly_available_at, evidence_public_time_precision FROM entity_relationships"
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        evidence_publicly_available_at, evidence_public_time_precision = rows[0]
        # Invariant 5's equality requirement -- not just non-null.
        assert evidence_publicly_available_at == canonical
        assert evidence_public_time_precision == precision
    finally:
        conn.close()


# ===========================================================================
# historical_replay, precision missing
# ===========================================================================

def test_historical_replay_precision_missing_raises_and_writes_no_row(disposable_db_name, monkeypatch):
    bd.cmd_bootstrap(disposable_db_name, "historical_replay")
    conn = bd.connect_to_target(disposable_db_name)
    try:
        canonical = datetime(2026, 2, 1, 9, 30, tzinfo=timezone.utc)
        target_entity_id = make_entity(conn, "Some Resolvable Entity")
        document_id = make_raw_document(
            conn, "some content",
            canonical_first_public_at=canonical, first_public_timestamp_precision=None,  # missing
        )
        extraction_run_id = make_extraction_run(conn, document_id, {})
        mention_id = make_bare_unresolved_mention(conn, document_id, extraction_run_id)
        conn.commit()
    finally:
        conn.close()

    dsn = f"dbname={disposable_db_name} user=postgres"
    with pytest.raises(HistoricalPublicTimeError):
        run_manual_resolve_main(monkeypatch, [
            "manual_resolve.py", "--dsn", dsn, "--purpose", "historical_replay",
            "resolve", "--mention-id", str(mention_id), "--entity-id", target_entity_id,
        ])

    conn = bd.connect_to_target(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM entity_relationships")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT status FROM unresolved_entity_mentions WHERE mention_id = %s", (mention_id,))
            assert cur.fetchone()[0] == "unresolved"  # never marked resolved either
    finally:
        conn.close()


# ===========================================================================
# historical_replay, canonical missing -- the fallback-reintroduction
# regression test
# ===========================================================================

def test_historical_replay_canonical_missing_raises_and_writes_no_row(disposable_db_name, monkeypatch):
    """The regression test for the fallback-reintroduction hazard: a
    buggy implementation that reintroduces `canonical_first_public_at or
    resolution_time` ahead of the helper call would silently pass a
    non-NULL, resolution-time-derived value into
    resolve_relationship_public_time, which would then see a non-NULL
    canonical and a valid precision and pass it through with NO error --
    causing this test (which asserts the error IS raised) to fail. This
    exercises the real manual_resolve.py code path (main() -> resolve_mention
    -> _write_backfilled_relationships) against a real disposable
    historical_replay-purpose database, not the helper in isolation."""
    bd.cmd_bootstrap(disposable_db_name, "historical_replay")
    conn = bd.connect_to_target(disposable_db_name)
    try:
        precision = timedelta(minutes=5)  # valid, non-NULL, non-negative
        target_entity_id = make_entity(conn, "Some Resolvable Entity")
        document_id = make_raw_document(
            conn, "some content",
            canonical_first_public_at=None,  # missing -- this is the case that must be caught
            first_public_timestamp_precision=precision,
        )
        extraction_run_id = make_extraction_run(conn, document_id, {})
        mention_id = make_bare_unresolved_mention(conn, document_id, extraction_run_id)
        conn.commit()
    finally:
        conn.close()

    dsn = f"dbname={disposable_db_name} user=postgres"
    with pytest.raises(HistoricalPublicTimeError):
        run_manual_resolve_main(monkeypatch, [
            "manual_resolve.py", "--dsn", dsn, "--purpose", "historical_replay",
            "resolve", "--mention-id", str(mention_id), "--entity-id", target_entity_id,
        ])

    conn = bd.connect_to_target(disposable_db_name)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM entity_relationships")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT status FROM unresolved_entity_mentions WHERE mention_id = %s", (mention_id,))
            assert cur.fetchone()[0] == "unresolved"
    finally:
        conn.close()
