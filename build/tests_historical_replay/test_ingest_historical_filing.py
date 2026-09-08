"""
Historical Replay Phase 1B (specs/historical-replay-phase1b-implementation-spec-final.md),
Section 8's ingest_historical_filing integration tests. Every database this
file touches comes exclusively from the `disposable_db_name` fixture
(conftest.py) -- never a hardcoded or passed-through name.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import bootstrap_database as bd
import historical_edgar_ingest as hei
from edgar_primitives import Filing, FilingPackageParseError, accession_already_ingested
from historical_edgar_ingest import (
    ingest_historical_filing,
    UnsupportedHistoricalSECFormError,
)

# Real "-index-headers.html" shape -- matches build/tests/test_edgar_ingest_worker.py's
# own verified-live fixture convention.
SAMPLE_INDEX_HEADERS_WITH_EXHIBITS = """
<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>primary.htm <DESCRIPTION>8-K </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.1 <SEQUENCE>2 <FILENAME>ex991.htm <DESCRIPTION>EX-99.1 </DOCUMENT>

<DOCUMENT> <TYPE>EX-99.2 <SEQUENCE>3 <FILENAME>ex992.htm <DESCRIPTION>EX-99.2 </DOCUMENT>
"""

SAMPLE_INDEX_HEADERS_PRIMARY_ONLY = """
<DOCUMENT> <TYPE>8-K <SEQUENCE>1 <FILENAME>primary.htm <DESCRIPTION>8-K </DOCUMENT>
"""


def make_filing(accession="0000320193-15-000042", form="8-K", filing_date="2026-01-15",
                 acceptance="2026-01-15T20:30:58.000Z", primary_document="primary.htm"):
    return Filing(
        accession_number=accession, form=form, filing_date=filing_date,
        acceptance_datetime=acceptance, primary_document=primary_document,
    )


def make_client(index_headers_text, document_texts: dict):
    """document_texts maps filename -> fake body text -- same convention as
    build/tests/test_edgar_ingest_worker.py's own make_client."""
    client = MagicMock()
    client.get_filing_index_headers.return_value = index_headers_text
    client.get_document_text.side_effect = lambda cik, acc, filename: document_texts[filename]
    return client


class SimulatedFailure(Exception):
    pass


def counts_for_accession(conn, accession_number):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM raw_documents WHERE sec_accession_number = %s", (accession_number,),
        )
        raw_documents_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT count(*) FROM catalysts c
            JOIN raw_documents rd ON rd.document_id = c.originating_document_id
            WHERE rd.sec_accession_number = %s
            """,
            (accession_number,),
        )
        catalysts_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT count(*) FROM catalyst_documents cd
            JOIN raw_documents rd ON rd.document_id = cd.document_id
            WHERE rd.sec_accession_number = %s
            """,
            (accession_number,),
        )
        catalyst_documents_count = cur.fetchone()[0]
    return raw_documents_count, catalysts_count, catalyst_documents_count


@pytest.fixture
def forward_db(disposable_db_name):
    """Historical acquisition is a normal Postgres transaction against
    whatever database it's pointed at -- Phase 1B doesn't require a
    historical_replay-purpose database specifically (that guard belongs to
    a later phase's write entry point, not this one -- see Phase 1B's own
    non-goals). 'forward' is used here purely because it's the simplest
    bootstrap target with the schema this module writes into."""
    bd.cmd_bootstrap(disposable_db_name, "forward")
    conn = bd.connect_to_target(disposable_db_name)
    yield conn
    conn.close()


CIK = "0000320193"


# ===========================================================================
# Form gate, unconditional
# ===========================================================================

@pytest.mark.parametrize("form", ["8-K", "8-K/A"])
def test_form_gate_accepts_supported_forms(forward_db, form):
    conn = forward_db
    filing = make_filing(form=form)
    client = make_client(SAMPLE_INDEX_HEADERS_PRIMARY_ONLY, {"primary.htm": "body"})
    catalyst_id = ingest_historical_filing(conn, client, CIK, filing)
    assert catalyst_id is not None


