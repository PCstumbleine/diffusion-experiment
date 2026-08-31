#!/usr/bin/env python3
"""
Extraction runner -- raw filing -> LLM extraction -> entity resolution ->
relationship write -> candidate generation. Implements
docs/EXTRACTION_RUNNER_DESIGN_V2.md end to end.

Two-phase pipeline, matching the design doc's own split:

  Phase 1 (per document, §2b/§2c): extract_document() calls the LLM against
  one raw_documents row, validates the output, and writes an
  extraction_runs row (+ extracted_events on success). Committed
  immediately per document -- this is checkpointed progress, not part of
  the atomic catalyst batch below, so a slow/expensive LLM call is never
  redone just because a later step in the same run fails.

  Phase 2 (per catalyst, §2a/§3/§4/§5): process_catalyst() runs ONLY once
  every document belonging to that catalyst has reached a terminal
  extraction_runs state (success or failed). It resolves entities,
  canonicalizes matching events across the catalyst's documents into
  canonical_events/event_versions/event_entities/event_document_links,
  writes entity_relationships, freezes decision_at, and generates
  candidates -- all in ONE transaction, committed atomically at the end
  (design doc §2c: "All writes for one successful catalyst-level
  processing batch ... commit atomically"). catalysts.canonicalization_
  completed_at makes this phase idempotent: a catalyst already merged is
  never reprocessed.

relationship_type vocabulary and directionality (§4a): the LLM's own
free-text relationship_type (extraction_prompt_v1.md places no enum
constraint on it) is mapped onto the closed vocabulary migration 002
added -- supplier, customer, competitor, partner, acquirer_target -- via
RELATIONSHIP_TYPE_SYNONYMS below. Direction is NOT re-interpreted: the
LLM's entity_a/entity_b order is stored as-is into entity_id_a/entity_id_b,
and the TYPE itself carries the fixed meaning ("a supplies b" for
'supplier', "a is a customer of b" for 'customer', "a is acquiring b" for
'acquirer_target'; 'competitor'/'partner' are symmetric). A relationship_
type that doesn't map to the closed vocabulary is dropped and logged, the
same as an invalid evidence_span -- never force-bucketed into an
arbitrary value or kept as free text (see validate_extraction_output).
This mapping question is not addressed by the design doc; flagged in the
implementation report as a gap it left open.

Known, flagged gap (design doc does not specify a mechanism): "event_
versions (version_number=1 unless the source itself flags explicit_
correction=true)" is implemented as version_number=1 for every event this
runner canonicalizes. There is no field anywhere in extraction_prompt_v1.md's
schema identifying WHICH prior disclosure a correction refers to (only a
bare boolean), and no cross-catalyst event-matching mechanism is specified
by the design doc -- so linking a correction to a specific earlier
event_version is not implemented. explicit_correction is preserved
verbatim in extracted_events.raw_llm_output for future manual/automated
linkage; it does not yet drive event_versions.version_number.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

import entity_resolution
from llm_client import PROMPT_VERSION, load_prompt_texts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extraction_runner")

DB_DSN = "dbname=diffusion_experiment user=postgres"

VALID_EVENT_CATEGORIES = {
    "guidance_revision", "capacity_change", "order_win", "order_loss",
    "supply_agreement", "customer_agreement", "earnings_surprise",
    "buyback_or_capital_return", "acquisition_or_divestiture", "other_material_event",
}
VALID_ROLES = {"issuer", "subject", "supplier", "customer", "buyer", "target", "partner", "counterparty"}
VALID_RELATIONSHIP_EVIDENCE = {"explicit_named", "quantified_named", "inferred_structured"}
VALID_SOURCE_AUTHORITY = {"government", "regulatory_filing", "company", "licensed_commercial", "secondary_inference"}

RELATIONSHIP_TYPE_SYNONYMS = {
    "supplier": "supplier", "vendor": "supplier", "key supplier": "supplier",
    "supplies": "supplier", "supplier_to": "supplier", "supplier to": "supplier",
    "customer": "customer", "buyer": "customer", "key customer": "customer", "client": "customer",
    "competitor": "competitor", "rival": "competitor", "competes with": "competitor",
    "partner": "partner", "strategic partner": "partner", "joint venture partner": "partner",
    "joint venture": "partner", "alliance": "partner",
    "acquirer_target": "acquirer_target", "acquisition target": "acquirer_target",
    "acquirer": "acquirer_target", "target": "acquirer_target", "merger partner": "acquirer_target",
    "acquisition_or_divestiture": "acquirer_target",
}

ELIGIBILITY_POLICY_VERSION = "candidate_eligibility_v1"


class ExtractionValidationError(Exception):
    """Raised for a document-level validation failure (unparseable output,
    wrong prompt version, missing required top-level fields) -- terminal
    'failed' state for that document's extraction_runs row. Distinct from
    a per-claim drop (bad evidence_span, unmapped relationship_type),
    which is logged and dropped without failing the whole document."""


# ---------------------------------------------------------------------------
# Phase 1: per-document extraction
# ---------------------------------------------------------------------------

def select_unprocessed_documents(conn, extraction_prompt_version: str, extractor_model_id: str,
                                  extractor_model_version: str) -> list[tuple[str, str]]:
    """Documents with no SUCCESSFUL extraction_runs row yet at this exact
    (prompt version, model) identity -- a previously 'failed' document is
    retried; a 'success' one never is (idempotency, design doc §2c)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rd.document_id, rd.raw_content
            FROM raw_documents rd
            WHERE NOT EXISTS (
                SELECT 1 FROM extraction_runs er
                WHERE er.document_id = rd.document_id
                  AND er.extraction_prompt_version = %s
                  AND er.extractor_model_id = %s
                  AND er.extractor_model_version = %s
                  AND er.status = 'success'
            )
            ORDER BY rd.created_at
            """,
            (extraction_prompt_version, extractor_model_id, extractor_model_version),
        )
        return [(str(doc_id), raw_content) for doc_id, raw_content in cur.fetchall()]


def validate_extraction_output(output: dict, raw_content: str, requested_prompt_version: str) -> tuple[dict, list[str]]:
    """Validates and cleans one document's raw LLM output. Returns
    (cleaned_output, drop_log) -- drop_log entries describe individual
    claims dropped (bad evidence_span, unmapped relationship_type,
    malformed event), never silently discarded without a trace.

    Raises ExtractionValidationError for a document-level failure: output
    isn't a dict, missing required top-level keys, or a prompt-version
    mismatch (design doc §2b: "extraction_prompt_version in the output
    matches what was requested"). An individual malformed EVENT inside an
    otherwise-valid document is dropped and logged, not treated as a
    whole-document failure -- matching the extraction prompt's own
    per-claim "if you cannot point to a span, do not extract" philosophy
    rather than an all-or-nothing document gate. This split is a judgment
    call the design doc's §2b/§6 don't fully specify (it says the output
    "matches the documented JSON schema" without saying whether a single
    bad event fails the whole document); flagged as such.
    """
    drop_log: list[str] = []

    if not isinstance(output, dict):
        raise ExtractionValidationError(f"LLM output is not a JSON object: {type(output)!r}")
    for required_key in ("document_id", "extraction_prompt_version", "events"):
        if required_key not in output:
            raise ExtractionValidationError(f"LLM output missing required field {required_key!r}")
    if output["extraction_prompt_version"] != requested_prompt_version:
        raise ExtractionValidationError(
            f"LLM output extraction_prompt_version={output['extraction_prompt_version']!r} "
            f"does not match requested {requested_prompt_version!r}"
        )
    if not isinstance(output["events"], list):
        raise ExtractionValidationError("LLM output 'events' is not a list")

    def span_ok(span) -> bool:
        return isinstance(span, str) and span != "" and span in raw_content

    cleaned_events = []
    for i, event in enumerate(output["events"]):
        if not isinstance(event, dict):
            drop_log.append(f"event[{i}]: not an object, dropped")
            continue
        category = event.get("event_category")
        if category not in VALID_EVENT_CATEGORIES:
            drop_log.append(f"event[{i}]: invalid event_category {category!r}, dropped")
            continue
        if not isinstance(event.get("entities"), list):
            drop_log.append(f"event[{i}]: missing/invalid entities list, dropped")
            continue

        cleaned_entities = []
        for j, ent in enumerate(event["entities"]):
            if not isinstance(ent, dict) or ent.get("role") not in VALID_ROLES or not ent.get("entity_name"):
                drop_log.append(f"event[{i}].entities[{j}]: malformed, dropped")
                continue
            if not span_ok(ent.get("evidence_span")):
                drop_log.append(
                    f"event[{i}].entities[{j}] ({ent.get('entity_name')!r}): evidence_span not an exact "
                    "substring of raw_content, dropped"
                )
                continue
            cleaned_entities.append(ent)
        if not cleaned_entities:
            drop_log.append(f"event[{i}]: no entities survived validation, whole event dropped")
            continue
        event = dict(event, entities=cleaned_entities)

        cleaned_relationships = []
        for j, rel in enumerate(event.get("relationships") or []):
            if not isinstance(rel, dict):
                drop_log.append(f"event[{i}].relationships[{j}]: not an object, dropped")
                continue
            if rel.get("relationship_evidence") not in VALID_RELATIONSHIP_EVIDENCE:
                drop_log.append(f"event[{i}].relationships[{j}]: invalid relationship_evidence, dropped")
                continue
            if rel.get("source_authority") not in VALID_SOURCE_AUTHORITY:
                drop_log.append(f"event[{i}].relationships[{j}]: invalid source_authority, dropped")
                continue
            if not span_ok(rel.get("evidence_span")):
                drop_log.append(f"event[{i}].relationships[{j}]: evidence_span not an exact substring, dropped")
                continue
            mapped_type = RELATIONSHIP_TYPE_SYNONYMS.get(str(rel.get("relationship_type", "")).strip().lower())
            if mapped_type is None:
                drop_log.append(
                    f"event[{i}].relationships[{j}]: relationship_type {rel.get('relationship_type')!r} "
                    "does not map to the closed vocabulary, dropped"
                )
                continue
            rel = dict(rel, relationship_type=mapped_type)
            cleaned_relationships.append(rel)
        event = dict(event, relationships=cleaned_relationships)

        surprise = event.get("surprise")
        if surprise is not None:
            if not isinstance(surprise, dict) or not span_ok(surprise.get("evidence_span")):
                drop_log.append(f"event[{i}].surprise: invalid or bad evidence_span, dropped (kept event, surprise=null)")
                surprise = None
            event = dict(event, surprise=surprise)

        cleaned_events.append(event)

    return dict(output, events=cleaned_events), drop_log


def extract_document(conn, llm_client, document_id: str, raw_content: str, prompt_version: str,
                      extractor_model_id: str, extractor_model_version: str) -> str:
    """Runs (or reuses) one document's extraction. Always returns an
    extraction_run_id; commits its own row immediately (Phase 1, see
    module docstring) regardless of success/failure."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT extraction_run_id FROM extraction_runs
            WHERE document_id = %s AND extraction_prompt_version = %s
              AND extractor_model_id = %s AND extractor_model_version = %s
              AND status = 'success'
            """,
            (document_id, prompt_version, extractor_model_id, extractor_model_version),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

    run_id = str(uuid.uuid4())
    try:
        raw_output = llm_client.extract(document_id, raw_content, prompt_version)
        cleaned_output, drop_log = validate_extraction_output(raw_output, raw_content, prompt_version)
        for msg in drop_log:
            log.warning("Document %s: %s", document_id, msg)
    except Exception as exc:
        log.exception("Extraction failed for document %s", document_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extraction_runs
                    (extraction_run_id, document_id, extraction_prompt_version,
                     extractor_model_id, extractor_model_version, status, error, completed_at)
                VALUES (%s, %s, %s, %s, %s, 'failed', %s, now())
                """,
                (run_id, document_id, prompt_version, extractor_model_id, extractor_model_version, str(exc)),
            )
        conn.commit()
        return run_id

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extraction_runs
                (extraction_run_id, document_id, extraction_prompt_version,
                 extractor_model_id, extractor_model_version, status, raw_llm_output, completed_at)
            VALUES (%s, %s, %s, %s, %s, 'success', %s, now())
            """,
            (run_id, document_id, prompt_version, extractor_model_id, extractor_model_version,
             psycopg2.extras.Json(cleaned_output)),
        )
    # extracted_events references event_versions, which doesn't exist until
    # catalyst-level canonicalization (§2a/§2b) -- so the validated,
    # per-document extraction is staged here as extraction_runs.raw_llm_output
    # only; extracted_events / canonical_events / event_versions are written
    # in Phase 2 (process_catalyst), once an event_version_id actually exists.
    conn.commit()
    return run_id


# ---------------------------------------------------------------------------
# Phase 2: catalyst-level canonicalization, resolution, relationships,
# candidate generation
# ---------------------------------------------------------------------------

def _event_fingerprint(event: dict, resolved_roles: frozenset) -> tuple:
    surprise = event.get("surprise") or {}
    return (
        event["event_category"],
        resolved_roles,
        surprise.get("surprise_type"),
        surprise.get("period"),
        surprise.get("observed_value"),
        surprise.get("reference_value"),
    )


def _get_catalyst_documents_with_terminal_runs(conn, catalyst_id: str, prompt_version: str,
                                                extractor_model_id: str, extractor_model_version: str):
    """Returns None if any document in this catalyst has not yet reached a
    terminal extraction_runs state at this identity -- the caller must skip
    processing this catalyst for now. Otherwise returns a list of
    (document_id, document_role, status, raw_llm_output) tuples."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT document_id, document_role FROM catalyst_documents WHERE catalyst_id = %s",
            (catalyst_id,),
        )
        catalyst_docs = [(str(doc_id), role) for doc_id, role in cur.fetchall()]

    results = []
    for document_id, role in catalyst_docs:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, raw_llm_output FROM extraction_runs
                WHERE document_id = %s AND extraction_prompt_version = %s
                  AND extractor_model_id = %s AND extractor_model_version = %s
                ORDER BY started_at DESC LIMIT 1
                """,
                (document_id, prompt_version, extractor_model_id, extractor_model_version),
            )
            row = cur.fetchone()
        if row is None or row[0] not in ("success", "failed"):
            return None  # not terminal yet -- catalyst isn't ready
        results.append((document_id, role, row[0], row[1]))
    return results


def classify_relationship_eligibility(rel: dict, decision_at: datetime) -> tuple[bool, str | None]:
    """§5a's eligibility timing/validity checks for ONE relationship row."""
    if rel["relationship_evidence"] not in ("explicit_named", "quantified_named"):
        return False, f"relationship_evidence={rel['relationship_evidence']}"
    if rel["evidence_publicly_available_at"] > decision_at:
        return False, "evidence_publicly_available_at_after_decision_at"
    if rel["system_observed_at"] > decision_at:
        return False, "system_observed_at_after_decision_at"
    if rel["relationship_valid_from"] is not None and rel["relationship_valid_from"] > decision_at:
        return False, "relationship_not_yet_valid"
    if rel["relationship_valid_to"] is not None and rel["relationship_valid_to"] <= decision_at:
        return False, "relationship_no_longer_valid"
    if rel["record_superseded_at"] is not None and rel["record_superseded_at"] <= decision_at:
        return False, "relationship_superseded"
    return True, None


