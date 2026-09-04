"""
Extraction-Runner Design v2, §6 testing strategy: catalyst-level
canonicalization of a duplicate primary+exhibit event, nullable-surprise
insertion, evidence-span verification, the decision_at timing rule,
full-pipeline idempotency, and atomicity under a forced mid-batch failure --
plus the coverage a pre-dry-run code review found missing: retry after a
failed extraction, a genuine concurrent-claim race, a document_id mismatch,
and an earlier event in a catalyst seeing a relationship a later event in
the SAME catalyst discovers (the decision_at-ordering fix).

Uses a stubbed LLM client (StubLLMClient below) -- never a real API call,
same convention as edgar_ingest_worker.py's tests (make_client / MagicMock).
"""
from datetime import datetime, timezone

import psycopg2
import pytest

from conftest import DB_DSN, make_entity, make_catalyst_with_documents, make_raw_document

from extraction_runner import (
    extract_document, claim_extraction_run, process_catalyst, run_extraction_pass,
    validate_extraction_output, classify_relationship_eligibility,
    generate_candidates_for_event_version, ExtractionValidationError,
    _verify_evidence_span, _repair_escaped_punctuation,
)
from llm_client import PROMPT_VERSION  # kept in sync with extraction_prompt_v1.md's schema automatically

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


def llm_output(document_id, events):
    # A prior version hardcoded "irrelevant" here for every call -- which is
    # exactly why validate_extraction_output's document_id check (added by
    # a pre-dry-run code review) wasn't caught by any existing test.
    return {"document_id": document_id, "extraction_prompt_version": PROMPT_VERSION, "events": events}


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
        doc_ids["primary"]: llm_output(doc_ids["primary"], [guidance_event("NVIDIA", primary_span)]),
        doc_ids["exhibit"]: llm_output(doc_ids["exhibit"], [guidance_event("NVIDIA Corporation", exhibit_span)]),
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
        doc_ids["primary"]: llm_output(doc_ids["primary"], [
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


def test_cover_page_with_empty_events_contributes_nothing_and_exhibit_event_stands_alone(conn):
    """Fix (extraction_prompt_v1.md v1.2.0, code review post-Dry-Run-001):
    a cover page that only points to an exhibit should extract to an empty
    events array, not a contentless stub -- Dry Run 001 found that a
    null-surprise stub event never canonicalizes with its own exhibit's
    real event (different fingerprints), producing two canonical events
    per catalyst as the normal case for an ordinary earnings 8-K. This
    confirms the pipeline already does the right thing once the cover
    page correctly contributes zero events: the exhibit's event becomes
    the catalyst's sole canonical event, and only the exhibit is linked."""
    issuer_id = make_entity(conn, "Acme Corporation")
    cover_span = "Acme Corporation issued a press release; see Exhibit 99.1."
    exhibit_span = "Acme Corporation reported revenue of $5.0 billion."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn,
        [("primary", "primary", cover_span), ("exhibit", "exhibit", exhibit_span)],
        issuer_entity_id=issuer_id,
    )
    real_event = {
        "event_category": "earnings_surprise", "catalyst_description": "Earnings",
        "entities": [{"entity_name": "Acme Corporation", "role": "issuer", "evidence_span": exhibit_span}],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_actual", "observed_value": 5.0, "reference_value": None,
            "reference_source": None, "reference_timestamp": None, "unit": "USD_billions",
            "period": "Q2", "evidence_span": exhibit_span,
        },
        "explicit_correction": False,
    }
    outputs = {
        doc_ids["primary"]: llm_output(doc_ids["primary"], []),  # correctly empty, per the new prompt rule
        doc_ids["exhibit"]: llm_output(doc_ids["exhibit"], [real_event]),
    }
    result = process_full(conn, catalyst_id, outputs)

    assert result["canonical_events_created"] == 1  # not 2 -- no contentless stub
    with conn.cursor() as cur:
        cur.execute(
            "SELECT edl.document_id FROM event_document_links edl "
            "JOIN canonical_events ce ON ce.canonical_event_id = edl.canonical_event_id "
            "WHERE ce.catalyst_id = %s",
            (catalyst_id,),
        )
        linked_docs = {str(r[0]) for r in cur.fetchall()}
    assert linked_docs == {doc_ids["exhibit"]}  # the cover page contributed nothing


# ---------------------------------------------------------------------------
# Range-valued guidance (extraction_prompt_v1.md v1.2.0)
# ---------------------------------------------------------------------------

