"""
Saved-source replay against the REAL independent blind-read extraction of
the Lilly Q2 2026 earnings release (Recall Check 001,
build/RECALL_CHECK_001.md) -- 34 adjudicated events, run through the real
production entrypoints end to end: seed_entities.seed_all (the real
108-company watchlist), extract_document, process_catalyst. No stub LLM
client -- FileBackedExtractionClient replays the real saved fixture, the
same mechanism Dry Run 001 itself used.

Purpose (see the task spec this module implements): confirm the
previously-shipped fixes (evidence-span escape-repair, curated "Lilly"
alias, relationship-deferral observability, hardening item D's
relationship_type synonyms for "acquired"/"agreement_to_acquire"/
"collaboration") hold up against the real document, AND confirm the
Round 2 canonicalization fix (actor_signature + merge-witness requirement
in extraction_runner.py's _event_fingerprint /
_split_into_canonicalization_units): 34 independently adjudicated
distinct events used to collapse to 23 canonical events under the prior
event-fingerprint logic (see git history for that known-bug-characterization
round); test_real_lilly_fixture_canonicalizes_to_34_distinct_events below
is now a regression guard confirming every one of the 34 maps to its own
distinct canonical event.

Centessa Pharmaceuticals, AtaiBeckley, and Boehringer Ingelheim are
deliberately NOT added to the watchlist or created as entities anywhere
in this module -- all three staying unresolved against the real
108-company watchlist is the correct, expected, real-data outcome.
"""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from conftest import make_catalyst_with_documents

from extraction_runner import extract_document, process_catalyst
from llm_client import FileBackedExtractionClient
from seed_entities import seed_all

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "lilly_q2_2026_blind_extraction.json"
SOURCE_PATH = (
    Path(__file__).parent.parent / "dry_run_extractions" / "sources"
    / "8b89a41e-b98a-4e19-8525-5fa9f1bb8f1b.txt"
)
LILLY_CIK = "0000059478"
PROMPT_VERSION = "1.2.0"
MODEL_ID = "recall-check-001-replay"
MODEL_VERSION = "v1"

IDEMPOTENCY_TABLES = [
    "canonical_events", "event_versions", "extracted_events", "entity_relationships",
    "catalyst_processing_runs", "unresolved_entity_mentions", "event_entities",
    "event_document_links", "candidate_signals", "candidate_supporting_relationships",
]


