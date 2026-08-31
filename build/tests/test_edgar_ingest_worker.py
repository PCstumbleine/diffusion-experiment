"""
The live SEC API can't be reached from this sandbox (no outbound network),
so this tests the parts that matter most and CAN be tested here: given a
realistic submissions-API response and a realistic "-index-headers.html"
document listing, does the worker correctly write raw_documents (primary +
exhibits) with the right timestamp taxonomy, does --dry-run genuinely write
nothing, is dedup keyed off accession+sequence rather than raw content or
a non-unique type label, does one filing's failure leave the rest of a
poll's batch intact, and is a filing's ingestion genuinely all-or-nothing?
These are exactly the bugs three rounds of code review caught in this file.

The fixtures below mirror real SEC formats confirmed by fetching live
filings directly (see edgar_ingest_worker.py's revision notes) rather than
assumed:
  - SAMPLE_INDEX_HEADERS_WITH_EXHIBITS mirrors an ordinary filing.
  - SAMPLE_INDEX_HEADERS_WITH_DUPLICATE_TYPES mirrors a REAL, live 8-K
    (UDR Inc., accession 0000074208-26-000045, filed 2026-04-29) that
    contains two EX-99.1 documents (.htm and .pdf) and two EX-99.2
    documents (.htm and .pdf) -- confirmed by fetching that exact filing's
    own "-index-headers.html". This is the filing that proved the old
    (accession, document_component) identity was not just theoretically
    unsafe but demonstrably broken on real, current data.
"""
import sys
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import psycopg2
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from edgar_ingest_worker import (
    Filing, FilingDocument, FilingPackageParseError,
    list_recent_target_filings, list_filing_documents,
    ingest_filing, find_existing_document, accession_already_ingested,
    poll_once, TARGET_FORMS, POLL_INTERVAL_SECONDS,
)

SAMPLE_SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "10-Q", "8-K", "8-K/A"],
            "accessionNumber": [
                "0000320193-26-000101", "0000320193-26-000099",
                "0000320193-26-000102", "0000320193-26-000103",
            ],
            "filingDate": ["2026-08-29", "2026-08-15", "2026-08-30", "2026-08-31"],
            "acceptanceDateTime": [
                "2026-08-29T16:32:11.000Z",
                "2026-08-15T20:01:00.000Z",
                "2026-08-30T09:05:44.000Z",
                "2026-08-31T10:00:00.000Z",
            ],
            "primaryDocument": ["exhibit1.htm", "q3.htm", "exhibit2.htm", "exhibit2a.htm"],
        }
    }
}

# Verified live against sec.gov: the real "-index-headers.html" shape --
# <DOCUMENT> blocks each carrying <TYPE>, <SEQUENCE>, <FILENAME>.
SAMPLE_INDEX_HEADERS_WITH_EXHIBITS = """
<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>exhibit1.htm <DESCRIPTION>8-K </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.1 <SEQUENCE>2 <FILENAME>ex991.htm <DESCRIPTION>EX-99.1 </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.2 <SEQUENCE>3 <FILENAME>ex992.htm <DESCRIPTION>EX-99.2 </DOCUMENT>

<DOCUMENT> <TYPE>XML <SEQUENCE>4 <FILENAME>R1.htm <DESCRIPTION>IDEA: XBRL DOCUMENT </DOCUMENT>
"""

# Mirrors the REAL, live UDR Inc. 8-K (0000074208-26-000045, filed
# 2026-04-29) -- confirmed by fetching its own "-index-headers.html"
# directly. EX-99.1 and EX-99.2 each appear twice: once as .htm, once as
# .pdf, sharing the same TYPE label but distinct SEQUENCE numbers.
SAMPLE_INDEX_HEADERS_WITH_DUPLICATE_TYPES = """
<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>udr-20260429x8k.htm <DESCRIPTION>8-K </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.1 <SEQUENCE>2 <FILENAME>udr-20260429xex99d1.htm <DESCRIPTION>EX-99.1 </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.2 <SEQUENCE>3 <FILENAME>udr-20260429xex99d2.htm <DESCRIPTION>EX-99.2 </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.1 <SEQUENCE>5 <FILENAME>udr-20260429xex99d1.pdf <DESCRIPTION>EX-99.1 </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.2 <SEQUENCE>6 <FILENAME>udr-20260429xex99d2.pdf <DESCRIPTION>EX-99.2 </DOCUMENT>
"""