def test_range_valued_guidance_inserts_and_is_stored(conn):
    issuer_id = make_entity(conn, "Acme Corporation")
    span = "Acme raised full-year revenue guidance to $85.0 billion to $87.0 billion."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", span)], issuer_entity_id=issuer_id,
    )
    event = {
        "event_category": "guidance_revision", "catalyst_description": "Guidance raise",
        "entities": [{"entity_name": "Acme Corporation", "role": "issuer", "evidence_span": span}],
        "relationships": [],
        "surprise": {
            "surprise_type": "revenue_guidance",
            "observed_value": None, "reference_value": None,
            "observed_value_low": 85.0, "observed_value_high": 87.0,
            "reference_value_low": None, "reference_value_high": None,
            "reference_source": None, "reference_timestamp": None,
            "unit": "USD_billions", "period": "FY2026",
            "evidence_span": span,
        },
        "explicit_correction": False,
    }
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [event])}
    result = process_full(conn, catalyst_id, outputs)
    assert result["canonical_events_created"] == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT observed_value, observed_value_low, observed_value_high FROM extracted_events ee "
            "JOIN event_versions ev ON ev.event_version_id = ee.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s",
            (catalyst_id,),
        )
        row = cur.fetchone()
    assert row == (None, 85.0, 87.0)


def test_different_guidance_ranges_stay_separate_events(conn):
    """Range fields are part of the canonicalization fingerprint -- two
    DIFFERENT stated ranges must not collide into one canonical event just
    because observed_value/reference_value are both None for a range
    claim."""
    issuer_id = make_entity(conn, "Acme Corporation")
    span_a = "First guidance range statement: $85.0 billion to $87.0 billion."
    span_b = "Second guidance range statement: $90.0 billion to $92.0 billion."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", f"{span_a} {span_b}")], issuer_entity_id=issuer_id,
    )

    def range_event(span, low, high):
        return {
            "event_category": "guidance_revision", "catalyst_description": "x",
            "entities": [{"entity_name": "Acme Corporation", "role": "issuer", "evidence_span": span}],
            "relationships": [],
            "surprise": {
                "surprise_type": "revenue_guidance", "observed_value": None, "reference_value": None,
                "observed_value_low": low, "observed_value_high": high,
                "reference_value_low": None, "reference_value_high": None,
                "reference_source": None, "reference_timestamp": None,
                "unit": "USD_billions", "period": "FY2026", "evidence_span": span,
            },
            "explicit_correction": False,
        }

    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [
        range_event(span_a, 85.0, 87.0), range_event(span_b, 90.0, 92.0),
    ])}
    result = process_full(conn, catalyst_id, outputs)
    assert result["canonical_events_created"] == 2


def test_extracted_events_rejects_observed_range_with_low_greater_than_high(conn):
    from conftest import make_extraction_run
    import psycopg2.extras

    issuer_id = make_entity(conn, "Acme Corporation")
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)
    with conn.cursor() as cur:
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
            "INSERT INTO event_versions (canonical_event_id, version_number) VALUES (%s, 1) "
            "RETURNING event_version_id",
            (canonical_event_id,),
        )
        event_version_id = cur.fetchone()[0]

    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO extracted_events (event_version_id, extraction_run_id, extraction_prompt_version, "
                "raw_llm_output, observed_value_low, observed_value_high) VALUES (%s, %s, %s, %s, %s, %s)",
                (event_version_id, run_id, PROMPT_VERSION, psycopg2.extras.Json({}), 100.0, 50.0),
            )
    conn.rollback()


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
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [event])}
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
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")
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
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")
    assert cleaned["events"] == []


def test_validate_extraction_output_rejects_wrong_prompt_version():
    output = {"document_id": "d1", "extraction_prompt_version": "9.9.9", "events": []}
    with pytest.raises(ExtractionValidationError):
        validate_extraction_output(output, "content", PROMPT_VERSION, "d1")


def test_validate_extraction_output_rejects_document_id_mismatch():
    """Code-review fix: only presence of document_id used to be checked,
    never that it actually matches the document sent -- the test helper
    that hid this gap (llm_output(), which used to hardcode "irrelevant")
    is fixed above."""
    output = {"document_id": "wrong-document-id", "extraction_prompt_version": PROMPT_VERSION, "events": []}
    with pytest.raises(ExtractionValidationError):
        validate_extraction_output(output, "content", PROMPT_VERSION, "the-real-document-id")


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
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")
    assert cleaned["events"][0]["relationships"] == []
    assert any("does not map to the closed vocabulary" in msg for msg in drop_log)


