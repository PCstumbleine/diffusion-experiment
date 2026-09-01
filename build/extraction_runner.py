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
  extraction_runs state (success or failed). It resolves entities and
  writes every relationship for EVERY event in the catalyst FIRST, only
  THEN freezes decision_at, and only THEN generates candidates for every
  event version -- so an earlier event's candidate pool can see a
  relationship a later event in the same catalyst discovers (fix from a
  pre-dry-run code review; see "Code-review fixes" below). Committed
  atomically at the end (design doc §2c: "All writes for one successful
  catalyst-level processing batch ... commit atomically").

Idempotency and concurrency safety (both extraction_runs and
catalyst_processing_runs use the SAME claim-then-update pattern -- see
"Code-review fixes" below): a document or catalyst is CLAIMED as 'pending'
in its own immediately-committed transaction before any expensive work
(an LLM call, or a whole catalyst batch) starts. Only the caller that
successfully claims it proceeds; a 'pending' claim older than
STALE_PENDING_MINUTES is eligible for reclaiming (recovery from a crashed
worker), and a 'failed' claim is always reclaimable (retry). This is what
makes two overlapping runner invocations -- or two processes -- safe to
run concurrently over the same documents/catalysts.

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
linkage; it does not yet drive event_versions.version_number. Deliberately
left as-is by the pre-dry-run review (out of scope for this fix round).

Code-review fixes (before the first live dry run), all against real bugs
independently traced against this code, not just claims -- migration 003
carries the schema side of these:

  1/2. extraction_runs used to be selected-then-inserted as two separate
       statements, with the LLM call in between: a retry after a 'failed'
       row hit the identity UNIQUE constraint on the second INSERT (a
       guaranteed uniqueness violation, not a rare edge case), and two
       concurrent callers could both pass the "no successful row yet"
       check and both pay for the same LLM call. Fixed: claim_extraction_
       run() atomically claims (INSERT) or reclaims (UPDATE, if 'failed'
       or stale-'pending') the ONE row for this identity, committing
       immediately; extract_document() only calls the LLM if it actually
       won the claim.
  3.   process_catalyst's old idempotency flag (catalysts.
       canonicalization_completed_at, one column) had no awareness of
       WHICH prompt/model config produced it -- reprocessing under a new
       prompt or model version would see it already set and silently
       no-op, contradicting the design doc's own "reprocessing creates
       new downstream data" requirement. Fixed: claim_catalyst_processing()
       against the new catalyst_processing_runs table, keyed by
       (catalyst, prompt version, model config) -- the same idea as
       extraction_runs one level up, closing the same claim-before-work
       race for whole catalyst batches.
  4.   decision_at used to be captured once at the very top of
       process_catalyst, before any relationship had been written, and
       candidates were generated for each event inside the same loop that
       was still writing later events' relationships -- an earlier event
       in a multi-event catalyst literally could not see a relationship a
       later event in the same catalyst discovered. Fixed: _do_process_
       catalyst() now writes every relationship for every event in the
       catalyst FIRST, only THEN captures decision_at, only THEN generates
       candidates for every event version -- see test_extraction_runner.py's
       test_earlier_event_sees_relationship_discovered_by_later_event_in_
       same_catalyst.
  5.   The real LLM call was constrained only by a text instruction, never
       actually enforced -- see llm_client.py's tool-use rewrite. Here:
       the raw provider response is now ALSO validated against the true
       JSON Schema (jsonschema.validate against EXTRACTION_JSON_SCHEMA,
       loaded verbatim from extraction_prompt_v1.md) before the existing
       hand-rolled per-claim checks run as a second pass.
  6.   validate_extraction_output() now requires the LLM's own echoed
       document_id to equal the document actually sent (previously only
       checked presence, not correctness -- the test helper that hid this
       gap, llm_output(), no longer hardcodes a placeholder). extraction_
       runs.raw_llm_output is now the TRUE raw provider response;
       cleaned_llm_output and validation_drop_log hold the post-validation
       result and what was dropped and why (migration 003).
       extracted_events now carries its own extraction_run_id FK so the
       full audit trail (raw response + cleaned version + drop log) is
       reachable from an extracted_events row, not just its own single
       event object.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from datetime import datetime, timezone

