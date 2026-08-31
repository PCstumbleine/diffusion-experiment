#!/usr/bin/env python3
"""
Manual resolution CLI for unresolved_entity_mentions -- Extraction-Runner
Design v2, §3/§3a. No UI needed at this scale (design doc §3: "a human
reviews the unresolved queue periodically -- weekly is enough").

Usage:
    python3 manual_resolve.py list
    python3 manual_resolve.py resolve --mention-id <uuid> --entity-id <uuid>
    python3 manual_resolve.py resolve --mention-id <uuid> --new-entity "Legal Name"

Resolving a mention applies the backfill rule from §3 EXACTLY: any
relationship that mention was part of is written NOW, with
system_observed_at set to the actual resolution timestamp -- never
backdated to the original filing/extraction time, even though
evidence_publicly_available_at (a fact about the document, not about this
pipeline) is unaffected. Per §3a, a manually-resolved entity does NOT need
to be one of the 108 watchlist companies -- --new-entity creates an entity
outside the polling watchlist (it is never added to watchlist_membership).
"""

from __future__ import annotations

import argparse
import logging
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

import entity_resolution
from extraction_runner import (
    DB_DSN, RELATIONSHIP_TYPE_SYNONYMS, generate_candidates_for_event_version,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("manual_resolve")


def list_pending(conn) -> None:
    mentions = entity_resolution.list_unresolved_mentions(conn)
    if not mentions:
        print("No pending unresolved mentions.")
        return
    for m in mentions:
        print(f"{m['mention_id']}  {m['raw_name']!r}  (first seen {m['first_seen_at']}, doc={m['document_id']})")
    print(f"\n{len(mentions)} pending (showing up to 100).")


def _write_backfilled_relationships(conn, mention: dict, newly_resolved_entity_id: str) -> int:
    """Scans the mention's own extraction run's CLEANED (validated) output
    for any relationship naming this mention's raw_name, and writes it now
    if the OTHER side is also resolvable. Returns the count written.

    Deliberately reads cleaned_llm_output, not raw_llm_output: since a
    code review split those two apart (extraction_runs now keeps the true
    raw provider response separately from the post-validation result),
    reading the raw one here would let a relationship whose evidence_span
    or relationship_type failed validation get backfilled anyway --
    resurrecting exactly the kind of claim validate_extraction_output
    exists to drop."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cleaned_llm_output FROM extraction_runs WHERE extraction_run_id = %s",
            (mention["extraction_run_id"],),
        )
        raw_output = cur.fetchone()[0]
    with conn.cursor() as cur:
        cur.execute("SELECT raw_content, canonical_first_public_at FROM raw_documents WHERE document_id = %s",
                    (mention["document_id"],))
        raw_content, canonical_first_public_at = cur.fetchone()

    resolution_index = entity_resolution.build_resolution_index(conn)
    written = 0
    resolution_time = datetime.now(timezone.utc)

    for event in raw_output.get("events", []):
        for rel in event.get("relationships", []):
            a_matches = entity_resolution.normalize_entity_name(rel["entity_a"]) == mention["normalized_name"]
            b_matches = entity_resolution.normalize_entity_name(rel["entity_b"]) == mention["normalized_name"]
            if not (a_matches or b_matches):
                continue

            entity_id_a = newly_resolved_entity_id if a_matches else entity_resolution.resolve_entity_name(
                conn, rel["entity_a"], mention["document_id"], mention["extraction_run_id"], index=resolution_index,
            )
            entity_id_b = newly_resolved_entity_id if b_matches else entity_resolution.resolve_entity_name(
                conn, rel["entity_b"], mention["document_id"], mention["extraction_run_id"], index=resolution_index,
            )
            if entity_id_a is None or entity_id_b is None or entity_id_a == entity_id_b:
                continue

            mapped_type = RELATIONSHIP_TYPE_SYNONYMS.get(str(rel.get("relationship_type", "")).strip().lower())
            if mapped_type is None or rel.get("evidence_span") not in raw_content:
                continue

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO entity_relationships
                        (entity_id_a, entity_id_b, relationship_type, source_authority,
                         relationship_evidence, shock_transmission_evidence,
                         raw_llm_relationship_score, evidence_publicly_available_at,
                         system_observed_at, source_document_id, extraction_run_id)
                    VALUES (%s, %s, %s, %s, %s, 'new_or_unobserved', %s, %s, %s, %s, %s)
                    ON CONFLICT (extraction_run_id, entity_id_a, entity_id_b, relationship_type) DO NOTHING
                    """,
                    (entity_id_a, entity_id_b, mapped_type, rel["source_authority"],
                     rel["relationship_evidence"], rel.get("raw_llm_relationship_score"),
                     canonical_first_public_at or resolution_time,
                     # Backfill rule (§3): system_observed_at is the ACTUAL
                     # resolution timestamp, never backdated to the original
                     # extraction time.
                     resolution_time, mention["document_id"], mention["extraction_run_id"]),
                )
                if cur.rowcount:
                    written += 1

    return written