# ---------------------------------------------------------------------------
# Evidenced relationship_type synonyms (hardening item D, Recall Check 001
# replay): three real relationship_type strings the independent blind-read
# extraction actually produced ("acquired", "agreement_to_acquire",
# "collaboration") weren't in RELATIONSHIP_TYPE_SYNONYMS, so all three
# relationships were silently dropped at Phase 1 validation before ever
# reaching entity resolution. Each test below runs the real
# validate_extraction_output() -- proving the mapped relationship_type
# survives validation with its canonical value, not just that the dict has
# the key.
# ---------------------------------------------------------------------------

def _one_relationship_output(relationship_type):
    raw_content = "A acquired B. See details here."
    return {
        "document_id": "d1", "extraction_prompt_version": PROMPT_VERSION,
        "events": [{
            "event_category": "acquisition_or_divestiture", "catalyst_description": "x",
            "entities": [{"entity_name": "A Co", "role": "issuer", "evidence_span": "A acquired B"}],
            "relationships": [{
                "entity_a": "A Co", "entity_b": "B Co", "relationship_type": relationship_type,
                "relationship_evidence": "explicit_named", "source_authority": "company",
                "document_explicitly_states_transmission_history": False,
                "evidence_span": "A acquired B",
            }],
            "surprise": None, "explicit_correction": False,
        }],
    }, raw_content


@pytest.mark.parametrize("given_type,expected_mapped", [
    ("acquired", "acquirer_target"),
    ("agreement_to_acquire", "acquirer_target"),
    ("collaboration", "partner"),
])
def test_evidenced_relationship_type_synonym_survives_validation(given_type, expected_mapped):
    output, raw_content = _one_relationship_output(given_type)
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")

    rels = cleaned["events"][0]["relationships"]
    assert len(rels) == 1  # not dropped
    assert not any("does not map to the closed vocabulary" in msg for msg in drop_log)
    assert rels[0]["relationship_type"] == expected_mapped
    assert rels[0]["entity_a"] == "A Co"  # direction unchanged
    assert rels[0]["entity_b"] == "B Co"


def test_unmapped_relationship_type_is_still_dropped_after_the_new_synonyms():
    """Negative control: the closed-vocabulary gate itself wasn't weakened
    by adding the three evidenced synonyms above."""
    output, raw_content = _one_relationship_output("some totally unknown relationship")
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")

    assert cleaned["events"][0]["relationships"] == []
    assert any("does not map to the closed vocabulary" in msg for msg in drop_log)


def test_validate_extraction_output_drops_surprise_with_reference_value_but_no_source():
    """Provenance invariant (code review, post-Dry-Run-001): a reference
    figure with no stated source is an unexplained benchmark, not a fact."""
    raw_content = "Revenue guidance raised to $10 billion from $9 billion."
    output = {
        "document_id": "d1", "extraction_prompt_version": PROMPT_VERSION,
        "events": [{
            "event_category": "guidance_revision", "catalyst_description": "x",
            "entities": [{"entity_name": "A Co", "role": "issuer", "evidence_span": "Revenue guidance raised to $10 billion"}],
            "relationships": [],
            "surprise": {
                "surprise_type": "revenue_guidance", "observed_value": 10, "reference_value": 9,
                "reference_source": None,  # the violation
                "reference_timestamp": None, "unit": "USD_billions", "period": "FY2026",
                "evidence_span": "Revenue guidance raised to $10 billion from $9 billion",
            },
            "explicit_correction": False,
        }],
    }
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")
    assert len(cleaned["events"]) == 1  # the event survives
    assert cleaned["events"][0]["surprise"] is None  # only the surprise claim is dropped
    assert any("reference_source" in msg for msg in drop_log)


def test_validate_extraction_output_drops_surprise_with_reference_range_but_no_source():
    """Same rule, exercised via the new range fields (v1.2.0) rather than
    the point reference_value."""
    raw_content = "Guidance raised to $10 billion to $11 billion from a prior range."
    output = {
        "document_id": "d1", "extraction_prompt_version": PROMPT_VERSION,
        "events": [{
            "event_category": "guidance_revision", "catalyst_description": "x",
            "entities": [{"entity_name": "A Co", "role": "issuer", "evidence_span": "Guidance raised to $10 billion to $11 billion"}],
            "relationships": [],
            "surprise": {
                "surprise_type": "revenue_guidance", "observed_value": None, "reference_value": None,
                "observed_value_low": 10, "observed_value_high": 11,
                "reference_value_low": 8, "reference_value_high": 9,
                "reference_source": None,  # the violation -- a reference range with no source
                "reference_timestamp": None, "unit": "USD_billions", "period": "FY2026",
                "evidence_span": "Guidance raised to $10 billion to $11 billion from a prior range",
            },
            "explicit_correction": False,
        }],
    }
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")
    assert cleaned["events"][0]["surprise"] is None
    assert any("reference_source" in msg for msg in drop_log)