def test_list_recent_target_filings_filters_by_form_only():
    """Fix #15: no time-based filtering any more -- every target-form
    filing in 'recent' is returned, and the database dedup check (not this
    function) decides what's actually new."""
    filings = list_recent_target_filings(SAMPLE_SUBMISSIONS, TARGET_FORMS)
    # 8-K and 8-K/A, not the 10-Q -- form filter works, and amendments are included.
    assert {f.form for f in filings} == {"8-K", "8-K/A"}
    assert {f.accession_number for f in filings} == {
        "0000320193-26-000101", "0000320193-26-000102", "0000320193-26-000103",
    }


def test_list_filing_documents_includes_relevant_exhibits_only():
    docs = list_filing_documents(SAMPLE_INDEX_HEADERS_WITH_EXHIBITS, primary_document="exhibit1.htm")
    labels = {d.component_label for d in docs}
    # Primary plus the two EX-99.x exhibits (where earnings/guidance numbers
    # actually live), but not the unrelated XML exhibit.
    assert labels == {"primary", "EX-99.1", "EX-99.2"}
    primary = [d for d in docs if d.role == "primary"]
    assert len(primary) == 1
    assert primary[0].filename == "exhibit1.htm"
    assert primary[0].sequence == 1


def test_list_filing_documents_raises_on_zero_document_entries():
    """Fix #13: a review round argued that a REAL filing's own primary
    document is necessarily at least one <DOCUMENT> block, so zero matches
    means the fetch/parse broke -- not that the filing has no exhibits.
    Silently falling back to primary-only used to recreate the original
    missing-exhibit bug under a new disguise; now it's a visible failure
    that poll_once retries next cycle instead."""
    with pytest.raises(FilingPackageParseError):
        list_filing_documents("this is not an index-headers document at all", primary_document="exhibit1.htm")


def test_list_filing_documents_raises_when_primary_document_not_among_entries():
    """A defensive companion to the zero-entries case: entries exist, but
    none of them is the primary_document the submissions API reported --
    also treated as a parse/mismatch failure, not silently ignored."""
    with pytest.raises(FilingPackageParseError):
        list_filing_documents(SAMPLE_INDEX_HEADERS_WITH_EXHIBITS, primary_document="does-not-exist.htm")


def test_list_filing_documents_raises_on_document_block_missing_a_required_field():
    """A fourth review round caught that a <DOCUMENT> block missing one of
    TYPE/SEQUENCE/FILENAME was silently skipped (log.warning + continue),
    inconsistent with the fail-visibly philosophy used everywhere else in
    this function -- SEC's spec requires all three in every document tag
    nest, so a block missing one means the package is malformed, and
    silently discarding it could hide exactly the exhibit this worker
    exists to fetch."""
    headers = """
<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>primary.htm <DESCRIPTION>8-K </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.1 <FILENAME>ex991.htm <DESCRIPTION>EX-99.1 </DOCUMENT>
"""
    with pytest.raises(FilingPackageParseError):
        list_filing_documents(headers, primary_document="primary.htm")


def test_list_filing_documents_raises_on_duplicate_sequence_numbers():
    """SEC's spec doesn't explicitly promise SEQUENCE is unique within one
    package -- this pipeline now relies on that for identity (fix #10), so
    it enforces the assumption itself and fails closed if it's ever
    violated, rather than silently picking one of the colliding documents."""
    headers = """
<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>primary.htm <DESCRIPTION>8-K </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.1 <SEQUENCE>2 <FILENAME>ex991.htm <DESCRIPTION>EX-99.1 </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.2 <SEQUENCE>2 <FILENAME>ex992.htm <DESCRIPTION>EX-99.2 </DOCUMENT>
"""
    with pytest.raises(FilingPackageParseError):
        list_filing_documents(headers, primary_document="primary.htm")


def test_list_filing_documents_raises_when_a_relevant_exhibit_exists_only_as_pdf():
    """SEC rules do allow a PDF to be the sole OFFICIAL document for some
    filing types (e.g. 8-K Item 6.10, asset-backed issuers -- flagged by a
    fourth review round). If the only copy of a relevant exhibit type is
    binary, this must fail visibly rather than silently mark the filing
    fully ingested while never reading that exhibit's content."""
    headers = """
<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>primary.htm <DESCRIPTION>8-K </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.1 <SEQUENCE>2 <FILENAME>ex991.pdf <DESCRIPTION>EX-99.1 </DOCUMENT>
"""
    with pytest.raises(FilingPackageParseError):
        list_filing_documents(headers, primary_document="primary.htm")


