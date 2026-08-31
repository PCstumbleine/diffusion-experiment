"""
Extraction-Runner Design v2, §6 testing strategy: catalyst-level
canonicalization of a duplicate primary+exhibit event, nullable-surprise
insertion, evidence-span verification, the decision_at timing rule,
full-pipeline idempotency, and atomicity under a forced mid-batch failure.

Uses a stubbed LLM client (StubLLMClient below) -- never a real API call,
same convention as edgar_ingest_worker.py's tests (make_client / MagicMock).
"""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_entity, make_catalyst_with_documents

from extraction_runner import (
    extract_document, process_catalyst, run_extraction_pass,
    validate_extraction_output, classify_relationship_eligibility,
    generate_candidates_for_event_version, ExtractionValidationError,
)

PROMPT_VERSION = "1.1.0"
MODEL_ID = "test-model"
MODEL_VERSION = "test-version"


class StubLLMClient:
    """outputs: dict[document_id, dict | Exception]."""

    def __init__(self, outputs: dict):
        self.outputs = outputs
        self.calls: list[str] = []

    def extract(self, document_id, raw_content, prompt_version):
        self.calls.append(document_id)
        result = self.outputs[document_id]
        if isinstance(result, Exception):
            raise result
        return result


def llm_output(events):
    return {"document_id": "irrelevant", "extraction_prompt_version": PROMPT_VERSION, "events": events}


def guidance_event(entity_name, evidence_span, observed=10_000_000_000, reference=9_000_000_000,
                    explicit_correction=False):
    return {
        "event_category": "guidance_revision",
        "catalyst_description": "Guidance update",
        "entities": [{"entity_name": entity_name, "role": "issuer", "evidence_span": evidence_span}],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_guidance",
            "observed_value": observed,
            "reference_value": reference,
            "reference_source": "company prior guidance",
            "reference_timestamp": None,
            "unit": "USD",
            "period": "FY2026",
            "evidence_span": evidence_span,
        },
        "source_published_at": None,
        "explicit_correction": explicit_correction,
    }


def process_full(conn, catalyst_id, outputs):
    """extract_document for every document in outputs, then process_catalyst."""
    client = StubLLMClient(outputs)
    for document_id, raw_content in _docs_with_content(conn, outputs.keys()):
        extract_document(conn, client, document_id, raw_content, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    return process_catalyst(conn, catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)


def _docs_with_content(conn, document_ids):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT document_id, raw_content FROM raw_documents WHERE document_id::text = ANY(%s)",
            (list(document_ids),),
        )
        return [(str(doc_id), content) for doc_id, content in cur.fetchall()]


# ---------------------------------------------------------------------------
# Duplicate-event canonicalization
# ---------------------------------------------------------------------------

def test_duplicate_event_across_primary_and_exhibit_canonicalizes_to_one_event(conn):
    issuer_id = make_entity(conn, "NVIDIA Corporation")
    primary_span = "NVIDIA today raised full-year revenue guidance to $10 billion from $9 billion."
    exhibit_span = "In today's release, NVIDIA Corporation raised its guidance to $10 billion, up from $9 billion."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn,
        [
            ("primary", "primary", f"8-K cover page. {primary_span} See Exhibit 99.1."),
            ("exhibit", "exhibit", f"Exhibit 99.1 press release. {exhibit_span}"),
        ],
        issuer_entity_id=issuer_id,
    )

    outputs = {
        doc_ids["primary"]: llm_output([guidance_event("NVIDIA", primary_span)]),
        doc_ids["exhibit"]: llm_output([guidance_event("NVIDIA Corporation", exhibit_span)]),
    }
    result = process_full(conn, catalyst_id, outputs)

    assert result["canonical_events_created"] == 1

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM canonical_events WHERE catalyst_id = %s", (catalyst_id,))
        assert cur.fetchone()[0] == 1

        cur.execute(
            "SELECT edl.document_id, edl.relationship_type FROM event_document_links edl "
            "JOIN canonical_events ce ON ce.canonical_event_id = edl.canonical_event_id "
            "WHERE ce.catalyst_id = %s",
            (catalyst_id,),
        )
        links = cur.fetchall()
    assert len(links) == 2  # not two separate duplicate events
    roles = {r for _doc, r in links}
    assert roles == {"primary_source", "corroborating"}


def test_distinct_events_in_the_same_catalyst_stay_separate(conn):
    """Two GENUINELY different guidance figures (different observed_value)
    must NOT be merged into one canonical event just because they share a
    catalyst and category."""
    issuer_id = make_entity(conn, "NVIDIA Corporation")
    span_a = "Revenue guidance raised to $10 billion."
    span_b = "Gross margin guidance raised to 75%."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", f"{span_a} {span_b}")], issuer_entity_id=issuer_id,
    )
    outputs = {
        doc_ids["primary"]: llm_output([
            guidance_event("NVIDIA", span_a, observed=10_000_000_000, reference=9_000_000_000),
            {**guidance_event("NVIDIA", span_b), "surprise": {
                "surprise_type": "gross_margin_guidance", "observed_value": 75, "reference_value": 70,
                "reference_source": "company prior guidance", "reference_timestamp": None,
                "unit": "pct", "period": "FY2026", "evidence_span": span_b,
            }},
        ]),
    }
    result = process_full(conn, catalyst_id, outputs)
    assert result["canonical_events_created"] == 2