def test_validate_extraction_output_allows_null_reference_source_when_no_reference_value():
    """The other half of the invariant: reference_source may legitimately
    be null when there's genuinely no reference value to attribute (e.g.
    a first-time guidance issuance) -- this must NOT be treated as a
    violation."""
    raw_content = "NVIDIA guided next quarter's revenue to $108.0 billion."
    output = {
        "document_id": "d1", "extraction_prompt_version": PROMPT_VERSION,
        "events": [{
            "event_category": "guidance_revision", "catalyst_description": "x",
            "entities": [{"entity_name": "NVIDIA", "role": "issuer", "evidence_span": "NVIDIA guided next quarter's revenue to $108.0 billion"}],
            "relationships": [],
            "surprise": {
                "surprise_type": "revenue_guidance", "observed_value": 108.0, "reference_value": None,
                "reference_source": None,  # fine -- no reference_value to attribute
                "reference_timestamp": None, "unit": "USD_billions", "period": "Q3",
                "evidence_span": "NVIDIA guided next quarter's revenue to $108.0 billion",
            },
            "explicit_correction": False,
        }],
    }
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")
    assert cleaned["events"][0]["surprise"] is not None
    assert cleaned["events"][0]["surprise"]["observed_value"] == 108.0


# ---------------------------------------------------------------------------
# Evidence-span escape-repair (code review, post-Recall-Check-001): a span
# with spurious backslash-escaping in front of < > : / (seen specifically
# for spans pulled from nested HTML <table> markup) is repaired and
# accepted, rather than the whole claim being dropped over a formatting
# technicality -- but ONLY this one narrow pattern, never generically.
# ---------------------------------------------------------------------------

def test_verify_evidence_span_exact_match_is_accepted_unchanged():
    raw_content = "Revenue was $10 billion this quarter."
    ok, verified, mode = _verify_evidence_span("Revenue was $10 billion", raw_content)
    assert ok is True
    assert verified == "Revenue was $10 billion"
    assert mode == "exact"


def test_verify_evidence_span_repairs_escaped_punctuation_when_it_appears_exactly_once():
    raw_content = "Guidance table: <td>$85.0 billion</td> more text."
    given_span = r"\<td>$85.0 billion\<\/td>"  # zero real backslashes in raw_content
    ok, verified, mode = _verify_evidence_span(given_span, raw_content)
    assert ok is True
    assert verified == "<td>$85.0 billion</td>"
    assert mode == "escaped_punctuation_repair"


def test_verify_evidence_span_rejects_disqualifying_backslash_without_partial_repair():
    """A backslash NOT immediately followed by one of < > : / aborts the
    repair entirely -- this must never "rescue" a span that's malformed
    for some other reason by fixing only the part that happens to match."""
    raw_content = "Some markup <td>value</td> here."
    given_span = r"\foo<td>value</td>"  # backslash followed by 'f' -- disqualifying
    assert _repair_escaped_punctuation(given_span) is None
    ok, verified, mode = _verify_evidence_span(given_span, raw_content)
    assert ok is False
    assert verified is None and mode is None


def test_verify_evidence_span_rejects_repaired_candidate_absent_from_document():
    raw_content = "Nothing resembling the guidance table is in this document at all."
    given_span = r"\<td>$85.0 billion\<\/td>"
    ok, verified, mode = _verify_evidence_span(given_span, raw_content)
    assert ok is False


def test_verify_evidence_span_rejects_repaired_candidate_that_appears_more_than_once():
    """Ambiguous -- ok even though the repair "works" syntactically, per
    the exactly-once requirement."""
    raw_content = "<td>X</td> appears once here and <td>X</td> appears again here."
    given_span = r"\<td>X\<\/td>"
    ok, verified, mode = _verify_evidence_span(given_span, raw_content)
    assert ok is False