def _load_stripped_source_text(path: Path = SOURCE_PATH) -> str:
    """build/dry_run_extractions/sources/*.txt files carry a fixed 3-line
    header before the real document text: 'SOURCE URL: <url>', then
    'LENGTH: <n>', then one blank line. This is the one, explicit,
    deterministic rule for stripping that header -- there was no existing
    helper for it, so this is written once here rather than inlined ad hoc
    per-test."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n", 3)
    assert lines[0].startswith("SOURCE URL:"), "unexpected source file header format"
    assert lines[1].startswith("LENGTH:"), "unexpected source file header format"
    assert lines[2] == "", "unexpected source file header format"
    return lines[3]


def _load_fixture() -> dict:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


class _CountingExtractionClient:
    """Wraps FileBackedExtractionClient to count real .extract() calls --
    proves the claim-then-update state machine (claim_extraction_run)
    skips the "LLM call" entirely on a second call at an already-completed
    (document, prompt, model) identity, rather than just asserting on
    output shape."""

    def __init__(self, extractions_dir):
        self._inner = FileBackedExtractionClient(extractions_dir)
        self.call_count = 0

    def extract(self, document_id, raw_content, prompt_version):
        self.call_count += 1
        return self._inner.extract(document_id, raw_content, prompt_version)


def _setup_replay(conn):
    """Steps 1-4 of the replay spec: seed the real 108-company watchlist,
    load the real saved source text, and create the catalyst/document link
    via the real test helper (not a hand-assembled insert) -- process_catalyst's
    own document discovery reads catalyst_documents, so this is required
    for the replay to exercise the real entrypoint at all."""
    seeded = seed_all(conn)
    conn.commit()
    lilly_entity_id = dict(seeded)[LILLY_CIK]

    raw_content = _load_stripped_source_text()
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("main", "primary", raw_content)],
        issuer_entity_id=lilly_entity_id, issuer_cik=LILLY_CIK,
    )
    conn.commit()
    document_id = doc_ids["main"]

    fixture_copy = copy.deepcopy(_load_fixture())  # never mutate the checked-in fixture itself
    fixture_copy["document_id"] = document_id
    extractions_dir = tempfile.mkdtemp()
    with open(Path(extractions_dir) / f"{document_id}.json", "w", encoding="utf-8") as f:
        json.dump(fixture_copy, f)
    client = _CountingExtractionClient(extractions_dir)

    return catalyst_id, document_id, lilly_entity_id, raw_content, client


def _extract_and_process(conn, document_id, catalyst_id, raw_content, client):
    """Steps 5-6: the real production entrypoints -- extract_document (not
    a direct call into the validator) and process_catalyst (not
    _do_process_catalyst directly)."""
    extract_document(conn, client, document_id, raw_content, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    return process_catalyst(conn, catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)


def _fetch_cleaned_output(conn, document_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cleaned_llm_output, validation_drop_log FROM extraction_runs "
            "WHERE document_id = %s AND extraction_prompt_version = %s "
            "AND extractor_model_id = %s AND extractor_model_version = %s",
            (document_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION),
        )
        cleaned_output, drop_log = cur.fetchone()
    return cleaned_output, drop_log


def _table_counts(conn, tables):
    counts = {}
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
    return counts


# ---------------------------------------------------------------------------
# Assertions 1, 2, 3, 4, 5, 6: validation completeness, span repair,
# guidance ranges, issuer identity, bare "Lilly" resolution, relationship
# deferral -- all against the real production path.
# ---------------------------------------------------------------------------

def test_real_lilly_fixture_replay_validation_and_resolution(conn):
    catalyst_id, document_id, lilly_entity_id, raw_content, client = _setup_replay(conn)
    result = _extract_and_process(conn, document_id, catalyst_id, raw_content, client)

    cleaned_output, drop_log = _fetch_cleaned_output(conn, document_id)

    # ---- 1. Validation-level completeness: nothing dropped at validation ----
    assert len(cleaned_output["events"]) == 34

    # ---- 2. Span repair, precisely ----
    span_repairs = cleaned_output["span_repairs"]
    assert len(span_repairs) == 3
    for repair in span_repairs:
        assert repair["span_match_mode"] == "escaped_punctuation_repair"
        assert repair["original_span"] != repair["verified_span"]
    repaired_event_indices = {r["event_index"] for r in span_repairs}
    assert repaired_event_indices == {3, 4, 33}  # the three guidance-table spans
    for i in (3, 4, 33):
        assert not any(f"event[{i}]" in msg for msg in drop_log)

    # ---- 3. Guidance range integrity: no midpoint or derived value anywhere ----
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ee.surprise_type, ee.observed_value_low, ee.observed_value_high, "
            "ee.reference_value_low, ee.reference_value_high "
            "FROM extracted_events ee "
            "JOIN event_versions ev ON ev.event_version_id = ee.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s AND ee.surprise_type IN "
            "('revenue_guidance', 'eps_guidance', 'performance_margin_guidance')",
            (catalyst_id,),
        )
        by_type = {row[0]: row[1:] for row in cur.fetchall()}
    assert by_type["revenue_guidance"] == (85.0, 87.0, 82.0, 85.0)
    assert by_type["eps_guidance"] == (35.5, 36.5, 35.5, 37.0)
    assert by_type["performance_margin_guidance"] == (49.0, 50.5, 47.0, 48.5)

    # ---- 4. Issuer identity: every issuer-role entity is the real seeded Lilly ----
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT entity_id FROM event_entities ee "
            "JOIN event_versions ev ON ev.event_version_id = ee.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s AND ee.role = 'issuer'",
            (catalyst_id,),
        )
        issuer_entity_ids = {str(row[0]) for row in cur.fetchall()}
    assert issuer_entity_ids == {str(lilly_entity_id)}

    # ---- 5. Bare "Lilly" (role=buyer) resolves via ordinary resolution ----
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT entity_id FROM event_entities ee "
            "JOIN event_versions ev ON ev.event_version_id = ee.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s AND ee.role = 'buyer'",
            (catalyst_id,),
        )
        buyer_entity_ids = {str(row[0]) for row in cur.fetchall()}
    assert buyer_entity_ids == {str(lilly_entity_id)}  # not the issuer shortcut -- role != "issuer"

    # ---- 6. Relationship deferral: exact count, verified against the real fixture ----
    assert result["relationships_deferred_unresolved"] == 3

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entity_relationships WHERE source_document_id = %s", (document_id,))
        assert cur.fetchone()[0] == 0  # none of the 3 written -- both endpoints required

    with conn.cursor() as cur:
        cur.execute(
            "SELECT processing_issues FROM catalyst_processing_runs WHERE catalyst_id = %s "
            "AND extraction_prompt_version = %s AND extractor_model_id = %s AND extractor_model_version = %s",
            (catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION),
        )
        processing_issues = cur.fetchone()[0]  # queried directly from the DB, not the in-memory result

    assert len(processing_issues) == 3
    by_entity_b = {issue["entity_b"]: issue for issue in processing_issues}
    assert set(by_entity_b) == {"Centessa Pharmaceuticals", "AtaiBeckley", "Boehringer Ingelheim"}
    for entity_b, issue in by_entity_b.items():
        assert issue["reason"] == "relationship_endpoint_unresolved"
        assert issue["unresolved_endpoints"] == ["entity_b"]  # proves entity_a DID resolve
    assert by_entity_b["Centessa Pharmaceuticals"]["entity_a"] == "Lilly"
    assert by_entity_b["AtaiBeckley"]["entity_a"] == "Lilly"
    assert by_entity_b["Boehringer Ingelheim"]["entity_a"] == "Eli Lilly and Company"


# ---------------------------------------------------------------------------
# Assertion 7: canonicalization regression guard (Round 2 fix).
# ---------------------------------------------------------------------------

def test_real_lilly_fixture_canonicalizes_to_34_distinct_events(conn):
    """Regression guard for the Round 2 canonicalization fix
    (actor_signature + merge-witness requirement in _event_fingerprint /
    _split_into_canonicalization_units, extraction_runner.py). Before that
    fix, these same 34 independently adjudicated distinct events
    incorrectly collapsed to 23 canonical events (nine, three, and two of
    them respectively sharing a category + issuer-only-actors +
    no-quantified-surprise fingerprint with nothing to distinguish them --
    see git history for the prior known-bug-characterization version of
    this test). Now every one of the 34 adjudicated events maps to its own
    distinct canonical event: no collision groups at all for this fixture."""
    catalyst_id, document_id, _lilly_entity_id, raw_content, client = _setup_replay(conn)
    _extract_and_process(conn, document_id, catalyst_id, raw_content, client)

    with conn.cursor() as cur:
        cur.execute("SELECT canonical_event_id FROM canonical_events WHERE catalyst_id = %s", (catalyst_id,))
        canonical_event_ids = [str(row[0]) for row in cur.fetchall()]

    assert len(canonical_event_ids) == 34
    assert len(set(canonical_event_ids)) == 34  # all distinct -- no collision groups


# ---------------------------------------------------------------------------
# Assertion 8: cover-page regression guard, via the real production path.
# ---------------------------------------------------------------------------

def test_cover_page_regression_guard_via_real_production_path(conn):
    """Re-runs the Dry Run 001 cover-page fix's scenario (a cover page that
    only points to an exhibit should extract to an empty events array)
    through the real production path -- extract_document +
    FileBackedExtractionClient -- rather than the stubbed-client unit test
    in test_extraction_runner.py."""
    seeded = seed_all(conn)
    conn.commit()
    lilly_entity_id = dict(seeded)[LILLY_CIK]

    cover_content = "Eli Lilly and Company 8-K cover page. See Exhibit 99.1 for the press release."
    catalyst_id, doc_ids = make_catalyst_with_documents(
        conn, [("cover", "primary", cover_content)],
        issuer_entity_id=lilly_entity_id, issuer_cik=LILLY_CIK,
    )
    conn.commit()
    document_id = doc_ids["cover"]

    output = {"document_id": document_id, "extraction_prompt_version": PROMPT_VERSION, "events": []}
    extractions_dir = tempfile.mkdtemp()
    with open(Path(extractions_dir) / f"{document_id}.json", "w", encoding="utf-8") as f:
        json.dump(output, f)
    client = FileBackedExtractionClient(extractions_dir)

    extract_document(conn, client, document_id, cover_content, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    cleaned_output, _drop_log = _fetch_cleaned_output(conn, document_id)
    assert cleaned_output["events"] == []

    result = process_catalyst(conn, catalyst_id, PROMPT_VERSION, MODEL_ID, MODEL_VERSION)
    assert result["canonical_events_created"] == 0


# ---------------------------------------------------------------------------
# Assertion 9: idempotency.
# ---------------------------------------------------------------------------

def test_real_lilly_fixture_replay_is_idempotent_on_rerun(conn):
    catalyst_id, document_id, _lilly_entity_id, raw_content, client = _setup_replay(conn)

    _extract_and_process(conn, document_id, catalyst_id, raw_content, client)
    assert client.call_count == 1

    counts_after_first_run = _table_counts(conn, IDEMPOTENCY_TABLES)

    second_result = _extract_and_process(conn, document_id, catalyst_id, raw_content, client)

    # The claim-then-update state machine recognizes the already-completed
    # (document, prompt, model) identity and never calls the "LLM" again.
    assert client.call_count == 1

    # process_catalyst recognizes the already-'success' (catalyst, prompt,
    # model) identity and skips reprocessing rather than redoing the batch.
    assert second_result.get("skipped") == "already_done_or_in_progress"

    counts_after_second_run = _table_counts(conn, IDEMPOTENCY_TABLES)
    assert counts_after_second_run == counts_after_first_run