import jsonschema
import psycopg2
import psycopg2.extras

import entity_resolution
from llm_client import PROMPT_VERSION, load_prompt_texts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extraction_runner")

DB_DSN = "dbname=diffusion_experiment user=postgres"

_, _, EXTRACTION_JSON_SCHEMA = load_prompt_texts()

# A 'pending' claim (extraction_runs or catalyst_processing_runs) older
# than this is assumed to belong to a crashed/killed worker and is
# reclaimable -- otherwise a dead worker would permanently block that
# document/catalyst from ever being retried.
STALE_PENDING_MINUTES = 30

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
    wrong prompt version, document_id mismatch, missing required top-level
    fields, or a real JSON Schema violation) -- terminal 'failed' state for
    that document's extraction_runs row. Distinct from a per-claim drop
    (bad evidence_span, unmapped relationship_type), which is logged and
    dropped without failing the whole document."""


# ---------------------------------------------------------------------------
# Phase 1: per-document extraction
# ---------------------------------------------------------------------------

def select_unprocessed_documents(conn, extraction_prompt_version: str, extractor_model_id: str,
                                  extractor_model_version: str) -> list[tuple[str, str]]:
    """Documents with no SUCCESSFUL extraction_runs row yet at this exact
    (prompt version, model) identity. A 'failed' or stale-'pending' row is
    still selected here -- claim_extraction_run() (used by
    extract_document) is what actually decides whether this caller gets to
    retry it."""
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


def claim_extraction_run(conn, document_id: str, prompt_version: str, extractor_model_id: str,
                          extractor_model_version: str) -> tuple[str, bool]:
    """Atomically claims (or creates) the ONE extraction_runs row for this
    (document, prompt version, model) identity as 'pending', committing
    immediately so the claim is durable and visible before the caller does
    any expensive work. Returns (extraction_run_id, claimed):

      claimed=True  -- this caller now owns the row; proceed to call the LLM.
      claimed=False -- either already 'success' (done), or 'pending' and
                       NOT stale (another caller currently owns it); the
                       caller must not call the LLM.

    A prior version did this as a separate SELECT-then-INSERT, with the
    LLM call in between -- a retry of a 'failed' row hit the identity
    UNIQUE constraint on the second INSERT (guaranteed, not rare), and two
    concurrent callers could both pass the SELECT check and both pay for
    the LLM call. This single statement is race-safe: Postgres serializes
    concurrent INSERT ... ON CONFLICT attempts on the same conflicting row,
    and the DO UPDATE's WHERE clause is evaluated against the
    already-committed row, so at most one caller's statement satisfies it."""
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extraction_runs
                (extraction_run_id, document_id, extraction_prompt_version,
                 extractor_model_id, extractor_model_version, status, attempt_count, started_at)
            VALUES (%s, %s, %s, %s, %s, 'pending', 1, now())
            ON CONFLICT (document_id, extraction_prompt_version, extractor_model_id, extractor_model_version)
            DO UPDATE SET
                status = 'pending',
                attempt_count = extraction_runs.attempt_count + 1,
                started_at = now(),
                error = NULL,
                raw_llm_output = NULL,
                cleaned_llm_output = NULL,
                validation_drop_log = '[]'::jsonb,
                completed_at = NULL
            WHERE extraction_runs.status = 'failed'
               OR (extraction_runs.status = 'pending'
                   AND extraction_runs.started_at < now() - (%s || ' minutes')::interval)
            RETURNING extraction_run_id
            """,
            (run_id, document_id, prompt_version, extractor_model_id, extractor_model_version,
             str(STALE_PENDING_MINUTES)),
        )
        claimed_row = cur.fetchone()
    conn.commit()
    if claimed_row is not None:
        return str(claimed_row[0]), True

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT extraction_run_id FROM extraction_runs
            WHERE document_id = %s AND extraction_prompt_version = %s
              AND extractor_model_id = %s AND extractor_model_version = %s
            """,
            (document_id, prompt_version, extractor_model_id, extractor_model_version),
        )
        existing_id = cur.fetchone()[0]
    return str(existing_id), False


