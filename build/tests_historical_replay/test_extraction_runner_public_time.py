"""
Historical Replay Phase 1A (specs/historical-replay-phase1a-implementation-spec-final.md),
Section 6's extraction_runner.py regression test: unchanged forward-path
behavior -- a relationship written today with a non-NULL
raw_documents.first_public_timestamp_precision now also carries that value
into entity_relationships.evidence_public_time_precision. Every database
this file touches comes exclusively from the `disposable_db_name` fixture
(conftest.py) -- never a hardcoded or passed-through name.
"""
import uuid
from datetime import datetime, timedelta, timezone

import bootstrap_database as bd
from llm_client import PROMPT_VERSION


class StubLLMClient:
    """outputs: dict[document_id, dict]. Matches the shape
    build/tests/test_extraction_runner.py's own StubLLMClient uses --
    never a real API call."""

    def __init__(self, outputs: dict):
        self.outputs = outputs

    def extract(self, document_id, raw_content, prompt_version):
        return self.outputs[document_id]


def llm_output(document_id, events):
    return {"document_id": document_id, "extraction_prompt_version": PROMPT_VERSION, "events": events}


def make_entity(conn, name):
    entity_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO entities (entity_id, legal_name) VALUES (%s, %s)", (entity_id, name))
    return entity_id


def make_catalyst_with_primary_document(conn, raw_content, issuer_entity_id,
                                         canonical_first_public_at, first_public_timestamp_precision):
    doc_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_documents (document_id, source_name, document_type, raw_content, content_hash, "
            "canonical_first_public_at, first_public_timestamp_precision) "
            "VALUES (%s, 'test', '8-K', %s, 'test-hash', %s, %s)",
            (doc_id, raw_content, canonical_first_public_at, first_public_timestamp_precision),
        )
        cur.execute(
            "INSERT INTO catalysts (originating_document_id, issuer_entity_id) VALUES (%s, %s) "
            "RETURNING catalyst_id",
            (doc_id, issuer_entity_id),
        )
        catalyst_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO catalyst_documents (catalyst_id, document_id, document_role) VALUES (%s, %s, 'primary')",
            (catalyst_id, doc_id),
        )
    return catalyst_id, doc_id


def test_forward_path_carries_precision_into_entity_relationships(disposable_db_name):
    """A relationship written through the real extract_document ->
    process_catalyst pipeline, from a source document whose
    first_public_timestamp_precision is non-NULL, must carry that same
    value into the written entity_relationships row -- unchanged, exact
    equality with the source (invariant 5), and confirms the propagation
    fix didn't break the already-working forward canonical-timestamp
    path."""
    bd.cmd_bootstrap(disposable_db_name, "forward")
    conn = bd.connect_to_target(disposable_db_name)
    try:
        from extraction_runner import extract_document, process_catalyst

        canonical = datetime(2026, 4, 1, 13, 30, tzinfo=timezone.utc)
        precision = timedelta(minutes=2)

        issuer_id = make_entity(conn, "Issuer Co")
        counterparty_id = make_entity(conn, "Counterparty Co")
        span = "Issuer Co supplies Counterparty Co."
        catalyst_id, doc_id = make_catalyst_with_primary_document(
            conn, span, issuer_id, canonical, precision,
        )

        event = {
            "event_category": "supply_agreement", "catalyst_description": "x",
            "entities": [
                {"entity_name": "Issuer Co", "role": "issuer", "evidence_span": span},
                {"entity_name": "Counterparty Co", "role": "supplier", "evidence_span": span},
            ],
            "relationships": [{
                "entity_a": "Issuer Co", "entity_b": "Counterparty Co",
                "relationship_type": "supplier", "relationship_evidence": "explicit_named",
                "source_authority": "company", "document_explicitly_states_transmission_history": False,
                "evidence_span": span, "raw_llm_relationship_score": 0.9,
            }],
            "surprise": None, "explicit_correction": False,
        }
        client = StubLLMClient({doc_id: llm_output(doc_id, [event])})
        extract_document(conn, client, doc_id, span, PROMPT_VERSION, "test-model", "test-version")
        result = process_catalyst(conn, catalyst_id, PROMPT_VERSION, "test-model", "test-version")

        assert result["relationships_written"] == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT evidence_publicly_available_at, evidence_public_time_precision FROM entity_relationships "
                "WHERE entity_id_a = %s AND entity_id_b = %s",
                (issuer_id, counterparty_id),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        evidence_publicly_available_at, evidence_public_time_precision = rows[0]
        assert evidence_publicly_available_at == canonical
        assert evidence_public_time_precision == precision
    finally:
        conn.close()