def test_list_filing_documents_handles_duplicate_type_labels_via_sequence():
    """Fix #10, the biggest finding of the third review round: a REAL, live
    8-K (UDR Inc., see module docstring) has two EX-99.1 documents (.htm
    and .pdf) and two EX-99.2 documents. The PDF copies must be skipped
    (fix #11 -- no PDF text extraction here), leaving exactly one EX-99.1
    and one EX-99.2, each carrying its own real SEQUENCE number."""
    docs = list_filing_documents(SAMPLE_INDEX_HEADERS_WITH_DUPLICATE_TYPES, primary_document="udr-20260429x8k.htm")
    labels = [d.component_label for d in docs if d.role == "exhibit"]
    assert labels == ["EX-99.1", "EX-99.2"]  # PDF duplicates skipped, HTML kept
    filenames = {d.filename for d in docs}
    assert "udr-20260429xex99d1.pdf" not in filenames
    assert "udr-20260429xex99d2.pdf" not in filenames
    assert "udr-20260429xex99d1.htm" in filenames
    sequences = {d.component_label: d.sequence for d in docs}
    assert sequences["EX-99.1"] == 2
    assert sequences["EX-99.2"] == 3


def test_list_filing_documents_type_field_may_contain_whitespace():
    """Fix #12: the old whole-file regex assumed <TYPE> never contains a
    space, which is false for some real EDGAR form types (e.g.
    "SCHEDULE 13D", confirmed live). Not one of this project's target
    forms, but the parser must not choke on/misparse a block like this."""
    headers = """
<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>primary.htm <DESCRIPTION>8-K </DOCUMENT>

<DOCUMENT> <TYPE>SCHEDULE 13D <SEQUENCE>2 <FILENAME>sc13d.htm <DESCRIPTION>SC 13D </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.1 <SEQUENCE>3 <FILENAME>ex991.htm <DESCRIPTION>EX-99.1 </DOCUMENT>
"""
    docs = list_filing_documents(headers, primary_document="primary.htm")
    # The multi-word-type block is correctly parsed and correctly excluded
    # (not an EX-99 prefix) rather than corrupting the parse of what follows.
    assert {d.component_label for d in docs} == {"primary", "EX-99.1"}


def make_client(index_headers_text, document_texts: dict[str, str]):
    """document_texts maps filename -> fake body text."""
    client = MagicMock()
    client.get_filing_index_headers.return_value = index_headers_text
    client.get_document_text.side_effect = lambda cik, acc, filename: document_texts[filename]
    return client


def test_ingest_filing_writes_primary_and_exhibits_with_full_timestamp_taxonomy(conn):
    client = make_client(
        SAMPLE_INDEX_HEADERS_WITH_EXHIBITS,
        {
            "exhibit1.htm": "<html>8-K cover page: see Exhibit 99.1</html>",
            "ex991.htm": "<html>Q3 earnings release: revenue $5.1B, guidance raised</html>",
            "ex992.htm": "<html>Supplemental commentary</html>",
        },
    )
    filing = Filing(
        accession_number="0000320193-26-000101", form="8-K", filing_date="2026-08-29",
        acceptance_datetime="2026-08-29T16:32:11.000Z", primary_document="exhibit1.htm",
    )
    catalyst_id = ingest_filing(conn, client, "0000320193", filing)
    assert catalyst_id is not None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT document_component, sec_document_sequence, raw_content, sec_acceptance_at, "
            "canonical_first_public_at, first_public_timestamp_source, source_published_at, "
            "first_public_timestamp_precision, source_observed_at "
            "FROM raw_documents WHERE sec_accession_number = %s "
            "ORDER BY sec_document_sequence",
            (filing.accession_number,),
        )
        rows = cur.fetchall()

    components = {r[0] for r in rows}
    assert components == {"primary", "EX-99.1", "EX-99.2"}
    sequences = [r[1] for r in rows]
    assert sequences == sorted(sequences) and len(set(sequences)) == 3  # each document has its own real sequence
    ex991 = next(r for r in rows if r[0] == "EX-99.1")
    assert "revenue $5.1B" in ex991[2]  # the actual earnings numbers -- not just the cover page

    observed_ats = {r[8] for r in rows}
    assert len(observed_ats) == 1  # fix #16: one shared observation instant across the whole package

    for (component, sequence, raw_content, sec_acceptance_at, canonical_first_public_at,
         source, source_published_at, precision, source_observed_at) in rows:
        assert source == "ingestion_poll_observed"
        assert canonical_first_public_at >= sec_acceptance_at
        assert source_published_at is None  # fix #8
        assert precision >= timedelta(seconds=POLL_INTERVAL_SECONDS)  # fix #9
        assert precision >= (canonical_first_public_at - sec_acceptance_at)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM catalyst_documents WHERE catalyst_id = %s", (catalyst_id,))
        assert cur.fetchone()[0] == 3  # primary + 2 exhibits, all linked to the same catalyst


