"""
edgar_primitives.py -- Historical Replay Phase 1B (spec Section 4.1).

Neutral SEC EDGAR primitives shared by both orchestrators: the live poller
(`edgar_ingest_worker.py`) and the historical backfill module
(`historical_edgar_ingest.py`). Everything here is moved verbatim out of
`edgar_ingest_worker.py` -- a "boring" refactor, not a redesign; no
signature changed in the move. The one genuinely new function is
`insert_raw_document_record`, the neutral persistence primitive that
today's `insert_raw_document` splits into: it takes every provenance value
as an already-computed parameter (no `historical: bool` mode flag),
matching Phase 1A's established pattern in `public_time_provenance.py`.

    edgar_primitives.py
   /                    \\
  edgar_ingest_worker.py   historical_edgar_ingest.py
        LIVE                     HISTORICAL
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

log = logging.getLogger("edgar_primitives")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Self-imposed ceiling, well under SEC's published 10 req/sec limit.
MAX_REQUESTS_PER_SECOND = 2.0
MIN_SECONDS_BETWEEN_REQUESTS = 1.0 / MAX_REQUESTS_PER_SECOND

# Exhibit types worth fetching alongside the primary document. Deliberately
# narrow (per the review's advice not to indiscriminately pull graphics/XBRL
# support files at this project's scale) -- widen if a real run shows
# relevant content living in an exhibit type not listed here.
RELEVANT_EXHIBIT_PREFIXES = ("EX-99",)

# Fix #11 (v4 revision note): a live 8-K was found filing the SAME exhibit
# twice, once as .htm and once as .pdf. requests' `.text` on binary PDF
# bytes does not extract PDF text -- it silently decodes garbage. Rather
# than build a PDF-text-extraction path, a binary-format copy of a relevant
# exhibit is skipped (an .htm/.txt/.xml copy has been present alongside it
# in every live example seen).
SKIPPED_BINARY_EXTENSIONS = (".pdf",)

# Splits a filing's "-index-headers.html" text into one chunk per
# <DOCUMENT> block, then TYPE/SEQUENCE/FILENAME are extracted from WITHIN
# each block independently (fix #12, v4 revision note) -- this is what
# lets a <TYPE> containing a space (e.g. "SCHEDULE 13D", confirmed live;
# not a form this project targets, but the old whole-file regex would have
# silently mis-parsed near one) parse correctly, and keeps three required
# fields tied to the SAME document even if one is ever reordered.
DOCUMENT_BLOCK_RE = re.compile(r"<DOCUMENT>(.*?)(?=<DOCUMENT>|\Z)", re.IGNORECASE | re.DOTALL)
TYPE_FIELD_RE = re.compile(r"<TYPE>\s*([^\r\n<]+)", re.IGNORECASE)
SEQUENCE_FIELD_RE = re.compile(r"<SEQUENCE>\s*(\d+)", re.IGNORECASE)
FILENAME_FIELD_RE = re.compile(r"<FILENAME>\s*([^\r\n<]+)", re.IGNORECASE)


class FilingPackageParseError(Exception):
    """Raised when a filing's "-index-headers.html" doesn't parse into any
    <DOCUMENT> entries at all, or its own reported primary document isn't
    among them (fix #13, v4 revision note). SEC's Public Dissemination
    Technical Specification requires TYPE/SEQUENCE/FILENAME in every
    document tag nest, and a filing's own primary document is necessarily
    at least one <DOCUMENT> block -- so either condition means the fetch or
    parse broke, not that the filing legitimately has no exhibits. A review
    round argued that silently falling back to "primary only" here just
    recreates the original missing-exhibit bug under a different disguise;
    this is raised instead so the caller's existing per-filing rollback
    handles it the same way as any other ingestion failure -- visibly, and
    retried on the next poll/run."""


# ---------------------------------------------------------------------------
# Rate-limited SEC client
# ---------------------------------------------------------------------------

class EdgarClient:
    def __init__(self, user_agent: str, min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS):
        if "REPLACE_WITH_YOUR_EMAIL" in user_agent:
            raise RuntimeError(
                "Set a real identifying User-Agent before running against "
                "the live SEC API — see the module docstring."
            )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval
        self._last_request_at = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, max_retries: int = 4) -> requests.Response:
        backoff = 2.0
        for attempt in range(max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=15)
            except (requests.Timeout, requests.ConnectionError) as exc:
                # A review round caught that only HTTP 429 was retried --
                # a transient network hiccup used to fail the whole poll
                # immediately. Transient network errors get the same
                # exponential-backoff treatment as a 5xx below.
                if attempt == max_retries - 1:
                    raise
                log.warning("Network error fetching %s (%s) -- retrying in %.0fs", url, exc, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            self._last_request_at = time.monotonic()

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 30))
                log.warning("429 from %s — backing off %ss (fair-access limit hit; if this "
                            "recurs, the request pattern needs to slow down further, not retry harder)", url, retry_after)
                time.sleep(retry_after)
                continue
            if resp.status_code == 403:
                # SEC explicitly restricts unidentified/excessive automated access.
                # Do not retry blindly — this needs a human to look at it.
                raise RuntimeError(
                    f"403 from {url} — SEC may be restricting this User-Agent or "
                    "request pattern. Stop and investigate rather than retrying."
                )
            if 500 <= resp.status_code < 600:
                if attempt == max_retries - 1:
                    resp.raise_for_status()
                log.warning("%s from %s -- transient server error, retrying in %.0fs",
                            resp.status_code, url, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError(f"Exceeded retries fetching {url}")

    def get_submissions(self, cik: str) -> dict:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        return self.get(url).json()

    def get_filing_index_headers(self, cik: str, accession_no_dashes: str, accession_with_dashes: str) -> str:
        """SEC publishes a per-document header dump (TYPE/SEQUENCE/FILENAME
        for every file in the filing, including the real EDGAR exhibit type
        like 'EX-99.1') at this path. Verified live against sec.gov across
        three review rounds -- see edgar_ingest_worker.py's revision notes."""
        cik_int = str(int(cik))
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/"
               f"{accession_with_dashes}-index-headers.html")
        return self.get(url).text

    def get_document_text(self, cik: str, accession_no_dashes: str, filename: str) -> str:
        cik_int = str(int(cik))
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{filename}")
        return self.get(url).text