def test_validate_extraction_output_accepts_and_records_a_span_repair():
    raw_content = "Guidance table: <td>$85.0 billion</td> more text about revenue."
    given_span = r"\<td>$85.0 billion\<\/td>"
    output = {
        "document_id": "d1", "extraction_prompt_version": PROMPT_VERSION,
        "events": [{
            "event_category": "guidance_revision", "catalyst_description": "x",
            "entities": [{"entity_name": "A Co", "role": "issuer", "evidence_span": "Guidance table"}],
            "relationships": [],
            "surprise": {
                "surprise_type": "revenue_guidance", "observed_value": None, "reference_value": None,
                "reference_source": None, "reference_timestamp": None,
                "unit": "USD_billions", "period": "FY2026",
                "evidence_span": given_span,
            },
            "explicit_correction": False,
        }],
    }
    cleaned, drop_log = validate_extraction_output(output, raw_content, PROMPT_VERSION, "d1")
    assert len(cleaned["events"]) == 1
    surprise = cleaned["events"][0]["surprise"]
    assert surprise is not None  # NOT dropped -- repaired and kept
    assert surprise["evidence_span"] == "<td>$85.0 billion</td>"  # the repaired string, not the original
    assert not any("surprise" in msg for msg in drop_log)  # never logged as a drop -- it wasn't one

    span_repairs = cleaned["span_repairs"]
    assert len(span_repairs) == 1
    assert span_repairs[0] == {
        "event_index": 0, "claim_type": "surprise", "claim_index": None,
        "original_span": given_span, "verified_span": "<td>$85.0 billion</td>",
        "span_match_mode": "escaped_punctuation_repair",
    }


def test_entity_level_span_repair_feeds_event_document_links_with_repaired_string(conn):
    """The correctness detail the fix explicitly calls out: a repair on an
    ENTITY's evidence_span (not just surprise/relationship) must be
    reflected in the cleaned event BEFORE _do_process_catalyst computes
    event_document_links.evidence_span_start/_end via raw_content.find(),
    or that lookup silently fails to find the (unrepaired) original."""
    issuer_id = make_entity(conn, "Acme Corporation")
    raw_content = "Cover text. <td>Acme Corporation</td> reported results."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", raw_content)], issuer_entity_id=issuer_id,
    )
    given_span = r"\<td>Acme Corporation\<\/td>"
    event = {
        "event_category": "other_material_event", "catalyst_description": "x",
        "entities": [{"entity_name": "Acme Corporation", "role": "issuer", "evidence_span": given_span}],
        "relationships": [], "surprise": None, "explicit_correction": False,
    }
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [event])}
    result = process_full(conn, catalyst_id, outputs)
    assert result["canonical_events_created"] == 1

    repaired_span = "<td>Acme Corporation</td>"
    expected_start = raw_content.find(repaired_span)
    assert expected_start != -1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT evidence_span_start, evidence_span_end FROM event_document_links edl "
            "JOIN canonical_events ce ON ce.canonical_event_id = edl.canonical_event_id "
            "WHERE ce.catalyst_id = %s",
            (catalyst_id,),
        )
        start, end = cur.fetchone()
    assert start == expected_start
    assert end == expected_start + len(repaired_span)


# ---------------------------------------------------------------------------
# Retry-after-failure and concurrent-claim races (code review fixes #1/#2)
# ---------------------------------------------------------------------------

def test_retry_after_failure_reuses_the_same_row_and_increments_attempt_count(conn):
    """A prior version selected-then-inserted as two separate statements:
    a retry of a 'failed' row hit the identity UNIQUE constraint on the
    second INSERT -- a guaranteed uniqueness violation, not a rare edge
    case. Now the SAME row is claimed and updated."""
    raw_content = "Some real document text."
    document_id = make_raw_document(conn, raw_content=raw_content)

    failing_client = StubLLMClient({document_id: RuntimeError("simulated LLM failure")})
    run_id_1 = extract_document(conn, failing_client, document_id, raw_content, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    with conn.cursor() as cur:
        cur.execute("SELECT status, attempt_count FROM extraction_runs WHERE extraction_run_id = %s", (run_id_1,))
        status, attempts = cur.fetchone()
    assert status == "failed"
    assert attempts == 1

    working_output = llm_output(document_id, [guidance_event("Some Co", raw_content)])
    working_client = StubLLMClient({document_id: working_output})
    run_id_2 = extract_document(conn, working_client, document_id, raw_content, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)

    assert run_id_2 == run_id_1  # same row -- not a second INSERT under the same identity
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, error, raw_llm_output IS NOT NULL FROM extraction_runs "
            "WHERE extraction_run_id = %s",
            (run_id_1,),
        )
        status, attempts, error, has_output = cur.fetchone()
    assert status == "success"
    assert attempts == 2
    assert error is None
    assert has_output is True