def test_ingest_filing_handles_a_real_duplicate_type_filing_without_colliding(conn):
    """The exact scenario that broke the old identity: a real filing with
    two EX-99.1 documents (.htm + .pdf). Must ingest cleanly, keep the HTML
    copy, skip the PDF, and use sequence (not the shared 'EX-99.1' label)
    as the actual database identity."""
    client = make_client(
        SAMPLE_INDEX_HEADERS_WITH_DUPLICATE_TYPES,
        {
            "udr-20260429x8k.htm": "<html>8-K cover page</html>",
            "udr-20260429xex99d1.htm": "<html>EX-99.1 press release text</html>",
            "udr-20260429xex99d2.htm": "<html>EX-99.2 supplemental text</html>",
        },
    )
    filing = Filing(
        accession_number="0000074208-26-000045", form="8-K", filing_date="2026-04-29",
        acceptance_datetime="2026-04-29T16:00:00.000Z", primary_document="udr-20260429x8k.htm",
    )
    catalyst_id = ingest_filing(conn, client, "0000074208", filing)
    assert catalyst_id is not None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT document_component, sec_document_sequence, source_url FROM raw_documents "
            "WHERE sec_accession_number = %s ORDER BY sec_document_sequence",
            (filing.accession_number,),
        )
        rows = cur.fetchall()
    assert len(rows) == 3  # primary + EX-99.1(.htm) + EX-99.2(.htm) -- PDFs skipped, no unique-violation
    assert {r[2] for r in rows if r[2].endswith(".pdf")} == set()
    # client.get_document_text was never even asked to fetch the PDFs, since
    # list_filing_documents already excluded them.
    fetched_filenames = {call.args[2] for call in client.get_document_text.call_args_list}
    assert "udr-20260429xex99d1.pdf" not in fetched_filenames


def test_dry_run_writes_nothing_to_the_database(conn):
    """Fix #2, the confirmed bug: a prior version of ingest_filing wrote a
    placeholder into raw_documents during --dry-run and committed it,
    which then permanently blocked the real filing (the 'already ingested'
    check keyed off the same identity either way). This test fails loudly
    if that regresses."""
    client = make_client(
        SAMPLE_INDEX_HEADERS_WITH_EXHIBITS,
        {"exhibit1.htm": "real body", "ex991.htm": "real exhibit", "ex992.htm": "real exhibit 2"},
    )
    filing = Filing(
        accession_number="0000320193-26-000999", form="8-K", filing_date="2026-08-30",
        acceptance_datetime="2026-08-30T09:05:44.000Z", primary_document="exhibit1.htm",
    )

    result = ingest_filing(conn, client, "0000320193", filing, dry_run=True)
    assert result is None

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_documents")
        assert cur.fetchone()[0] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM catalysts")
        assert cur.fetchone()[0] == 0

    # Crucially: a REAL run for the same filing afterward must not be
    # blocked by the dry run.
    real_id = ingest_filing(conn, client, "0000320193", filing, dry_run=False)
    assert real_id is not None
    assert accession_already_ingested(conn, filing.accession_number)