_INELIGIBLE_REASON_PRIORITY = [
    "evidence_publicly_available_at_after_decision_at",
    "system_observed_at_after_decision_at",
    "relationship_not_yet_valid",
    "relationship_no_longer_valid",
    "relationship_superseded",
]


def _pick_ineligible_reason(reasons: list[str]) -> str:
    for candidate in _INELIGIBLE_REASON_PRIORITY:
        if candidate in reasons:
            return candidate
    return reasons[0]


def generate_candidates_for_event_version(conn, event_version_id: str, issuer_entity_id: str,
                                           decision_at: datetime) -> dict:
    """§5/§5a: one candidate_signals row per counterparty entity connected
    to the issuer via at least one entity_relationships row, as of
    decision_at. Idempotent via candidate_signals' own UNIQUE(event_version_id,
    entity_id) constraint (ON CONFLICT DO NOTHING)."""
    if issuer_entity_id is None:
        return {"eligible": 0, "ineligible": 0}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT relationship_id, entity_id_a, entity_id_b, relationship_evidence,
                   evidence_publicly_available_at, system_observed_at,
                   relationship_valid_from, relationship_valid_to, record_superseded_at
            FROM entity_relationships
            WHERE entity_id_a = %s OR entity_id_b = %s
            """,
            (issuer_entity_id, issuer_entity_id),
        )
        rows = cur.fetchall()

    by_counterparty: dict[str, list[dict]] = {}
    for rel in rows:
        counterparty = str(rel["entity_id_b"]) if str(rel["entity_id_a"]) == str(issuer_entity_id) else str(rel["entity_id_a"])
        by_counterparty.setdefault(counterparty, []).append(rel)

    counts = {"eligible": 0, "ineligible": 0}
    for counterparty_id, rels in by_counterparty.items():
        reasons = []
        any_eligible = False
        for rel in rels:
            ok, reason = classify_relationship_eligibility(rel, decision_at)
            if ok:
                any_eligible = True
            else:
                reasons.append(reason)

        status = "eligible" if any_eligible else "ineligible"
        reason = None if any_eligible else _pick_ineligible_reason(reasons)
        counts[status] += 1

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO candidate_signals
                    (candidate_id, event_version_id, entity_id, eligibility_status,
                     eligibility_reason, policy_version, decision_timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_version_id, entity_id) DO NOTHING
                RETURNING candidate_id
                """,
                (str(uuid.uuid4()), event_version_id, counterparty_id, status, reason,
                 ELIGIBILITY_POLICY_VERSION, decision_at),
            )
            result = cur.fetchone()
            if result is None:
                cur.execute(
                    "SELECT candidate_id FROM candidate_signals WHERE event_version_id = %s AND entity_id = %s",
                    (event_version_id, counterparty_id),
                )
                result = cur.fetchone()
            candidate_id = result[0]

            for rel in rels:
                cur.execute(
                    "INSERT INTO candidate_supporting_relationships (candidate_id, relationship_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (candidate_id, rel["relationship_id"]),
                )
    return counts


