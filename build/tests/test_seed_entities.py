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
from conftest import make_raw_document, make_extraction_run

from seed_entities import seed_all
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