def test_identical_content_under_a_different_accession_is_kept_not_dropped(conn):
    """Fix #4, the confirmed bug: content_hash was UNIQUE, so a second,
    genuinely distinct disclosure that happened to share identical
    boilerplate text with an earlier one would silently vanish. Now
    identity is (accession, sequence); content_hash is only used to flag
    a duplicate, never to skip inserting it."""
    identical_text = "<html>Standard boilerplate 8-K text</html>"
    client = make_client(
        "<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>exhibit1.htm <DESCRIPTION>8-K </DOCUMENT>",
        {"exhibit1.htm": identical_text},
    )

    filing_1 = Filing("0000320193-26-000201", "8-K", "2026-08-29",
                       "2026-08-29T16:32:11.000Z", "exhibit1.htm")
    filing_2 = Filing("0000320193-26-000202", "8-K", "2026-08-30",
                       "2026-08-30T16:32:11.000Z", "exhibit1.htm")

    id_1 = ingest_filing(conn, client, "0000320193", filing_1)
    id_2 = ingest_filing(conn, client, "0000320193", filing_2)
    assert id_1 is not None and id_2 is not None
    assert id_1 != id_2  # two distinct catalysts, not collapsed into one

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_documents WHERE raw_content = %s", (identical_text,))
        assert cur.fetchone()[0] == 2  # both occurrences kept

        cur.execute(
            "SELECT duplicate_content_of_document_id FROM raw_documents "
            "WHERE sec_accession_number = %s AND document_component = 'primary'",
            (filing_2.accession_number,),
        )
        # The second occurrence is flagged as a likely duplicate of the first...
        assert cur.fetchone()[0] is not None


def test_watchlist_entity_id_is_recorded_on_the_catalyst(conn):
    """A review round caught that entity_id was pulled from WATCHLIST and
    used only in an error-log message -- the ingestion worker already knows
    the issuer at ingestion time and shouldn't make extraction rediscover
    it later. Confirms it's actually persisted now."""
    from conftest import make_entity
    entity_id = make_entity(conn, "Apple Inc")
    client = make_client(
        "<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>exhibit1.htm <DESCRIPTION>8-K </DOCUMENT>",
        {"exhibit1.htm": "test body"},
    )
    filing = Filing("0000320193-26-000401", "8-K", "2026-08-30",
                     "2026-08-30T09:05:44.000Z", "exhibit1.htm")

    catalyst_id = ingest_filing(conn, client, "0000320193", filing, entity_id=entity_id)
    with conn.cursor() as cur:
        cur.execute("SELECT issuer_entity_id, issuer_cik FROM catalysts WHERE catalyst_id = %s", (catalyst_id,))
        issuer_entity_id, issuer_cik = cur.fetchone()
    assert issuer_entity_id == entity_id
    assert issuer_cik == "0000320193"


def test_already_ingested_accession_is_skipped_not_reprocessed(conn):
    client = make_client(
        "<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>exhibit1.htm <DESCRIPTION>8-K </DOCUMENT>",
        {"exhibit1.htm": "some body"},
    )
    filing = Filing("0000320193-26-000301", "8-K", "2026-08-30",
                     "2026-08-30T09:05:44.000Z", "exhibit1.htm")

    first_id = ingest_filing(conn, client, "0000320193", filing)
    assert first_id is not None
    assert client.get_filing_index_headers.call_count == 1

    second_id = ingest_filing(conn, client, "0000320193", filing)
    assert second_id is None
    assert client.get_filing_index_headers.call_count == 1  # unchanged -- no redundant re-fetch


def test_partial_exhibit_failure_rolls_back_entire_filing_and_is_retried_cleanly(conn):
    """Fix #14: the confirmed-but-unfixed gap from the second review round.
    A prior version caught a failed exhibit fetch with log+continue,
    committing the filing anyway with that exhibit permanently missing --
    the next poll's dedup check would see the accession already present
    and never retry it. Confirms both halves of the real fix: poll 1 rolls
    back everything (not a partial filing), and poll 2 succeeds completely."""
    accession = "0000320193-26-000701"
    filing = Filing(accession, "8-K", "2026-08-30", "2026-08-30T09:05:44.000Z", "exhibit1.htm")

    failing_client = make_client(SAMPLE_INDEX_HEADERS_WITH_EXHIBITS, {})
    call_count = {"n": 0}

    def flaky_get_document_text(cik, acc, filename):
        call_count["n"] += 1
        if filename == "exhibit1.htm":
            return "<html>primary body</html>"
        raise RuntimeError(f"simulated network failure fetching {filename}")

    failing_client.get_document_text.side_effect = flaky_get_document_text

    with pytest.raises(RuntimeError):
        ingest_filing(conn, failing_client, "0000320193", filing)
    conn.rollback()  # mirrors what poll_once's per-filing handler does

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_documents")
        assert cur.fetchone()[0] == 0  # nothing partial left behind
    assert not accession_already_ingested(conn, accession)

    # Second attempt, everything succeeds this time.
    working_client = make_client(
        SAMPLE_INDEX_HEADERS_WITH_EXHIBITS,
        {"exhibit1.htm": "<html>primary body</html>", "ex991.htm": "ex991 body", "ex992.htm": "ex992 body"},
    )
    catalyst_id = ingest_filing(conn, working_client, "0000320193", filing)
    assert catalyst_id is not None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_documents WHERE sec_accession_number = %s", (accession,))
        assert cur.fetchone()[0] == 3  # the complete filing, not a hole where the failed exhibit was


