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
alias, relationship-deferral observability) and hardening item D
(relationship_type synonyms for "acquired"/"agreement_to_acquire"/
"collaboration") hold up against the real document, AND formally document
-- with a real, reproducible, DB-backed test -- a genuine canonicalization
bug: 34 independently adjudicated distinct events collapse to 23
canonical events under the current event-fingerprint logic.
test_real_lilly_fixture_characterizes_known_canonicalization_collision
below characterizes that bug precisely; it does NOT fix it. Do not modify
_event_fingerprint or any canonicalization/merging logic because of
anything in this file -- that's Round 2, a separate, dedicated fix.

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
# Assertion 7: canonicalization known-bug characterization.
# ---------------------------------------------------------------------------

def test_real_lilly_fixture_characterizes_known_canonicalization_collision(conn):
    """Current known-bug characterization only. 34 adjudicated distinct
    source events incorrectly collapse to 23 canonical events under the
    existing event-fingerprint logic. Tracked for the dedicated Round 2
    canonicalization fix; update this docstring with the eventual
    issue/commit reference when that fix lands. This test documents the
    defect precisely so a future fix can be verified against it; 23 is NOT
    the desired long-term behavior -- Round 2 will change this assertion
    to 34."""
    catalyst_id, document_id, _lilly_entity_id, raw_content, client = _setup_replay(conn)
    _extract_and_process(conn, document_id, catalyst_id, raw_content, client)

    fixture = _load_fixture()
    description_to_index = {e["catalyst_description"]: i for i, e in enumerate(fixture["events"])}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ev.canonical_event_id, ext.raw_llm_output->>'catalyst_description' "
            "FROM extracted_events ext "
            "JOIN event_versions ev ON ev.event_version_id = ext.event_version_id "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s",
            (catalyst_id,),
        )
        rows = cur.fetchall()

    groups: dict[str, list[str]] = {}
    for canonical_event_id, description in rows:
        groups.setdefault(str(canonical_event_id), []).append(description)

    # KNOWN-BUG CHARACTERIZATION -- desired value is 34. Round 2 must
    # change this assertion, not preserve 23.
    assert len(groups) == 23

    multi_member_groups = [sorted(members) for members in groups.values() if len(members) > 1]
    multi_member_groups.sort(key=len)

    expected_2 = sorted([
        "Lilly completed the acquisition of Centessa Pharmaceuticals.",
        "Lilly entered into an agreement to acquire AtaiBeckley.",
    ])
    expected_3 = sorted([
        "Lilly completed the acquisition of Orna Therapeutics, Inc.",
        "Lilly completed the acquisition of Ajax Therapeutics, Inc.",
        "Lilly completed the acquisition of Kelonia Therapeutics, Inc.",
    ])
    expected_9 = sorted([
        "The European Commission approved Jaypirca as monotherapy for adults with chronic lymphocytic leukemia across all lines of therapy.",
        "Lilly submitted orforglipron for type 2 diabetes in the U.S.",
        "Mounjaro was added to China's National Reimbursement Drug List, which Lilly said drove lower realized prices outside the U.S.",
        "CHMP recommended Jaypirca for approval in the European Union for adults with CLL across all lines of therapy.",
        "Olomorasib received U.S. FDA Breakthrough Therapy designation for previously treated KRAS G12C-mutant advanced pancreatic cancer.",
        "Foundayo was associated with significant weight loss in women at every stage of menopause.",
        "Retatrutide produced substantial improvements in weight, A1C, knee osteoarthritis pain, and obstructive sleep apnea.",
        "Retatrutide delivered powerful weight loss in a pivotal Phase 3 obesity trial.",
        "Foundayo and Zepbound became covered for millions of Americans.",
    ])

    assert len(multi_member_groups) == 3  # exactly one 2-, one 3-, one 9-member group
    assert multi_member_groups == [expected_2, expected_3, expected_9]

    # Cross-check against the fixture's own zero-based indices, per the spec.
    expected_2_indices = sorted(description_to_index[d] for d in expected_2)
    expected_3_indices = sorted(description_to_index[d] for d in expected_3)
    expected_9_indices = sorted(description_to_index[d] for d in expected_9)
    assert expected_2_indices == [11, 14]
    assert expected_3_indices == [9, 10, 12]
    assert expected_9_indices == [6, 7, 16, 18, 19, 23, 24, 28, 31]

    # 34 - 9 - 3 - 2 + 3 = 23: the three groups each collapse to 1,
    # accounting for all 11 "missing" events -- proves the gap is exactly
    # this known, understood collapse and nothing else.
    assert 34 - 9 - 3 - 2 + 3 == 23


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
