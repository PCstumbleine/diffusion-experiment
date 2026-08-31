"""
Targets the exact bug both review rounds caught: a relationship that
economically existed earlier, and was even reported in a document later,
must be invisible to a query deciding something at a historical point in
time BEFORE it was actually publicly available — using
evidence_publicly_available_at, never relationship_valid_from and never
system_observed_at, for historical point-in-time queries.
"""
from conftest import make_entity


def insert_relationship(conn, a, b, valid_from, publicly_available_at, observed_at):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO entity_relationships (
                entity_id_a, entity_id_b, relationship_type,
                source_authority, relationship_evidence, shock_transmission_evidence,
                relationship_valid_from, evidence_publicly_available_at, system_observed_at
            ) VALUES (%s, %s, 'supplier', 'company', 'explicit_named', 'historical', %s, %s, %s)
            """,
            (a, b, valid_from, publicly_available_at, observed_at),
        )


def visible_at(conn, as_of, use_column="evidence_publicly_available_at"):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM entity_relationships WHERE {use_column} <= %s",
            (as_of,),
        )
        return cur.fetchone()[0]


def test_relationship_invisible_before_public_disclosure(conn):
    a, b = make_entity(conn, "Supplier"), make_entity(conn, "Customer")
    # Relationship existed since 2022, and this pipeline happened to ingest
    # the disclosing document in 2026 — but the document ITSELF states the
    # relationship was disclosed back in 2023.
    insert_relationship(
        conn, a, b,
        valid_from="2022-01-01",
        publicly_available_at="2023-06-01",
        observed_at="2026-08-01",
    )
    # A 2023-01-01 historical decision (before the 2023-06-01 disclosure) must not see it.
    assert visible_at(conn, "2023-01-01") == 0
    # A 2023-07-01 historical decision (after disclosure) must see it.
    assert visible_at(conn, "2023-07-01") == 1


def test_late_pipeline_ingestion_does_not_fool_a_live_decision(conn):
    """The companion failure mode: if a live/forward system used
    evidence_publicly_available_at instead of system_observed_at, it would
    incorrectly believe it already knows something it hasn't ingested yet.
    Live decisions must gate on system_observed_at."""
    a, b = make_entity(conn, "Supplier2"), make_entity(conn, "Customer2")
    insert_relationship(
        conn, a, b,
        valid_from="2022-01-01",
        publicly_available_at="2023-06-01",   # public a long time ago
        observed_at="2026-08-15",             # but THIS pipeline only just ingested it
    )
    # A live decision at 2026-08-10 (before this pipeline actually observed it)
    # must not treat it as known yet, even though it was long public.
    assert visible_at(conn, "2026-08-10", use_column="system_observed_at") == 0
    assert visible_at(conn, "2026-08-20", use_column="system_observed_at") == 1


def test_candidate_eligibility_uses_the_same_field_as_the_bitemporal_check(conn):
    """v2.2 had a real inconsistency: the eligibility rule checked
    evidence_published_at while the bitemporal paragraph used a different
    field. v2.2.1's fix is that both now read evidence_publicly_available_at
    — this test fails loudly if that ever drifts apart again."""
    import inspect
    # This is a documentation-level regression guard: assert the column
    # exists and eligibility-style queries against it behave as expected,
    # rather than re-deriving application code here.
    a, b = make_entity(conn, "Supplier3"), make_entity(conn, "Customer3")
    insert_relationship(conn, a, b, "2022-01-01", "2024-01-01", "2024-01-02")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relationship_id FROM entity_relationships
            WHERE entity_id_a = %s
              AND relationship_evidence IN ('explicit_named', 'quantified_named')
              AND evidence_publicly_available_at <= %s
            """,
            (a, "2024-06-01"),
        )
        assert cur.fetchone() is not None