def _rerun_candidate_generation_for_document(conn, document_id: str) -> None:
    """After a late resolution, re-run candidate generation for every
    event_version under this document's catalyst using its ALREADY-FROZEN
    decision_at (never recomputed) -- a newly-backfilled relationship will
    correctly come out ineligible (system_observed_at > decision_at) per
    §5a's timing rule, but recording that is the point (§5: "never
    dropped, gives real empirical data")."""
    with conn.cursor() as cur:
        cur.execute("SELECT catalyst_id FROM catalyst_documents WHERE document_id = %s", (document_id,))
        row = cur.fetchone()
        if row is None:
            return
        catalyst_id = row[0]
        cur.execute("SELECT issuer_entity_id FROM catalysts WHERE catalyst_id = %s", (catalyst_id,))
        issuer_entity_id = cur.fetchone()[0]
        cur.execute(
            "SELECT ev.event_version_id, ev.decision_at FROM event_versions ev "
            "JOIN canonical_events ce ON ce.canonical_event_id = ev.canonical_event_id "
            "WHERE ce.catalyst_id = %s AND ev.decision_at IS NOT NULL",
            (catalyst_id,),
        )
        event_versions = cur.fetchall()

    for event_version_id, decision_at in event_versions:
        generate_candidates_for_event_version(conn, event_version_id, issuer_entity_id, decision_at)


def resolve_mention(conn, mention_id: str, entity_id: str) -> None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM unresolved_entity_mentions WHERE mention_id = %s", (mention_id,))
        mention = cur.fetchone()
    if mention is None:
        raise ValueError(f"No such mention_id: {mention_id}")
    if mention["status"] == "resolved":
        raise ValueError(f"Mention {mention_id} is already resolved (to entity {mention['resolved_entity_id']})")

    written = _write_backfilled_relationships(conn, mention, entity_id)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unresolved_entity_mentions SET status = 'resolved', resolved_entity_id = %s, resolved_at = now() "
            "WHERE mention_id = %s",
            (entity_id, mention_id),
        )

    _rerun_candidate_generation_for_document(conn, mention["document_id"])
    conn.commit()
    log.info("Resolved mention %s (%r) -> entity %s; %d relationship(s) backfilled.",
              mention_id, mention["raw_name"], entity_id, written)


def create_entity_and_resolve(conn, mention_id: str, legal_name: str) -> str:
    """§3a: a manually-resolved entity outside the 108-company watchlist is
    added to `entities` but deliberately NEVER to `watchlist_membership` --
    it becomes a valid candidate-graph member without becoming a polled
    filer."""
    entity_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (entity_id, legal_name, entity_status) VALUES (%s, %s, 'active')",
            (entity_id, legal_name),
        )
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias_text, normalized_alias, alias_source) "
            "VALUES (%s, %s, %s, 'manual_resolution')",
            (entity_id, legal_name, entity_resolution.normalize_entity_name(legal_name)),
        )
    resolve_mention(conn, mention_id, entity_id)
    return entity_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DB_DSN)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("--mention-id", required=True)
    group = resolve_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--entity-id", help="resolve to this existing entity_id")
    group.add_argument("--new-entity", help="create a new entity with this legal name and resolve to it")
    args = parser.parse_args()

    conn = psycopg2.connect(args.dsn)
    try:
        if args.command == "list":
            list_pending(conn)
        elif args.command == "resolve":
            if args.new_entity:
                create_entity_and_resolve(conn, args.mention_id, args.new_entity)
            else:
                resolve_mention(conn, args.mention_id, args.entity_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