def validate_extraction_output(output: dict, raw_content: str, requested_prompt_version: str,
                                requested_document_id: str) -> tuple[dict, list[str]]:
    """Validates and cleans one document's raw LLM output. Returns
    (cleaned_output, drop_log) -- drop_log entries describe individual
    claims dropped (bad evidence_span, unmapped relationship_type,
    malformed event), never silently discarded without a trace.

    Raises ExtractionValidationError for a document-level failure: output
    isn't a dict, missing required top-level keys, a document_id that
    doesn't match the document actually sent, or a prompt-version mismatch
    (design doc §2b). An individual malformed EVENT inside an otherwise-
    valid document is dropped and logged, not treated as a whole-document
    failure -- matching the extraction prompt's own per-claim "if you
    cannot point to a span, do not extract" philosophy rather than an
    all-or-nothing document gate. This split is a judgment call the design
    doc's §2b/§6 don't fully specify; flagged as such.

    Called AFTER the caller has already run jsonschema.validate() against
    the real extraction_prompt_v1.md schema (a code-review fix -- see
    module docstring) -- these hand-rolled checks are a second,
    per-claim-granular pass on top of that, not a replacement for it."""
    drop_log: list[str] = []

    if not isinstance(output, dict):
        raise ExtractionValidationError(f"LLM output is not a JSON object: {type(output)!r}")
    for required_key in ("document_id", "extraction_prompt_version", "events"):
        if required_key not in output:
            raise ExtractionValidationError(f"LLM output missing required field {required_key!r}")
    if output["document_id"] != requested_document_id:
        raise ExtractionValidationError(
            f"LLM output document_id={output['document_id']!r} does not match the document actually "
            f"sent ({requested_document_id!r}) -- refusing to attribute this output to the wrong document."
        )
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
    """Runs (or reuses) one document's extraction via the claim-then-update
    state machine (see claim_extraction_run). Always returns an
    extraction_run_id; if this call didn't win the claim, the LLM is never
    called and the existing row is returned untouched."""
    run_id, claimed = claim_extraction_run(
        conn, document_id, prompt_version, extractor_model_id, extractor_model_version,
    )
    if not claimed:
        return run_id

    try:
        raw_provider_output = llm_client.extract(document_id, raw_content, prompt_version)
        try:
            jsonschema.validate(instance=raw_provider_output, schema=EXTRACTION_JSON_SCHEMA)
        except jsonschema.exceptions.ValidationError as exc:
            raise ExtractionValidationError(f"LLM output failed JSON Schema validation: {exc.message}") from exc
        cleaned_output, drop_log = validate_extraction_output(
            raw_provider_output, raw_content, prompt_version, document_id,
        )
        for msg in drop_log:
            log.warning("Document %s: %s", document_id, msg)
    except Exception as exc:
        log.exception("Extraction failed for document %s", document_id)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE extraction_runs SET status = 'failed', error = %s, completed_at = now() "
                "WHERE extraction_run_id = %s",
                (str(exc), run_id),
            )
        conn.commit()
        return run_id

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extraction_runs
            SET status = 'success', raw_llm_output = %s, cleaned_llm_output = %s,
                validation_drop_log = %s, completed_at = now()
            WHERE extraction_run_id = %s
            """,
            (psycopg2.extras.Json(raw_provider_output), psycopg2.extras.Json(cleaned_output),
             psycopg2.extras.Json(drop_log), run_id),
        )
    # extracted_events references event_versions, which doesn't exist until
    # catalyst-level canonicalization (§2a/§2b) -- so the validated,
    # per-document extraction is staged here as extraction_runs.
    # cleaned_llm_output only; extracted_events / canonical_events /
    # event_versions are written in Phase 2 (process_catalyst), once an
    # event_version_id actually exists.
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
    (document_id, document_role, status, cleaned_llm_output) tuples."""
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
                SELECT status, cleaned_llm_output FROM extraction_runs
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