def test_claim_extraction_run_two_concurrent_callers_only_one_wins(conn):
    """The exact race a prior check-then-insert design allowed: two
    callers could both see 'no successful row yet' and both proceed to
    pay for an LLM call. Uses TWO real, separate connections -- this is
    coverage the existing idempotency test never provided (it only reruns
    the same config sequentially, never actually races)."""
    document_id = make_raw_document(conn, raw_content="text")
    conn.commit()  # make it visible to the second connection

    conn2 = psycopg2.connect(DB_DSN)
    try:
        run_id_1, claimed_1 = claim_extraction_run(conn, document_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
        run_id_2, claimed_2 = claim_extraction_run(conn2, document_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    finally:
        conn2.close()

    assert run_id_1 == run_id_2  # both refer to the same row
    assert claimed_1 is True and claimed_2 is False  # exactly one caller may proceed to call the LLM


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


# ---------------------------------------------------------------------------
# Issuer-identity shortcut (code review, post-Dry-Run-001)
# ---------------------------------------------------------------------------

def test_issuer_role_resolves_to_the_known_issuer_even_when_the_name_would_not_match(conn):
    """Dry Run 001's real finding: an issuer can name itself in its own
    filing in a way that fails ordinary name-based resolution (Eli Lilly's
    own press release calls itself "Eli Lilly and Company", which didn't
    match its seeded alias). process_catalyst already knows the catalyst's
    issuer independently (from the filing's own CIK via watchlist_
    membership) -- an extracted entity with role="issuer" is resolved
    directly to that known entity_id, never through name-based resolution,
    so a normalization mismatch can never orphan the issuer's own events."""
    issuer_id = make_entity(conn, "Formal Legal Name Inc")
    span = "SomeCo Weirdname reported quarterly earnings of $5 million."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", span)], issuer_entity_id=issuer_id,
    )
    event = {
        "event_category": "earnings_surprise", "catalyst_description": "Earnings",
        # Deliberately a name with NO alias anywhere for "Formal Legal Name
        # Inc" -- would fail ordinary resolution if it were attempted.
        "entities": [{"entity_name": "SomeCo Weirdname", "role": "issuer", "evidence_span": span}],
        "relationships": [], "surprise": None, "explicit_correction": False,
    }
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [event])}
    result = process_full(conn, catalyst_id, outputs)
    assert result["canonical_events_created"] == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ee.entity_id, ee.role FROM event_entities ee "
            "JOIN event_versions ev ON ev.event_version_id = ee.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s",
            (catalyst_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert str(rows[0][0]) == issuer_id
    assert rows[0][1] == "issuer"

    # The shortcut bypasses name-based resolution entirely for role=issuer
    # -- confirms this isn't secretly working via some alias coincidence.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM unresolved_entity_mentions WHERE raw_name = %s",
            ("SomeCo Weirdname",),
        )
        assert cur.fetchone()[0] == 0


def test_lilly_curated_alias_resolves_role_buyer_entity_and_relationship_endpoint(conn):
    """Item 2 (code review, post-Recall-Check-001): the 2 of 34 Lilly
    events naming the issuer as bare "Lilly" use role="buyer", not
    "issuer" -- so the issuer-identity shortcut above correctly does NOT
    apply, and this exercises the curated alias instead. Relationships
    carry no role at all, so entity_a="Lilly" always goes through ordinary
    resolution regardless -- this is the case that was silently deferred
    before the fix."""
    from seed_entities import seed_all
    seed_all(conn)  # the real, checked-in 108-company CSV -- no network calls
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM entities WHERE cik = '0000059478'")
        lilly_id = str(cur.fetchone()[0])

    target_id = make_entity(conn, "Centessa Pharmaceuticals plc")
    span = "Lilly agreed to acquire Centessa Pharmaceuticals plc."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", span)], issuer_entity_id=lilly_id,
    )
    event = {
        "event_category": "acquisition_or_divestiture", "catalyst_description": "x",
        "entities": [
            {"entity_name": "Lilly", "role": "buyer", "evidence_span": span},
            {"entity_name": "Centessa Pharmaceuticals plc", "role": "target", "evidence_span": span},
        ],
        "relationships": [{
            "entity_a": "Lilly", "entity_b": "Centessa Pharmaceuticals plc",
            "relationship_type": "acquirer_target", "relationship_evidence": "explicit_named",
            "source_authority": "company", "document_explicitly_states_transmission_history": False,
            "evidence_span": span,
        }],
        "surprise": None, "explicit_correction": False,
    }
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [event])}
    result = process_full(conn, catalyst_id, outputs)

    assert result["relationships_deferred_unresolved"] == 0
    assert result["relationships_written"] == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ee.entity_id, ee.role FROM event_entities ee "
            "JOIN event_versions ev ON ev.event_version_id = ee.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s",
            (catalyst_id,),
        )
        rows = {(str(r[0]), r[1]) for r in cur.fetchall()}
    assert (lilly_id, "buyer") in rows
    assert (target_id, "target") in rows

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM entity_relationships WHERE entity_id_a = %s AND entity_id_b = %s",
            (lilly_id, target_id),
        )
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Relationship deferral observability (code review, post-Recall-Check-001)
# -- a relationship deferred on an unresolved endpoint is not silent data
# loss (the mention is already logged, and manual_resolve.py already
# backfills it later), but it wasn't previously countable or visible as
# its own event, distinct from a bare unresolved-mention log entry.
# ---------------------------------------------------------------------------