def process_catalyst(conn, catalyst_id: str, prompt_version: str, extractor_model_id: str,
                      extractor_model_version: str) -> dict:
    """Phase 2 entry point. Returns a counts dict; returns
    {'skipped': 'not_ready'} if some document in the catalyst hasn't
    reached a terminal extraction state yet, or {'skipped': 'already_done'}
    if canonicalization_completed_at is already set (idempotency)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT issuer_entity_id, canonicalization_completed_at FROM catalysts WHERE catalyst_id = %s",
            (catalyst_id,),
        )
        issuer_entity_id, already_done = cur.fetchone()
    if already_done is not None:
        return {"skipped": "already_done"}

    docs = _get_catalyst_documents_with_terminal_runs(
        conn, catalyst_id, prompt_version, extractor_model_id, extractor_model_version,
    )
    if docs is None:
        return {"skipped": "not_ready"}

    processed_at = datetime.now(timezone.utc)
    resolution_index = entity_resolution.build_resolution_index(conn)

    counts = {
        "mentions_matched": 0, "mentions_unresolved": 0,
        "canonical_events_created": 0, "relationships_written": 0,
        "candidates_eligible": 0, "candidates_ineligible": 0,
    }

    def resolve(raw_name: str, document_id: str, extraction_run_id: str) -> str | None:
        entity_id = entity_resolution.resolve_entity_name(
            conn, raw_name, document_id, extraction_run_id, index=resolution_index,
        )
        if entity_id is not None:
            counts["mentions_matched"] += 1
        else:
            counts["mentions_unresolved"] += 1
        return entity_id

    # Need each document's own extraction_run_id (for resolution provenance
    # and entity_relationships.extraction_run_id) alongside its raw output.
    doc_run_ids: dict[str, str] = {}
    for document_id, _role, status, _raw in docs:
        if status != "success":
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT extraction_run_id FROM extraction_runs
                WHERE document_id = %s AND extraction_prompt_version = %s
                  AND extractor_model_id = %s AND extractor_model_version = %s AND status = 'success'
                """,
                (document_id, prompt_version, extractor_model_id, extractor_model_version),
            )
            doc_run_ids[document_id] = str(cur.fetchone()[0])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT document_id, raw_content FROM raw_documents WHERE document_id::text = ANY(%s)",
            (list(doc_run_ids.keys()),),
        )
        raw_content_by_doc = {str(doc_id): content for doc_id, content in cur.fetchall()}

    # Build (document_id, event_index, event_dict, resolved_entities) for
    # every successfully-extracted event across the catalyst's documents,
    # then group by fingerprint (§2a).
    groups: dict[tuple, list[tuple[str, dict, list[tuple[str, str]]]]] = {}
    primary_document_id = next((doc_id for doc_id, role, _s, _r in docs if role == "primary"), None)

    for document_id, _role, status, raw_output in docs:
        if status != "success" or raw_output is None:
            continue
        extraction_run_id = doc_run_ids[document_id]
        for event in raw_output.get("events", []):
            resolved_entities = []
            for ent in event.get("entities", []):
                entity_id = resolve(ent["entity_name"], document_id, extraction_run_id)
                if entity_id is not None:
                    resolved_entities.append((entity_id, ent["role"]))
            role_set = frozenset(resolved_entities)
            fingerprint = _event_fingerprint(event, role_set)
            groups.setdefault(fingerprint, []).append((document_id, event, resolved_entities))

    for fingerprint, members in groups.items():
        event_category = fingerprint[0]
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO canonical_events (catalyst_id, event_category) VALUES (%s, %s) "
                "RETURNING canonical_event_id",
                (catalyst_id, event_category),
            )
            canonical_event_id = cur.fetchone()[0]

            # Known gap (see module docstring): version_number is always 1;
            # explicit_correction does not yet drive a real supersession
            # chain, since no field identifies which prior event it corrects.
            cur.execute(
                "INSERT INTO event_versions (canonical_event_id, version_number, decision_at) "
                "VALUES (%s, 1, %s) RETURNING event_version_id",
                (canonical_event_id, processed_at),
            )
            event_version_id = cur.fetchone()[0]
        counts["canonical_events_created"] += 1

        all_resolved_roles: set[tuple[str, str]] = set()
        for document_id, event, resolved_entities in members:
            all_resolved_roles.update(resolved_entities)

            surprise = event.get("surprise") or {}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extracted_events
                        (event_version_id, surprise_type, observed_value, reference_value,
                         reference_source, reference_timestamp, unit, period,
                         extraction_prompt_version, raw_llm_output)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (event_version_id, surprise.get("surprise_type"), surprise.get("observed_value"),
                     surprise.get("reference_value"), surprise.get("reference_source"),
                     surprise.get("reference_timestamp"), surprise.get("unit"), surprise.get("period"),
                     prompt_version, psycopg2.extras.Json(event)),
                )

            evidence_start, evidence_end = None, None
            first_span = next((e.get("evidence_span") for e in event.get("entities", []) if e.get("evidence_span")), None)
            if first_span:
                content = raw_content_by_doc.get(document_id, "")
                pos = content.find(first_span)
                if pos != -1:
                    evidence_start, evidence_end = pos, pos + len(first_span)

            link_role = "primary_source" if document_id == primary_document_id else "corroborating"
            if event.get("explicit_correction"):
                link_role = "correction"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO event_document_links
                        (canonical_event_id, document_id, relationship_type,
                         evidence_span_start, evidence_span_end)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (canonical_event_id, document_id, evidence_span_start, evidence_span_end)
                    DO NOTHING
                    """,
                    (canonical_event_id, document_id, link_role, evidence_start, evidence_end),
                )

            extraction_run_id = doc_run_ids[document_id]
            for rel in event.get("relationships", []):
                entity_id_a = resolve(rel["entity_a"], document_id, extraction_run_id)
                entity_id_b = resolve(rel["entity_b"], document_id, extraction_run_id)
                if entity_id_a is None or entity_id_b is None or entity_id_a == entity_id_b:
                    continue
                public_at = processed_at
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT canonical_first_public_at FROM raw_documents WHERE document_id = %s",
                        (document_id,),
                    )
                    canonical_public_row = cur.fetchone()
                    if canonical_public_row and canonical_public_row[0]:
                        public_at = canonical_public_row[0]
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO entity_relationships
                            (entity_id_a, entity_id_b, relationship_type, source_authority,
                             relationship_evidence, shock_transmission_evidence,
                             raw_llm_relationship_score, evidence_publicly_available_at,
                             system_observed_at, source_document_id, extraction_run_id)
                        VALUES (%s, %s, %s, %s, %s, 'new_or_unobserved', %s, %s, %s, %s, %s)
                        ON CONFLICT (extraction_run_id, entity_id_a, entity_id_b, relationship_type)
                        DO NOTHING
                        """,
                        (entity_id_a, entity_id_b, rel["relationship_type"], rel["source_authority"],
                         rel["relationship_evidence"], rel.get("raw_llm_relationship_score"),
                         public_at, processed_at, document_id, extraction_run_id),
                    )
                    if cur.rowcount:
                        counts["relationships_written"] += 1

        with conn.cursor() as cur:
            for entity_id, role in all_resolved_roles:
                cur.execute(
                    "INSERT INTO event_entities (event_version_id, entity_id, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT (event_version_id, entity_id, role) DO NOTHING",
                    (event_version_id, entity_id, role),
                )

        candidate_counts = generate_candidates_for_event_version(
            conn, event_version_id, issuer_entity_id, processed_at,
        )
        counts["candidates_eligible"] += candidate_counts["eligible"]
        counts["candidates_ineligible"] += candidate_counts["ineligible"]

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE catalysts SET canonicalization_completed_at = %s WHERE catalyst_id = %s",
            (processed_at, catalyst_id),
        )
    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# Orchestration / CLI