def test_poll_once_continues_after_one_filing_fails_and_rolls_back(conn):
    """A prior version let one filing's ingestion failure propagate out of
    poll_once's per-company loop entirely, silently skipping every OTHER
    new filing for that company too, and (once isolated per-filing) would
    then have left the shared connection's transaction aborted for every
    subsequent query. Confirms both halves: the good filing still gets
    ingested, and the connection is still usable afterward."""
    good_accession = "0000320193-26-000501"
    bad_accession = "0000320193-26-000502"
    submissions = {
        "filings": {
            "recent": {
                "form": ["8-K", "8-K"],
                "accessionNumber": [bad_accession, good_accession],
                "filingDate": ["2026-08-30", "2026-08-30"],
                "acceptanceDateTime": [
                    "2026-08-30T09:00:00.000Z", "2026-08-30T09:05:00.000Z",
                ],
                "primaryDocument": ["bad.htm", "good.htm"],
            }
        }
    }
    client = MagicMock()
    client.get_submissions.return_value = submissions

    def index_headers_side_effect(cik, acc_no_dashes, acc_with_dashes):
        if acc_with_dashes == bad_accession:
            raise RuntimeError("simulated index-fetch failure (e.g. the wrong-URL bug)")
        return "<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>good.htm <DESCRIPTION>8-K </DOCUMENT>"

    client.get_filing_index_headers.side_effect = index_headers_side_effect
    client.get_document_text.side_effect = lambda cik, acc, filename: "some real body"

    from conftest import make_entity
    entity_id = make_entity(conn, "Test Co")
    conn.commit()  # in production WATCHLIST entities already exist and are
    # committed long before a poll runs; this mirrors that so the bad
    # filing's rollback() below (which rolls back this whole connection's
    # CURRENT transaction, not just the failed filing) doesn't also discard
    # the test's own setup.

    poll_once(conn, client, [("0000320193", entity_id)], dry_run=False)

    # The bad filing must not have ingested anything...
    assert not accession_already_ingested(conn, bad_accession)
    # ...but the good filing, later in the same batch, must have gone through.
    assert accession_already_ingested(conn, good_accession)

    # And the connection must still be usable -- proves the failed filing's
    # aborted transaction was rolled back rather than poisoning it.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_documents")
        assert cur.fetchone()[0] == 1


def test_poll_once_scans_all_target_filings_regardless_of_age(conn):
    """Fix #15: no lookback/time filter any more. A filing whose
    acceptanceDateTime is old (e.g. because it only just became visible in
    the API after a long delay) must still be picked up -- correctness
    comes entirely from accession_already_ingested, never from a time
    window."""
    accession = "0000320193-26-000601"
    old_acceptance = datetime.now(timezone.utc) - timedelta(days=200)

    submissions = {
        "filings": {
            "recent": {
                "form": ["8-K"],
                "accessionNumber": [accession],
                "filingDate": [old_acceptance.strftime("%Y-%m-%d")],
                "acceptanceDateTime": [old_acceptance.isoformat().replace("+00:00", "Z")],
                "primaryDocument": ["late.htm"],
            }
        }
    }
    client = MagicMock()
    client.get_submissions.return_value = submissions
    client.get_filing_index_headers.return_value = (
        "<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>late.htm <DESCRIPTION>8-K </DOCUMENT>"
    )
    client.get_document_text.side_effect = lambda cik, acc, filename: "late-disseminated body"

    poll_once(conn, client, [("0000320193", None)], dry_run=False)

    assert accession_already_ingested(conn, accession)
