"""
Extraction-Runner Design v2, §3's backfill rule: when a human resolves an
unresolved_entity_mentions row, any relationship it was part of is written
THEN, with system_observed_at set to the ACTUAL resolution timestamp --
never backdated to the original filing/extraction time.
"""
from datetime import datetime, timezone

import psycopg2.extras

from conftest import make_entity, make_raw_document, make_extraction_run

from entity_resolution import resolve_entity_name
from manual_resolve import create_entity_and_resolve, resolve_mention


def _log_a_relationship_mention(conn, issuer_id, counterparty_raw_name, document_id, extraction_run_id):
    """Simulates what process_catalyst would have done: the issuer resolves
    fine, the counterparty doesn't, so a relationship-bearing mention gets
    logged (not silently dropped) and the relationship itself is NOT written
    yet."""
    raw_output = {
        "document_id": document_id, "extraction_prompt_version": "1.1.0",
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
        cur.execute("UPDATE extraction_runs SET raw_llm_output = %s WHERE extraction_run_id = %s",
                    (psycopg2.extras.Json(raw_output), extraction_run_id))
    resolve_entity_name(conn, counterparty_raw_name, document_id, extraction_run_id)


def test_resolving_a_mention_backfills_relationship_with_resolution_time_not_backdated(conn):
    issuer_id = make_entity(conn, "Issuer Co")
    document_id = make_raw_document(
        conn, raw_content="Issuer Co supplies Formerly Unknown Corp. Filed long ago.",
    )
    extraction_run_id = make_extraction_run(conn, document_id=document_id)
    _log_a_relationship_mention(conn, issuer_id, "Formerly Unknown Corp", document_id, extraction_run_id)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entity_relationships")
        assert cur.fetchone()[0] == 0  # not written yet -- counterparty unresolved

        cur.execute("SELECT mention_id FROM unresolved_entity_mentions WHERE document_id = %s", (document_id,))
        mention_id = cur.fetchone()[0]

    before_resolution = datetime.now(timezone.utc)
    new_entity_id = create_entity_and_resolve(conn, str(mention_id), "Formerly Unknown Corporation")
    after_resolution = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id_a, entity_id_b, system_observed_at, evidence_publicly_available_at "
            "FROM entity_relationships",
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    entity_id_a, entity_id_b, system_observed_at, evidence_publicly_available_at = rows[0]
    assert str(entity_id_a) == issuer_id and str(entity_id_b) == new_entity_id

    # The core backfill assertion: system_observed_at is the REAL resolution
    # moment, not backdated to when the document was originally logged.
    assert before_resolution <= system_observed_at <= after_resolution

    with conn.cursor() as cur:
        cur.execute("SELECT status, resolved_entity_id FROM unresolved_entity_mentions WHERE mention_id = %s",
                    (mention_id,))
        status, resolved_entity_id = cur.fetchone()
    assert status == "resolved"
    assert str(resolved_entity_id) == new_entity_id


def test_new_entity_from_manual_resolution_is_not_added_to_watchlist(conn):
    issuer_id = make_entity(conn, "Issuer Co")
    document_id = make_raw_document(conn, raw_content="Issuer Co supplies Some Counterparty Inc.")
    extraction_run_id = make_extraction_run(conn, document_id=document_id)
    _log_a_relationship_mention(conn, issuer_id, "Some Counterparty Inc", document_id, extraction_run_id)

    with conn.cursor() as cur:
        cur.execute("SELECT mention_id FROM unresolved_entity_mentions WHERE document_id = %s", (document_id,))
        mention_id = cur.fetchone()[0]

    new_entity_id = create_entity_and_resolve(conn, str(mention_id), "Some Counterparty Incorporated")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM watchlist_membership WHERE entity_id = %s", (new_entity_id,))
        assert cur.fetchone()[0] == 0