def test_relationship_deferred_on_unresolved_endpoint_produces_one_processing_issue(conn):
    issuer_id = make_entity(conn, "Issuer Co")
    span = "Issuer Co supplies Totally Unknown Counterparty Inc."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", span)], issuer_entity_id=issuer_id,
    )
    event = {
        "event_category": "supply_agreement", "catalyst_description": "x",
        "entities": [{"entity_name": "Issuer Co", "role": "issuer", "evidence_span": span}],
        "relationships": [{
            "entity_a": "Issuer Co", "entity_b": "Totally Unknown Counterparty Inc",
            "relationship_type": "supplier", "relationship_evidence": "explicit_named",
            "source_authority": "company", "document_explicitly_states_transmission_history": False,
            "evidence_span": span,
        }],
        "surprise": None, "explicit_correction": False,
    }
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [event])}
    result = process_full(conn, catalyst_id, outputs)

    assert result["relationships_deferred_unresolved"] == 1
    assert result["relationships_written"] == 0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT processing_issues FROM catalyst_processing_runs WHERE catalyst_id = %s",
            (catalyst_id,),
        )
        processing_issues = cur.fetchone()[0]
    assert len(processing_issues) == 1
    issue = processing_issues[0]
    assert issue["reason"] == "relationship_endpoint_unresolved"
    assert issue["entity_a"] == "Issuer Co"
    assert issue["entity_b"] == "Totally Unknown Counterparty Inc"
    assert issue["unresolved_endpoints"] == ["entity_b"]
    assert issue["event_index"] == 0
    assert issue["relationship_index"] == 0
    assert issue["document_id"] == doc_ids["primary"]

    # The underlying mention is still logged exactly as before -- this fix
    # adds visibility, it doesn't replace or duplicate that mechanism.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM unresolved_entity_mentions WHERE raw_name = %s",
            ("Totally Unknown Counterparty Inc",),
        )
        assert cur.fetchone()[0] >= 1


def test_relationship_with_both_endpoints_resolved_produces_no_processing_issue(conn):
    issuer_id = make_entity(conn, "Issuer Co")
    make_entity(conn, "Counterparty Co")
    span = "Issuer Co supplies Counterparty Co."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", span)], issuer_entity_id=issuer_id,
    )
    event = {
        "event_category": "supply_agreement", "catalyst_description": "x",
        "entities": [{"entity_name": "Issuer Co", "role": "issuer", "evidence_span": span}],
        "relationships": [{
            "entity_a": "Issuer Co", "entity_b": "Counterparty Co",
            "relationship_type": "supplier", "relationship_evidence": "explicit_named",
            "source_authority": "company", "document_explicitly_states_transmission_history": False,
            "evidence_span": span,
        }],
        "surprise": None, "explicit_correction": False,
    }
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [event])}
    result = process_full(conn, catalyst_id, outputs)

    assert result["relationships_deferred_unresolved"] == 0
    assert result["relationships_written"] == 1

    with conn.cursor() as cur:
        cur.execute("SELECT processing_issues FROM catalyst_processing_runs WHERE catalyst_id = %s", (catalyst_id,))
        assert cur.fetchone()[0] == []


def test_generate_candidates_marks_ineligible_for_late_observed_relationship(conn):
    from conftest import make_extraction_run

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