def claim_catalyst_processing(conn, catalyst_id: str, prompt_version: str, extractor_model_id: str,
                               extractor_model_version: str) -> bool:
    """Same claim-then-update pattern as claim_extraction_run, one level
    up: atomically claims this (catalyst, prompt version, model) identity
    as 'pending' in catalyst_processing_runs, committing immediately.
    Returns True if this caller now owns it (must proceed to process the
    batch), False if it's already 'success' or 'pending'-and-not-stale
    (another caller owns it -- or already completed under this exact
    config; reprocessing under a DIFFERENT prompt/model config is a
    separate row and is allowed, per the design doc's own requirement)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO catalyst_processing_runs
                (catalyst_id, extraction_prompt_version, extractor_model_id, extractor_model_version,
                 status, started_at)
            VALUES (%s, %s, %s, %s, 'pending', now())
            ON CONFLICT (catalyst_id, extraction_prompt_version, extractor_model_id, extractor_model_version)
            DO UPDATE SET status = 'pending', started_at = now(), error = NULL, completed_at = NULL
            WHERE catalyst_processing_runs.status = 'failed'
               OR (catalyst_processing_runs.status = 'pending'
                   AND catalyst_processing_runs.started_at < now() - (%s || ' minutes')::interval)
            RETURNING catalyst_id
            """,
            (catalyst_id, prompt_version, extractor_model_id, extractor_model_version,
             str(STALE_PENDING_MINUTES)),
        )
        claimed = cur.fetchone() is not None
    conn.commit()
    return claimed