# ---------------------------------------------------------------------------
# Filing / document data model
# ---------------------------------------------------------------------------

@dataclass
class Filing:
    accession_number: str        # as SEC gives it, with dashes, e.g. "0000320193-26-000123"
    form: str
    filing_date: str             # YYYY-MM-DD
    acceptance_datetime: str     # ISO8601, from submissions API
    primary_document: str


@dataclass
class FilingDocument:
    filename: str
    sequence: int         # SEC's own per-document SEQUENCE -- the real identity now (fix #10)
    role: str             # 'primary' | 'exhibit'
    component_label: str  # 'primary' | 'EX-99.1' | ... ; descriptive only, NOT identity


def list_recent_target_filings(submissions: dict, target_forms: set[str]) -> list[Filing]:
    """submissions['filings']['recent'] holds several parallel arrays; SEC's
    documented shape as of this writing. If SEC changes this shape, this
    function is the one place that needs updating.

    No time-based filtering here (fix #15, v4 revision note): SEC's own
    'recent' array is already bounded (at least a year of filings, or the
    1000 most recent, whichever is more), and narrowed to TARGET_FORMS
    that's small for a hobby-scale watchlist -- so scanning all of it and
    letting accession_already_ingested's database check decide what's new
    removes an entire class of "silently missed a late-disseminated
    filing" bugs (an earlier acceptance-time lookback filter could exclude
    a filing that only just became visible in the API -- see fix #6)
    instead of just padding around it with a buffer."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    out: list[Filing] = []
    for i, form in enumerate(forms):
        if form not in target_forms:
            continue
        accession = recent["accessionNumber"][i]
        filing_date = recent["filingDate"][i]
        acceptance = recent.get("acceptanceDateTime", [None] * len(forms))[i]
        primary_doc = recent["primaryDocument"][i]
        if acceptance is None:
            continue
        out.append(Filing(accession, form, filing_date, acceptance, primary_doc))
    return out


def list_filing_documents(index_headers_text: str, primary_document: str) -> list[FilingDocument]:
    """Given a filing's "-index-headers.html" text, return the primary
    document plus any relevant, non-binary exhibits (RELEVANT_EXHIBIT_PREFIXES,
    excluding SKIPPED_BINARY_EXTENSIONS), read from the real per-document
    <TYPE>/<SEQUENCE>/<FILENAME> triplets -- one <DOCUMENT> block at a time
    (fix #12). Raises FilingPackageParseError if no blocks parse at all, or
    if the submissions API's own primary_document isn't among them (fix
    #13) -- see that class's docstring for why that's failure, not "no
    exhibits".

    Fix #19 (found live while implementing the extraction-runner bridge,
    2026-08-31): fetched directly against sec.gov rather than taken on
    faith -- every "-index-headers.html" page serves its <DOCUMENT> block
    HTML-entity-escaped (literally "&lt;DOCUMENT&gt;", "&lt;TYPE&gt;", ...)
    inside a <PRE> tag, confirmed on two unrelated live filings (NVIDIA
    0001045810-26-000073 and the new-CIK ExxonMobil Holdings Corp filing
    0001193125-26-373026). The regexes below look for literal "<DOCUMENT>"
    etc., which NEVER matches the real page -- every prior test fixture
    (SAMPLE_INDEX_HEADERS_WITH_EXHIBITS and friends) used unescaped text
    directly as a Python string literal, which is not what requests'
    `.text` actually returns from this URL. Net effect as shipped through
    v4: every real (non-dry-run) poll would raise FilingPackageParseError
    on every single filing -- this worker could never have ingested
    anything from a live run, despite three prior review rounds fetching
    live filings and not catching it (they checked the URL and the
    TYPE/SEQUENCE/FILENAME field NAMES against a live page, but not
    whether the fetched bytes needed unescaping before the regexes could
    see them at all). Fixed: unescape before parsing."""
    index_headers_text = html.unescape(index_headers_text)
    entries: list[tuple[str, int, str]] = []  # (filename, sequence, edgar_type)
    for block_match in DOCUMENT_BLOCK_RE.finditer(index_headers_text):
        block = block_match.group(1)
        type_match = TYPE_FIELD_RE.search(block)
        sequence_match = SEQUENCE_FIELD_RE.search(block)
        filename_match = FILENAME_FIELD_RE.search(block)
        if not (type_match and sequence_match and filename_match):
            # A fourth review round caught that warn+continue here was
            # inconsistent with the fail-visibly philosophy adopted
            # everywhere else in this function: SEC's spec requires all
            # three fields in every document tag nest, so a block missing
            # one is a malformed/unexpected package, not an irrelevant
            # document to shrug off -- silently discarding it could hide
            # exactly the exhibit this worker exists to fetch.
            raise FilingPackageParseError(
                "A <DOCUMENT> block is missing a required TYPE/SEQUENCE/FILENAME field "
                f"(SEC's spec requires all three in every document tag nest) -- "
                f"block started: {block[:80]!r}"
            )
        entries.append((
            filename_match.group(1).strip(),
            int(sequence_match.group(1)),
            type_match.group(1).strip().upper(),
        ))

    if not entries:
        raise FilingPackageParseError(
            "No parseable <DOCUMENT> entries found in filing index headers -- this "
            "means the fetch or parse broke, not that the filing legitimately has no "
            "exhibits (every filing's own primary document is itself at least one "
            "<DOCUMENT> block per SEC's dissemination spec)."
        )

    # A fourth review round noted that SEC's own spec doesn't explicitly
    # promise SEQUENCE is unique within one package (only that it's
    # required, numeric, and per-document) -- every live filing checked
    # across four review rounds behaves that way, but since this pipeline
    # now RELIES on that for identity (fix #10), it enforces it itself and
    # fails closed if it's ever violated, rather than assuming SEC
    # guarantees something its spec doesn't say in so many words.
    sequences_seen = [seq for _, seq, _ in entries]
    if len(sequences_seen) != len(set(sequences_seen)):
        raise FilingPackageParseError(
            "Duplicate SEC document SEQUENCE values found within one filing package -- "
            "treated as malformed rather than guessing which document is authoritative."
        )

    primary_matches = [e for e in entries if e[0] == primary_document]
    if not primary_matches:
        raise FilingPackageParseError(
            f"Primary document {primary_document!r} (from the submissions API) was "
            "not found among this filing's own <DOCUMENT> entries -- the index "
            "headers may be for the wrong filing or in an unexpected format."
        )
    primary_filename, primary_sequence, _primary_type = primary_matches[0]
    docs: list[FilingDocument] = [FilingDocument(primary_filename, primary_sequence, "primary", "primary")]

    # Group relevant (non-primary) exhibit entries by their EDGAR type label
    # so a PDF is skipped only when a non-binary copy of the SAME exhibit
    # type exists alongside it (every live example checked follows the
    # UDR .htm+.pdf pattern) -- a fourth review round pointed out that SEC
    # rules do allow a PDF to be the sole OFFICIAL document for some filing
    # types (e.g. 8-K Item 6.10, asset-backed issuers), so a relevant
    # exhibit that exists ONLY as a binary file is now a visible failure,
    # not a silently-skipped one -- the old behavior would have marked such
    # a filing "fully ingested" while quietly never reading that exhibit.
    relevant_by_type: dict[str, list[tuple[str, int]]] = {}
    for filename, sequence, item_type in entries:
        if filename == primary_document:
            continue
        if not item_type.startswith(RELEVANT_EXHIBIT_PREFIXES):
            continue
        relevant_by_type.setdefault(item_type, []).append((filename, sequence))

    for item_type, files in relevant_by_type.items():
        binary = [(f, s) for f, s in files if f.lower().endswith(SKIPPED_BINARY_EXTENSIONS)]
        non_binary = [(f, s) for f, s in files if not f.lower().endswith(SKIPPED_BINARY_EXTENSIONS)]
        if not non_binary:
            raise FilingPackageParseError(
                f"Relevant exhibit type {item_type!r} exists only in a binary format "
                f"({[f for f, _ in files]!r}) with no text-format copy in this filing -- "
                "this pipeline has no PDF-text-extraction step, so it cannot safely mark "
                "this filing as fully ingested."
            )
        for filename, _sequence in binary:
            log.info("Skipping relevant exhibit %s (%s) -- binary format not text-extracted by "
                      "this pipeline; a non-binary copy of the same exhibit type is present "
                      "in this filing (see fix #11 in edgar_ingest_worker.py's module docstring)",
                      filename, item_type)
        for filename, sequence in non_binary:
            docs.append(FilingDocument(filename, sequence, "exhibit", item_type))
    return docs


def source_url_for(cik: str, accession_no_dashes: str, filename: str) -> str:
    cik_int = str(int(cik))
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{filename}"


def find_existing_document(conn, sec_accession_number: str, document_component: str) -> str | None:
    """Looks up a specific document by its descriptive component label --
    still useful for tests/inspection, but NOT used any more for the
    ingest-time "already seen this filing" check (see
    accession_already_ingested), since document_component is no longer
    guaranteed unique within an accession (fix #10)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT document_id FROM raw_documents WHERE sec_accession_number = %s AND document_component = %s",
            (sec_accession_number, document_component),
        )
        row = cur.fetchone()
        return row[0] if row else None


def accession_already_ingested(conn, sec_accession_number: str) -> bool:
    """Whether ANY row already exists for this accession. Fix #14 (v4
    revision note) made ingest_filing all-or-nothing per filing -- a
    partial exhibit failure no longer commits, it rolls back the whole
    filing -- so a filing is now either fully absent or fully present, and
    checking existence at all (rather than specifically the 'primary'
    component) is both simpler and correct. It also sidesteps a
    chicken-and-egg problem the old primary-component check had: this
    check has to run BEFORE fetching the filing's index, but identity now
    includes SEQUENCE (fix #10), which isn't known until the index is
    fetched."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM raw_documents WHERE sec_accession_number = %s LIMIT 1", (sec_accession_number,))
        return cur.fetchone() is not None


def find_content_duplicate(conn, content_hash: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT document_id FROM raw_documents WHERE content_hash = %s LIMIT 1", (content_hash,))
        row = cur.fetchone()
        return row[0] if row else None


def load_watchlist(conn) -> list[tuple[str, str]]:
    """Resolves (cik, entity_id) pairs from watchlist_membership at startup
    -- replaces the old hardcoded WATCHLIST list of hand-copied entity_id
    UUIDs (Extraction-Runner Design v2, §1: "generated database IDs should
    never need manual sync into source code"). Run build/seed_entities.py
    first to populate watchlist_membership from
    build/seed_data/watchlist_ciks.csv."""
    with conn.cursor() as cur:
        cur.execute("SELECT cik, entity_id FROM watchlist_membership")
        return [(cik, str(entity_id)) for cik, entity_id in cur.fetchall()]


def filter_watchlist(watchlist: list[tuple[str, str]], only_ciks: str | None) -> list[tuple[str, str]]:
    """Scopes a loaded watchlist down to a comma-separated list of CIKs for
    one invocation, WITHOUT touching watchlist_membership itself -- useful
    for a small, reviewable dry run against a handful of companies rather
    than the full watchlist. CIKs may be given with or without leading
    zeros (both "1045810" and "0001045810" match). None/empty is a no-op."""
    if not only_ciks:
        return watchlist
    wanted = {c.strip().zfill(10) for c in only_ciks.split(",") if c.strip()}
    return [(cik, entity_id) for cik, entity_id in watchlist if cik in wanted]


def link_to_catalyst(conn, catalyst_id: str, document_id: str, role: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalyst_documents (catalyst_id, document_id, document_role) VALUES (%s, %s, %s)",
            (catalyst_id, document_id, role),
        )


def insert_raw_document_record(
    conn,
    cik: str,
    filing: Filing,
    doc: FilingDocument,
    raw_text: str,
    sec_acceptance_at: datetime | None,
    canonical_first_public_at: datetime,
    first_public_timestamp_source: str,
    first_public_timestamp_precision: timedelta,
    source_observed_at: datetime,
    ingested_at: datetime,
) -> str:
    """The neutral persistence primitive both live (edgar_ingest_worker.py)
    and historical (historical_edgar_ingest.py) call -- every provenance
    value is an already-computed parameter; there is no `historical: bool`
    mode flag here (Historical Replay Phase 1B, spec Section 4.1). Does
    NOT link to a catalyst, since the primary document's insert has to
    happen BEFORE a catalyst can exist (catalysts.originating_document_id
    references it).

    Identity is (sec_accession_number, sec_document_sequence) -- NOT
    document_component/content_hash (fix #10 -- see list_filing_documents'
    docstring). content_hash is still recorded and checked, but only to
    flag a likely duplicate, never to skip a distinct filing/component
    (spec Section 3, invariant 8)."""
    accession_no_dashes = filing.accession_number.replace("-", "")
    src_url = source_url_for(cik, accession_no_dashes, doc.filename)
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    duplicate_of = find_content_duplicate(conn, content_hash)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_documents (
                source_name, source_url, document_type, raw_content, content_hash,
                sec_accession_number, document_component, sec_document_sequence,
                duplicate_content_of_document_id,
                source_published_at, sec_acceptance_at, source_observed_at, ingested_at,
                canonical_first_public_at, first_public_timestamp_source,
                first_public_timestamp_precision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING document_id
            """,
            (
                "sec_edgar", src_url, filing.form, raw_text, content_hash,
                filing.accession_number, doc.component_label, doc.sequence,
                duplicate_of,
                # source_published_at: SEC's submissions API gives no
                # genuine source-stated publication timestamp, live or
                # historical (fix #8).
                None, sec_acceptance_at, source_observed_at, ingested_at,
                canonical_first_public_at, first_public_timestamp_source,
                first_public_timestamp_precision,
            ),
        )
        document_id = cur.fetchone()[0]

    if duplicate_of:
        log.info("Ingested %s/%s (seq %s) -- flagged as content-duplicate of %s (kept as its own occurrence)",
                  filing.accession_number, doc.component_label, doc.sequence, duplicate_of)
    return document_id