def test_earlier_event_sees_relationship_discovered_by_later_event_in_same_catalyst(conn):
    """Code-review fix #4: decision_at used to be captured once at the top
    of process_catalyst, and candidates were generated inside the same
    loop that was still writing later events' relationships -- an earlier
    event's candidate pool literally could not see a relationship a later
    event in the SAME catalyst discovered. Now every relationship for the
    whole catalyst is written before ANY candidate is generated."""
    issuer_id = make_entity(conn, "Issuer Co")
    counterparty_id = make_entity(conn, "Counterparty Co")
    span_a = "Order win worth $500 million."
    span_b = "Issuer Co supplies Counterparty Co under a long-term agreement."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", f"{span_a} {span_b}")], issuer_entity_id=issuer_id,
    )
    order_event = {
        "event_category": "order_win", "catalyst_description": "Order win",
        "entities": [{"entity_name": "Issuer Co", "role": "issuer", "evidence_span": span_a}],
        "relationships": [],
        "surprise": None, "explicit_correction": False,
    }
    supply_event = {
        "event_category": "supply_agreement", "catalyst_description": "Supply agreement",
        "entities": [{"entity_name": "Issuer Co", "role": "issuer", "evidence_span": span_b}],
        "relationships": [{
            "entity_a": "Issuer Co", "entity_b": "Counterparty Co", "relationship_type": "supplier",
            "relationship_evidence": "explicit_named", "source_authority": "company",
            "document_explicitly_states_transmission_history": False,
            "evidence_span": span_b,
        }],
        "surprise": None, "explicit_correction": False,
    }
    # order_event is listed FIRST (processed first) -- supply_event, listed
    # second, is what discovers the relationship.
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [order_event, supply_event])}
    result = process_full(conn, catalyst_id, outputs)

    assert result["canonical_events_created"] == 2
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ce.event_category FROM candidate_signals cs "
            "JOIN event_versions ev ON ev.event_version_id = cs.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s AND cs.entity_id = %s",
            (catalyst_id, counterparty_id),
        )
        categories = {row[0] for row in cur.fetchall()}
    # BOTH events' candidate pools include the counterparty -- including
    # order_win, which never itself mentions any relationship.
    assert categories == {"order_win", "supply_agreement"}


# ---------------------------------------------------------------------------
# Full-pipeline idempotency
# ---------------------------------------------------------------------------

def test_full_pipeline_idempotency_on_rerun(conn):
    issuer_id = make_entity(conn, "NVIDIA Corporation")
    span = "NVIDIA raised guidance to $10 billion from $9 billion."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("primary", "primary", span)], issuer_entity_id=issuer_id,
    )
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [guidance_event("NVIDIA", span)])}

    process_full(conn, catalyst_id, outputs)

    def counts():
        tables = ["extraction_runs", "extracted_events", "canonical_events", "event_versions",
                  "event_entities", "event_document_links", "entity_relationships", "candidate_signals"]
        with conn.cursor() as cur:
            return {t: cur.execute(f"SELECT count(*) FROM {t}") or cur.fetchone()[0] for t in tables}

    first = counts()

    # Re-run the whole pipeline again over the SAME documents/catalyst/model
    # config -- extract_document should skip re-calling the LLM (already
    # 'success'), and process_catalyst should no-op (already succeeded for
    # this exact catalyst+config in catalyst_processing_runs).
    client = StubLLMClient(outputs)
    for document_id, raw_content in _docs_with_content(conn, outputs.keys()):
        extract_document(conn, client, document_id, raw_content, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    result = process_catalyst(conn, catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)

    assert result == {"skipped": "already_done_or_in_progress"}
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
    outputs = {doc_ids["primary"]: llm_output(doc_ids["primary"], [guidance_event("NVIDIA", span_a), order_event])}

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

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM canonical_events WHERE catalyst_id = %s", (catalyst_id,))
        assert cur.fetchone()[0] == 0  # NEITHER event's rows survived, not just the second one
        cur.execute(
            "SELECT status FROM catalyst_processing_runs WHERE catalyst_id = %s "
            "AND extraction_prompt_version = %s AND extractor_model_id = %s AND extractor_model_version = %s",
            (catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION),
        )
        assert cur.fetchone()[0] == "failed"  # the claim itself is reclaimable, not stuck at 'pending'

    # And a clean retry afterward succeeds completely.
    result = process_catalyst(conn, catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    assert result["canonical_events_created"] == 2