# ---------------------------------------------------------------------------
# Nullable surprise
# ---------------------------------------------------------------------------

def test_nullable_surprise_extraction_inserts_successfully(conn):
    issuer_id = make_entity(conn, "Acme Corporation")
    span = "Acme Corporation announced it will divest its logistics division."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", span)], issuer_entity_id=issuer_id,
    )
    event = {
        "event_category": "acquisition_or_divestiture",
        "catalyst_description": "Divestiture announcement",
        "entities": [{"entity_name": "Acme Corporation", "role": "issuer", "evidence_span": span}],
        "relationships": [],
        "surprise": None,
        "source_published_at": None,
        "explicit_correction": False,
    }
    outputs = {doc_ids["primary"]: llm_output([event])}
    result = process_full(conn, catalyst_id, outputs)

    assert result["canonical_events_created"] == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT surprise_type, observed_value FROM extracted_events ee "
            "JOIN event_versions ev ON ev.event_version_id = ee.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s",
            (catalyst_id,),
        )
        row = cur.fetchone()
    assert row == (None, None)


# ---------------------------------------------------------------------------
# Evidence-span verification
# ---------------------------------------------------------------------------

def test_validate_extraction_output_drops_claim_with_bad_evidence_span():
    raw_content = "The real text of the document."
    output = {
        "document_id": "d1", "extraction_prompt_version": PROMPT_VERSION,
        "events": [{
            "event_category": "other_material_event",
            "catalyst_description": "x",
            "entities": [
                {"entity_name": "Real Co", "role": "issuer", "evidence_span": "The real text"},
                {"entity_name": "Fabricated Co", "role": "subject", "evidence_span": "text that does not appear anywhere"},
            ],
            "relationships": [],
            "surprise": None,
            "explicit_correction": False,
        }],
    }
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION)
    assert len(cleaned["events"]) == 1
    names = {e["entity_name"] for e in cleaned["events"][0]["entities"]}
    assert names == {"Real Co"}  # the fabricated span was dropped, not paraphrased/kept
    assert any("Fabricated Co" in msg for msg in drop_log)


def test_validate_extraction_output_drops_whole_event_when_all_entities_have_bad_spans():
    raw_content = "The real text."
    output = {
        "document_id": "d1", "extraction_prompt_version": PROMPT_VERSION,
        "events": [{
            "event_category": "other_material_event", "catalyst_description": "x",
            "entities": [{"entity_name": "Ghost Co", "role": "issuer", "evidence_span": "not in document"}],
            "relationships": [], "surprise": None, "explicit_correction": False,
        }],
    }
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION)
    assert cleaned["events"] == []


def test_validate_extraction_output_rejects_wrong_prompt_version():
    output = {"document_id": "d1", "extraction_prompt_version": "9.9.9", "events": []}
    with pytest.raises(ExtractionValidationError):
        validate_extraction_output(output, "content", PROMPT_VERSION)


def test_validate_extraction_output_drops_relationship_with_unmapped_type():
    raw_content = "A supplies B. See details here."
    output = {
        "document_id": "d1", "extraction_prompt_version": PROMPT_VERSION,
        "events": [{
            "event_category": "supply_agreement", "catalyst_description": "x",
            "entities": [{"entity_name": "A Co", "role": "issuer", "evidence_span": "A supplies B"}],
            "relationships": [{
                "entity_a": "A Co", "entity_b": "B Co", "relationship_type": "a completely made up relation",
                "relationship_evidence": "explicit_named", "source_authority": "company",
                "document_explicitly_states_transmission_history": False,
                "evidence_span": "A supplies B",
            }],
            "surprise": None, "explicit_correction": False,
        }],
    }
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION)
    assert cleaned["events"][0]["relationships"] == []
    assert any("does not map to the closed vocabulary" in msg for msg in drop_log)


# ---------------------------------------------------------------------------
# decision_at timing rule
# ---------------------------------------------------------------------------

def test_classify_relationship_eligibility_excludes_late_system_observation():
    decision_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
    rel = {
        "relationship_evidence": "explicit_named",
        "evidence_publicly_available_at": datetime(2024, 1, 1, tzinfo=timezone.utc),  # well before
        "system_observed_at": datetime(2025, 6, 15, tzinfo=timezone.utc),  # AFTER decision_at
        "relationship_valid_from": None, "relationship_valid_to": None, "record_superseded_at": None,
    }
    ok, reason = classify_relationship_eligibility(rel, decision_at)
    assert ok is False
    assert reason == "system_observed_at_after_decision_at"


def test_classify_relationship_eligibility_passes_when_both_clocks_precede_decision(conn):
    decision_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
    rel = {
        "relationship_evidence": "explicit_named",
        "evidence_publicly_available_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "system_observed_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
        "relationship_valid_from": None, "relationship_valid_to": None, "record_superseded_at": None,
    }
    ok, reason = classify_relationship_eligibility(rel, decision_at)
    assert ok is True and reason is None