def _mark_catalyst_processing(conn, catalyst_id: str, prompt_version: str, extractor_model_id: str,
                               extractor_model_version: str, status: str, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE catalyst_processing_runs SET status = %s, error = %s, completed_at = now()
            WHERE catalyst_id = %s AND extraction_prompt_version = %s
              AND extractor_model_id = %s AND extractor_model_version = %s
            """,
            (status, error, catalyst_id, prompt_version, extractor_model_id, extractor_model_version),
        )
    conn.commit()


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
    entity_id) constraint (ON CONFLICT DO NOTHING). Must be called only
    after EVERY relationship for the whole catalyst has been written (see
    _do_process_catalyst) -- otherwise a relationship discovered by a
    later event in the same catalyst would be invisible to an earlier
    event's candidate pool (a real completeness bug fixed before the first
    live dry run)."""
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


def _do_process_catalyst(conn, catalyst_id: str, docs: list, prompt_version: str,
                          extractor_model_id: str, extractor_model_version: str) -> dict:
    """The actual batch (claimed by process_catalyst before this is
    called). Two passes, per the decision_at-ordering fix: pass 1 resolves
    entities and writes canonical_events/event_versions/extracted_events/
    event_document_links/event_entities/entity_relationships for EVERY
    event in the catalyst; only after ALL of that is done does pass 2
    freeze decision_at and generate candidates for every event_version."""
    with conn.cursor() as cur:
        cur.execute("SELECT issuer_entity_id FROM catalysts WHERE catalyst_id = %s", (catalyst_id,))
        issuer_entity_id = cur.fetchone()[0]

    # Captured once, before any resolution/relationship-writing -- used as
    # entity_relationships.system_observed_at (and the conservative
    # fallback for evidence_publicly_available_at) for everything written
    # in this batch. decision_at (below) is captured LATER, strictly after
    # every relationship in the catalyst has been written, so
    # system_observed_at <= decision_at holds for all of them by
    # construction, not by coincidence.
    system_observed_at = datetime.now(timezone.utc)
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

    doc_run_ids: dict[str, str] = {}
    for document_id, _role, status, _cleaned in docs:
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
            "SELECT document_id, raw_content, canonical_first_public_at FROM raw_documents "
            "WHERE document_id::text = ANY(%s)",
            (list(doc_run_ids.keys()),),
        )
        doc_meta = {str(doc_id): (content, public_at) for doc_id, content, public_at in cur.fetchall()}
    raw_content_by_doc = {doc_id: meta[0] for doc_id, meta in doc_meta.items()}

    groups: dict[tuple, list[tuple[str, dict, list[tuple[str, str]]]]] = {}
    primary_document_id = next((doc_id for doc_id, role, _s, _c in docs if role == "primary"), None)

    for document_id, _role, status, cleaned_output in docs:
        if status != "success" or cleaned_output is None:
            continue
        extraction_run_id = doc_run_ids[document_id]
        for event in cleaned_output.get("events", []):
            resolved_entities = []
            for ent in event.get("entities", []):
                entity_id = resolve(ent["entity_name"], document_id, extraction_run_id)
                if entity_id is not None:
                    resolved_entities.append((entity_id, ent["role"]))
            role_set = frozenset(resolved_entities)
            fingerprint = _event_fingerprint(event, role_set)
            groups.setdefault(fingerprint, []).append((document_id, event, resolved_entities))

    # ---- Pass 1: canonicalize every event and write every relationship ----
    event_version_ids: list[str] = []

    for fingerprint, members in groups.items():
        event_category = fingerprint[0]
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO canonical_events (catalyst_id, event_category) VALUES (%s, %s) "
                "RETURNING canonical_event_id",
                (catalyst_id, event_category),
            )
            canonical_event_id = cur.fetchone()[0]

            # Known gap (see module docstring): version_number is always 1.
            # decision_at is left NULL here -- frozen in pass 2, below,
            # only after every relationship in the WHOLE catalyst exists.
            cur.execute(
                "INSERT INTO event_versions (canonical_event_id, version_number) VALUES (%s, 1) "
                "RETURNING event_version_id",
                (canonical_event_id,),
            )
            event_version_id = cur.fetchone()[0]
        counts["canonical_events_created"] += 1
        event_version_ids.append(str(event_version_id))

        all_resolved_roles: set[tuple[str, str]] = set()
        for document_id, event, resolved_entities in members:
            all_resolved_roles.update(resolved_entities)
            extraction_run_id = doc_run_ids[document_id]

            surprise = event.get("surprise") or {}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extracted_events
                        (event_version_id, extraction_run_id, surprise_type, observed_value, reference_value,
                         reference_source, reference_timestamp, unit, period,
                         extraction_prompt_version, raw_llm_output)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (event_version_id, extraction_run_id, surprise.get("surprise_type"),
                     surprise.get("observed_value"), surprise.get("reference_value"),
                     surprise.get("reference_source"), surprise.get("reference_timestamp"),
                     surprise.get("unit"), surprise.get("period"),
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

            for rel in event.get("relationships", []):
                entity_id_a = resolve(rel["entity_a"], document_id, extraction_run_id)
                entity_id_b = resolve(rel["entity_b"], document_id, extraction_run_id)
                if entity_id_a is None or entity_id_b is None or entity_id_a == entity_id_b:
                    continue
                _content, canonical_public_at = doc_meta.get(document_id, (None, None))
                public_at = canonical_public_at or system_observed_at
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
                         public_at, system_observed_at, document_id, extraction_run_id),
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

    # ---- Pass 2: freeze decision_at, THEN generate candidates ----
    # By this point every relationship for every event in the WHOLE
    # catalyst exists -- an earlier event's candidate pool now correctly
    # sees a relationship a later event discovered (the fix).
    decision_at = datetime.now(timezone.utc)
    if event_version_ids:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE event_versions SET decision_at = %s WHERE event_version_id = ANY(%s::uuid[])",
                (decision_at, event_version_ids),
            )

    for event_version_id in event_version_ids:
        candidate_counts = generate_candidates_for_event_version(
            conn, event_version_id, issuer_entity_id, decision_at,
        )
        counts["candidates_eligible"] += candidate_counts["eligible"]
        counts["candidates_ineligible"] += candidate_counts["ineligible"]

    return counts