# ---------------------------------------------------------------------------

def run_extraction_pass(conn, llm_client, prompt_version: str, extractor_model_id: str,
                         extractor_model_version: str) -> dict:
    summary = {
        "documents_processed": 0, "documents_failed": 0,
        "mentions_matched": 0, "mentions_unresolved": 0,
        "canonical_events_created": 0, "relationships_written": 0,
        "candidates_eligible": 0, "candidates_ineligible": 0,
        "catalysts_canonicalized": 0,
    }

    documents = select_unprocessed_documents(conn, prompt_version, extractor_model_id, extractor_model_version)
    for document_id, raw_content in documents:
        run_id = extract_document(
            conn, llm_client, document_id, raw_content, prompt_version,
            extractor_model_id, extractor_model_version,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM extraction_runs WHERE extraction_run_id = %s", (run_id,))
            status = cur.fetchone()[0]
        summary["documents_processed"] += 1
        if status == "failed":
            summary["documents_failed"] += 1

    with conn.cursor() as cur:
        cur.execute("SELECT catalyst_id FROM catalysts WHERE canonicalization_completed_at IS NULL")
        pending_catalysts = [str(row[0]) for row in cur.fetchall()]

    for catalyst_id in pending_catalysts:
        try:
            result = process_catalyst(conn, catalyst_id, prompt_version, extractor_model_id, extractor_model_version)
        except Exception:
            conn.rollback()
            log.exception("Failed processing catalyst %s -- rolled back, will retry next run", catalyst_id)
            continue
        if result.get("skipped"):
            continue
        summary["catalysts_canonicalized"] += 1
        for key in ("mentions_matched", "mentions_unresolved", "canonical_events_created",
                    "relationships_written", "candidates_eligible", "candidates_ineligible"):
            summary[key] += result.get(key, 0)

    summary["pending_unresolved_mentions_total"] = entity_resolution.count_pending_unresolved_mentions(conn)

    log.info(
        "Extraction pass complete: %d document(s) processed (%d failed), %d catalyst(s) canonicalized, "
        "%d canonical event(s) created, %d entity mention(s) matched / %d unresolved, "
        "%d relationship(s) written, %d candidate(s) eligible / %d ineligible, "
        "%d unresolved mention(s) pending review overall.",
        summary["documents_processed"], summary["documents_failed"], summary["catalysts_canonicalized"],
        summary["canonical_events_created"], summary["mentions_matched"], summary["mentions_unresolved"],
        summary["relationships_written"], summary["candidates_eligible"], summary["candidates_ineligible"],
        summary["pending_unresolved_mentions_total"],
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="claude-sonnet-5", help="extractor_model_id to record and use")
    parser.add_argument("--model-version", default="2026-01", help="extractor_model_version to record")
    args = parser.parse_args()

    from llm_client import AnthropicExtractionClient

    conn = psycopg2.connect(DB_DSN)
    try:
        llm_client = AnthropicExtractionClient(model=args.model)
        run_extraction_pass(conn, llm_client, PROMPT_VERSION, args.model, args.model_version)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
