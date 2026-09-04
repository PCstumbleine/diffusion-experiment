"""
seed_entities.py's "&"/"and" alias fix (code review, post-Dry-Run-001):
Dry Run 001's single load-bearing finding was that Eli Lilly's own press
release names itself "Eli Lilly and Company", which does not normalize
the same way as its seeded SEC title "ELI LILLY & Co" -- every one of
Lilly's 9 self-references failed to resolve. Fixed with an additional
seeded alias per "&"-bearing legal name (the "&" spelled out as "and"),
NOT a change to normalize_entity_name() or _CORPORATE_SUFFIXES -- see
seed_entities.py's own comment for why a global "&"->"and" canonicalization
would be worse (it would silently change "Johnson & Johnson"'s already-
working normalized form, among others).
"""
import pytest

from conftest import make_entity, make_raw_document, make_extraction_run

from seed_entities import seed_all, insert_curated_alias
from entity_resolution import resolve_entity_name

# (CIK, SEC-seeded legal name, real-filing spelled-out form) -- all 4
# confirmed affected in the current 108-company watchlist.
AMPERSAND_CASES = [
    ("0000315189", "DEERE & CO", "Deere and Company"),
    ("0000059478", "ELI LILLY & Co", "Eli Lilly and Company"),
    ("0000310158", "Merck & Co., Inc.", "Merck and Company"),
    ("0000019617", "JPMORGAN CHASE & CO", "JPMorgan Chase and Co."),
]


def test_ampersand_and_spelled_out_forms_resolve_to_the_same_entity(conn):
    seed_all(conn)  # the real, checked-in 108-company CSV -- no network calls
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)

    for cik, seeded_legal_name, real_filing_form in AMPERSAND_CASES:
        with conn.cursor() as cur:
            cur.execute("SELECT entity_id FROM entities WHERE cik = %s", (cik,))
            row = cur.fetchone()
        assert row is not None, f"CIK {cik} not seeded"
        expected_entity_id = str(row[0])

        ampersand_resolved = resolve_entity_name(conn, seeded_legal_name, document_id, run_id)
        assert ampersand_resolved == expected_entity_id, (
            f"{seeded_legal_name!r} (the seeded SEC title itself) failed to resolve"
        )

        and_resolved = resolve_entity_name(conn, real_filing_form, document_id, run_id)
        assert and_resolved == expected_entity_id, (
            f"{real_filing_form!r} (the real filing's own spelled-out form) failed to resolve "
            f"to the same entity as {seeded_legal_name!r} -- the exact Dry Run 001 gap"
        )


def test_seeding_is_idempotent_with_the_new_alias(conn):
    """Running seed_entities twice must not duplicate the new alias any
    more than it duplicates the existing ones."""
    seed_all(conn)
    seed_all(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entity_aliases WHERE alias_source = 'seed_ampersand_and_form'")
        first_count = cur.fetchone()[0]
    seed_all(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entity_aliases WHERE alias_source = 'seed_ampersand_and_form'")
        second_count = cur.fetchone()[0]
    assert first_count > 0
    assert first_count == second_count


# ---------------------------------------------------------------------------
# Curated "Lilly" alias (code review, post-Recall-Check-001, see
# build/RECALL_CHECK_001.md): 32 of 34 Lilly events use role="issuer" and
# already resolve via the issuer-identity shortcut in extraction_runner.py
# (untouched by this fix). The remaining 2 use bare "Lilly" with
# role="buyer" -- falls through to ordinary resolution, and no alias
# covered it before this.
# ---------------------------------------------------------------------------

def test_lilly_bare_alias_resolves_to_the_eli_lilly_entity(conn):
    seed_all(conn)
    document_id = make_raw_document(conn)
    run_id = make_extraction_run(conn, document_id=document_id)

    with conn.cursor() as cur:
        cur.execute("SELECT entity_id FROM entities WHERE cik = '0000059478'")
        lilly_id = str(cur.fetchone()[0])

    resolved = resolve_entity_name(conn, "Lilly", document_id, run_id)
    assert resolved == lilly_id


def test_curated_alias_seeding_fails_loudly_on_collision_with_a_different_entity(conn):
    """Constructed collision, not real data (per the fix's own explicit
    instruction) -- a curated alias must never silently skip or overwrite
    when its normalized form already belongs to a DIFFERENT entity."""
    other_entity_id = make_entity(conn, "Some Other Lilly Company")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias_text, normalized_alias, alias_source) "
            "VALUES (%s, 'Lilly', 'lilly', 'test_setup_collision')",
            (other_entity_id,),
        )
    real_lilly_id = make_entity(conn, "Eli Lilly and Company Test")

    with pytest.raises(RuntimeError):
        insert_curated_alias(conn, real_lilly_id, "Lilly")

    # And it must not have silently inserted anything for real_lilly_id either.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM entity_aliases WHERE entity_id = %s AND normalized_alias = 'lilly'",
            (real_lilly_id,),
        )
        assert cur.fetchone()[0] == 0


def test_curated_alias_seeding_succeeds_when_no_collision_exists(conn):
    entity_id = make_entity(conn, "Eli Lilly and Company Test 2")
    insert_curated_alias(conn, entity_id, "Lilly")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_alias, alias_source FROM entity_aliases WHERE entity_id = %s",
            (entity_id,),
        )
        row = cur.fetchone()
    assert row == ("lilly", "seed_curated_alias")