@pytest.mark.parametrize("bad_form", ["10-K", "4", "8-k", "8-K ", " 8-K", None])
def test_form_gate_rejects_unsupported_forms_before_any_db_read_or_write(forward_db, bad_form):
    conn = forward_db
    filing = make_filing(form=bad_form, accession="0000320193-99-999901")
    client = MagicMock()  # never called -- form gate fires first

    with pytest.raises(UnsupportedHistoricalSECFormError):
        ingest_historical_filing(conn, client, CIK, filing)

    client.get_filing_index_headers.assert_not_called()
    client.get_document_text.assert_not_called()
    raw_docs, catalysts, catalyst_docs = counts_for_accession(conn, filing.accession_number)
    assert (raw_docs, catalysts, catalyst_docs) == (0, 0, 0)


def test_form_gate_fires_even_when_accession_already_present(forward_db):
    """Required variant (spec Section 8): an unsupported-form Filing whose
    accession is ALREADY present in the database (pre-seeded directly)
    still raises on a second call with an unsupported form -- proving the
    form gate is never short-circuited by accession_already_ingested
    returning True first."""
    conn = forward_db
    accession = "0000320193-15-000777"

    # Pre-seed the accession directly -- simulates it already being fully
    # ingested (e.g. once with a supported form in a real run).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_documents (source_name, document_type, raw_content, content_hash, "
            "sec_accession_number, document_component, sec_document_sequence, "
            "source_observed_at, ingested_at) "
            "VALUES ('sec_edgar', '8-K', 'x', 'hash1', %s, 'primary', 1, now(), now())",
            (accession,),
        )
    conn.commit()
    assert accession_already_ingested(conn, accession)

    filing = make_filing(accession=accession, form="10-K")  # unsupported
    client = MagicMock()
    with pytest.raises(UnsupportedHistoricalSECFormError):
        ingest_historical_filing(conn, client, CIK, filing)
    client.get_filing_index_headers.assert_not_called()


# ===========================================================================
# Accession-level atomicity, with the caller's rollback made explicit
# ===========================================================================

def test_accession_atomicity_with_explicit_caller_rollback(forward_db):
    conn = forward_db
    accession = "0000320193-15-000888"
    filing = make_filing(accession=accession)

    def failing_get_document_text(cik, acc, filename):
        if filename == "ex991.htm":
            raise SimulatedFailure("simulated exhibit fetch failure")
        return {"primary.htm": "primary body", "ex992.htm": "ex992 body"}[filename]

    client = MagicMock()
    client.get_filing_index_headers.return_value = SAMPLE_INDEX_HEADERS_WITH_EXHIBITS
    client.get_document_text.side_effect = failing_get_document_text

    with pytest.raises(SimulatedFailure):
        ingest_historical_filing(conn, client, CIK, filing)

    # Mirrors the production caller's contract (poll_once's own except-block
    # rollback) -- ingest_historical_filing does NOT roll back internally
    # (invariant 5), so the test must, exactly like a real caller would,
    # before issuing further queries on this same connection (an aborted
    # Postgres transaction rejects further statements until rolled back).
    conn.rollback()

    raw_docs, catalysts, catalyst_docs = counts_for_accession(conn, accession)
    assert (raw_docs, catalysts, catalyst_docs) == (0, 0, 0)

    # Remove the simulated failure condition -- the SAME call now succeeds.
    client.get_document_text.side_effect = lambda cik, acc, filename: {
        "primary.htm": "primary body", "ex991.htm": "ex991 body", "ex992.htm": "ex992 body",
    }[filename]
    catalyst_id = ingest_historical_filing(conn, client, CIK, filing)
    assert catalyst_id is not None


# ===========================================================================
# Accession-level provenance uniformity
# ===========================================================================

def test_provenance_uniformity_primary_and_exhibits_share_the_identical_triple(forward_db):
    conn = forward_db
    filing = make_filing(accession="0000320193-15-000900", filing_date="2026-02-10",
                          acceptance="2026-02-10T21:00:00.000Z")
    client = make_client(SAMPLE_INDEX_HEADERS_WITH_EXHIBITS, {
        "primary.htm": "primary body", "ex991.htm": "ex991 body", "ex992.htm": "ex992 body",
    })

    catalyst_id = ingest_historical_filing(conn, client, CIK, filing)
    assert catalyst_id is not None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_first_public_at, first_public_timestamp_precision, "
            "first_public_timestamp_source FROM raw_documents WHERE sec_accession_number = %s",
            (filing.accession_number,),
        )
        rows = cur.fetchall()
    assert len(rows) == 3  # primary + 2 exhibits
    assert len(set(rows)) == 1  # every row shares the IDENTICAL triple

    expected_canonical, expected_precision = hei.historical_public_time_interval(
        hei.parse_filing_date_strict(filing)
    )
    canonical, precision, source = rows[0]
    assert canonical == expected_canonical
    assert precision == expected_precision
    assert source == hei.FIRST_PUBLIC_TIMESTAMP_SOURCE


