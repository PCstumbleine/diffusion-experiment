"""
Extraction-Runner Design v2, §3/§6: normalization, exact/alias resolution,
and -- the specific assertion the design doc's testing strategy calls out
by name -- an unresolved mention never creates a row in `entities`.
"""
from conftest import make_entity, make_raw_document, make_extraction_run

from entity_resolution import (
    normalize_entity_name, strip_sec_filing_index_noise, resolve_entity_name,
    log_unresolved_mention, list_unresolved_mentions, count_pending_unresolved_mentions,
)


def test_normalize_entity_name_strips_suffix_and_punctuation():
    assert normalize_entity_name("NVIDIA Corporation") == "nvidia"
    assert normalize_entity_name("Marvell Technology, Inc.") == "marvell technology"
    assert normalize_entity_name("Eaton Corp plc") == "eaton"
    assert normalize_entity_name("JPMORGAN CHASE & CO") == "jpmorgan chase"


def test_normalize_entity_name_does_not_strip_suffix_from_the_middle():
    # "Corp" appears mid-name here, not as a trailing corporate suffix --
    # must not be stripped, since suffix-stripping is only ever applied to
    # the END of the name (module docstring).
    assert "corp" in normalize_entity_name("Corp Travel Services Inc")


def test_strip_sec_filing_index_noise_removes_trailing_state_tag():
    assert strip_sec_filing_index_noise("MASCO CORP /DE/") == "MASCO CORP"
    assert strip_sec_filing_index_noise("PULTEGROUP INC/MI/") == "PULTEGROUP INC"
    assert strip_sec_filing_index_noise("CATERPILLAR INC") == "CATERPILLAR INC"  # no tag, unchanged


def test_resolve_entity_name_exact_legal_name_match(conn):
    entity_id = make_entity(conn, "Acme Corporation")
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)

    resolved = resolve_entity_name(conn, "Acme Corporation", document_id, run_id)
    assert resolved == entity_id

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM unresolved_entity_mentions")
        assert cur.fetchone()[0] == 0


def test_resolve_entity_name_alias_match(conn):
    entity_id = make_entity(conn, "Acme Corporation")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias_text, normalized_alias, alias_source) "
            "VALUES (%s, 'Acme Co (formerly Widgets Inc)', 'widgets', 'manual_resolution')",
            (entity_id,),
        )
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)

    resolved = resolve_entity_name(conn, "Widgets Inc", document_id, run_id)
    assert resolved == entity_id


def test_resolve_entity_name_no_match_logs_and_never_creates_an_entity(conn):
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities")
        entities_before = cur.fetchone()[0]

    resolved = resolve_entity_name(conn, "Totally Unknown Company LLC", document_id, run_id)
    assert resolved is None

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities")
        entities_after = cur.fetchone()[0]
    # The explicit assertion the design doc's §6 calls for: no-match never
    # creates a row in `entities`.
    assert entities_after == entities_before

    with conn.cursor() as cur:
        cur.execute(
            "SELECT raw_name, normalized_name, status FROM unresolved_entity_mentions WHERE document_id = %s",
            (document_id,),
        )
        row = cur.fetchone()
    # "Company" and "LLC" are both stripped as trailing corporate-suffix
    # words (iteratively, per normalize_entity_name's own documented
    # behavior) -- not a special case for this test.
    assert row == ("Totally Unknown Company LLC", "totally unknown", "unresolved")


def test_resolve_entity_name_ambiguous_alias_is_treated_as_unresolved(conn):
    """Two different entities sharing a normalized alias -- deliberately
    allowed by the schema (entity_aliases has no global-uniqueness
    constraint on normalized_alias) so the resolver can DETECT this rather
    than the schema silently forbidding it. Must not guess which one."""
    entity_a = make_entity(conn, "Ambiguous Co A")
    entity_b = make_entity(conn, "Ambiguous Co B")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias_text, normalized_alias, alias_source) "
            "VALUES (%s, 'Shared Name', 'shared name', 'manual_resolution')",
            (entity_a,),
        )
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias_text, normalized_alias, alias_source) "
            "VALUES (%s, 'Shared Name', 'shared name', 'manual_resolution')",
            (entity_b,),
        )
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)

    resolved = resolve_entity_name(conn, "Shared Name", document_id, run_id)
    assert resolved is None
    assert count_pending_unresolved_mentions(conn) == 1


def test_list_unresolved_mentions_and_count(conn):
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)
    log_unresolved_mention(conn, "Foo Inc", "foo", document_id, run_id)
    log_unresolved_mention(conn, "Bar Inc", "bar", document_id, run_id)

    assert count_pending_unresolved_mentions(conn) == 2
    mentions = list_unresolved_mentions(conn)
    assert {m["raw_name"] for m in mentions} == {"Foo Inc", "Bar Inc"}