def test_generate_candidates_marks_ineligible_for_late_observed_relationship(conn):
    from conftest import make_raw_document, make_extraction_run

    issuer_id = make_entity(conn, "Issuer Co")
    counterparty_id = make_entity(conn, "Counterparty Co")
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)

    decision_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO entity_relationships
                (entity_id_a, entity_id_b, relationship_type, source_authority,
                 relationship_evidence, shock_transmission_evidence,
                 evidence_publicly_available_at, system_observed_at, source_document_id, extraction_run_id)
            VALUES (%s, %s, 'supplier', 'company', 'explicit_named', 'new_or_unobserved', %s, %s, %s, %s)
            """,
            (issuer_id, counterparty_id, datetime(2024, 1, 1, tzinfo=timezone.utc),
             datetime(2025, 6, 15, tzinfo=timezone.utc), document_id, run_id),
        )
        cur.execute(
            "INSERT INTO catalysts (originating_document_id, issuer_entity_id) VALUES (%s, %s) RETURNING catalyst_id",
            (document_id, issuer_id),
        )
        catalyst_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO canonical_events (catalyst_id, event_category) VALUES (%s, 'guidance_revision') "
            "RETURNING canonical_event_id",
            (catalyst_id,),
        )
        canonical_event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO event_versions (canonical_event_id, version_number, decision_at) VALUES (%s, 1, %s) "
            "RETURNING event_version_id",
            (canonical_event_id, decision_at),
        )
        event_version_id = cur.fetchone()[0]

    counts = generate_candidates_for_event_version(conn, event_version_id, issuer_id, decision_at)
    assert counts == {"eligible": 0, "ineligible": 1}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT eligibility_status, eligibility_reason, policy_version FROM candidate_signals "
            "WHERE event_version_id = %s AND entity_id = %s",
            (event_version_id, counterparty_id),
        )
        row = cur.fetchone()
    assert row == ("ineligible", "system_observed_at_after_decision_at", "candidate_eligibility_v1")


# ---------------------------------------------------------------------------
# Full-pipeline idempotency
# ---------------------------------------------------------------------------

def test_full_pipeline_idempotency_on_rerun(conn):
    issuer_id = make_entity(conn, "NVIDIA Corporation")
    span = "NVIDIA raised guidance to $10 billion from $9 billion."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", span)], issuer_entity_id=issuer_id,
    )
    outputs = {doc_ids["primary"]: llm_output([guidance_event("NVIDIA", span)])}

    process_full(conn, catalyst_id, outputs)

    def counts():
        tables = ["extraction_runs", "extracted_events", "canonical_events", "event_versions",
                  "event_entities", "event_document_links", "entity_relationships", "candidate_signals"]
        with conn.cursor() as cur:
            return {t: cur.execute(f"SELECT count(*) FROM {t}") or cur.fetchone()[0] for t in tables}

    first = counts()

    # Re-run the whole pipeline again over the SAME documents/catalyst/model
    # config -- extract_document should skip re-calling the LLM (already
    # 'success'), and process_catalyst should no-op (already canonicalized).
    client = StubLLMClient(outputs)
    for document_id, raw_content in _docs_with_content(conn, outputs.keys()):
        extract_document(conn, client, document_id, raw_content, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    result = process_catalyst(conn, catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)

    assert result == {"skipped": "already_done"}
    assert client.calls == []  # the LLM was never called again
    second = counts()
    assert first == second


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

def test_forced_failure_partway_through_catalyst_batch_leaves_no_partial_rows(conn, monkeypatch):
    issuer_id = make_entity(conn, "NVIDIA Corporation")
    span_a = "Revenue guidance raised to $10 billion."
    span_b = "Order win worth $500 million announced."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", f"{span_a} {span_b}")], issuer_entity_id=issuer_id,
    )
    order_event = {
        "event_category": "order_win", "catalyst_description": "Order win",
        "entities": [{"entity_name": "NVIDIA", "role": "issuer", "evidence_span": span_b}],
        "relationships": [], "surprise": None, "explicit_correction": False,
    }
    outputs = {doc_ids["primary"]: llm_output([guidance_event("NVIDIA", span_a), order_event])}

    client = StubLLMClient(outputs)
    for document_id, raw_content in _docs_with_content(conn, outputs.keys()):
        extract_document(conn, client, document_id, raw_content, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)

    import extraction_runner
    call_count = {"n": 0}
    real_fn = extraction_runner.generate_candidates_for_event_version

    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated failure partway through the catalyst batch")
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(extraction_runner, "generate_candidates_for_event_version", flaky)

    with pytest.raises(RuntimeError):
        process_catalyst(conn, catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM canonical_events WHERE catalyst_id = %s", (catalyst_id,))
        assert cur.fetchone()[0] == 0  # NEITHER event's rows survived, not just the second one
        cur.execute("SELECT canonicalization_completed_at FROM catalysts WHERE catalyst_id = %s", (catalyst_id,))
        assert cur.fetchone()[0] is None

    # And a clean retry afterward succeeds completely.
    result = process_catalyst(conn, catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    assert result["canonical_events_created"] == 2