# ===========================================================================
# Full-package linkage
# ===========================================================================

def test_full_package_linkage_one_catalyst_correct_roles(forward_db):
    conn = forward_db
    filing = make_filing(accession="0000320193-15-001000")
    client = make_client(SAMPLE_INDEX_HEADERS_WITH_EXHIBITS, {
        "primary.htm": "primary body", "ex991.htm": "ex991 body", "ex992.htm": "ex992 body",
    })

    catalyst_id = ingest_historical_filing(conn, client, CIK, filing)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM catalysts WHERE catalyst_id = %s", (catalyst_id,))
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT document_role FROM catalyst_documents WHERE catalyst_id = %s ORDER BY document_role",
            (catalyst_id,),
        )
        roles = [r[0] for r in cur.fetchall()]
    assert roles == ["exhibit", "exhibit", "primary"]


# ===========================================================================
# Idempotence
# ===========================================================================

def test_idempotence_second_call_returns_none_and_writes_nothing(forward_db):
    conn = forward_db
    filing = make_filing(accession="0000320193-15-001100")
    client = make_client(SAMPLE_INDEX_HEADERS_WITH_EXHIBITS, {
        "primary.htm": "primary body", "ex991.htm": "ex991 body", "ex992.htm": "ex992 body",
    })

    first_catalyst_id = ingest_historical_filing(conn, client, CIK, filing)
    assert first_catalyst_id is not None
    raw_docs_after_first, catalysts_after_first, catalyst_docs_after_first = counts_for_accession(
        conn, filing.accession_number
    )
    assert (raw_docs_after_first, catalysts_after_first, catalyst_docs_after_first) == (3, 1, 3)

    client.get_filing_index_headers.reset_mock()
    client.get_document_text.reset_mock()

    second_result = ingest_historical_filing(conn, client, CIK, filing)
    assert second_result is None
    client.get_filing_index_headers.assert_not_called()
    client.get_document_text.assert_not_called()

    raw_docs_after_second, catalysts_after_second, catalyst_docs_after_second = counts_for_accession(
        conn, filing.accession_number
    )
    assert (raw_docs_after_second, catalysts_after_second, catalyst_docs_after_second) == (3, 1, 3)


# ===========================================================================
# source_observed_at / ingested_at, deterministic (no real-clock reliance)
# ===========================================================================

def test_utc_now_is_called_fresh_per_filing_not_reused_across_a_backfill_run(forward_db, monkeypatch):
    conn = forward_db
    first_instant = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 5, 2, 9, 30, 0, tzinfo=timezone.utc)
    instants = iter([first_instant, second_instant])
    monkeypatch.setattr(hei, "utc_now", lambda: next(instants))

    filing_1 = make_filing(accession="0000320193-15-001200")
    filing_2 = make_filing(accession="0000320193-15-001300")
    client = make_client(SAMPLE_INDEX_HEADERS_WITH_EXHIBITS, {
        "primary.htm": "primary body", "ex991.htm": "ex991 body", "ex992.htm": "ex992 body",
    })

    ingest_historical_filing(conn, client, CIK, filing_1)
    ingest_historical_filing(conn, client, CIK, filing_2)

    def observed_instants(accession):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT source_observed_at, ingested_at FROM raw_documents "
                "WHERE sec_accession_number = %s",
                (accession,),
            )
            return cur.fetchall()

    package_1 = observed_instants(filing_1.accession_number)
    package_2 = observed_instants(filing_2.accession_number)

    assert len(package_1) == 1  # every document in package 1 shares ONE instant
    assert package_1[0] == (first_instant, first_instant)  # source_observed_at == ingested_at
    assert len(package_2) == 1
    assert package_2[0] == (second_instant, second_instant)
    assert package_2[0][0] > package_1[0][0]  # a fresh, later instant for the later filing