def process_catalyst(conn, catalyst_id: str, prompt_version: str, extractor_model_id: str,
                      extractor_model_version: str) -> dict:
    """Phase 2 entry point. Returns a counts dict; returns
    {'skipped': 'not_ready'} if some document in the catalyst hasn't
    reached a terminal extraction state yet, or
    {'skipped': 'already_done_or_in_progress'} if this exact
    (catalyst, prompt version, model) identity is already 'success', or
    'pending' and not stale (another caller owns it)."""
    docs = _get_catalyst_documents_with_terminal_runs(
        conn, catalyst_id, prompt_version, extractor_model_id, extractor_model_version,
    )
    if docs is None:
        return {"skipped": "not_ready"}

    if not claim_catalyst_processing(conn, catalyst_id, prompt_version, extractor_model_id, extractor_model_version):
        return {"skipped": "already_done_or_in_progress"}

    try:
        result = _do_process_catalyst(
            conn, catalyst_id, docs, prompt_version, extractor_model_id, extractor_model_version,
        )
    except Exception as exc:
        # Roll back ONLY the partial batch -- the claim above was already
        # committed in its own transaction, so this rollback can't discard
        # it. Then mark the claim 'failed' (reclaimable on retry) in a
        # fresh, immediately-committed statement, and re-raise so the
        # caller still sees the original failure.
        conn.rollback()
        _mark_catalyst_processing(
            conn, catalyst_id, prompt_version, extractor_model_id, extractor_model_version,
            status="failed", error=str(exc),
        )
        raise

    _mark_catalyst_processing(
        conn, catalyst_id, prompt_version, extractor_model_id, extractor_model_version, status="success",
    )
    conn.commit()
    return result


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
        cur.execute(
            """
            SELECT c.catalyst_id FROM catalysts c
            WHERE NOT EXISTS (
                SELECT 1 FROM catalyst_processing_runs cpr
                WHERE cpr.catalyst_id = c.catalyst_id
                  AND cpr.extraction_prompt_version = %s
                  AND cpr.extractor_model_id = %s
                  AND cpr.extractor_model_version = %s
                  AND cpr.status = 'success'
            )
            """,
            (prompt_version, extractor_model_id, extractor_model_version),
        )
        pending_catalysts = [str(row[0]) for row in cur.fetchall()]

    for catalyst_id in pending_catalysts:
        try:
            result = process_catalyst(conn, catalyst_id, prompt_version, extractor_model_id, extractor_model_version)
        except Exception:
            log.exception("Failed processing catalyst %s -- will retry next run", catalyst_id)
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
    parser.add_argument("--dsn", default=DB_DSN,
                         help="Postgres connection string (default: %(default)r).")
    parser.add_argument("--extractions-dir", default=None,
                         help="If given, use FileBackedExtractionClient reading pre-saved extraction "
                              "JSON files from this directory instead of calling a real LLM API "
                              "(e.g. for a no-cost dry run) -- see llm_client.py.")
    args = parser.parse_args()

    conn = psycopg2.connect(args.dsn)
    try:
        if args.extractions_dir:
            from llm_client import FileBackedExtractionClient
            llm_client = FileBackedExtractionClient(args.extractions_dir)
        else:
            from llm_client import AnthropicExtractionClient
            llm_client = AnthropicExtractionClient(model=args.model)
        run_extraction_pass(conn, llm_client, PROMPT_VERSION, args.model, args.model_version)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
