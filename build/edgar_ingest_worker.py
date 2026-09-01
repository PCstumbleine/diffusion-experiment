#!/usr/bin/env python3
"""
SEC EDGAR ingestion worker — Diffusion Experiment v2.2.1, Section 6/7/10.

What this does: for a small watchlist of companies (by CIK), polls SEC's
public "submissions" API for new filings of the target forms (8-K/8-K-A by
default), fetches the filing's PRIMARY document AND its exhibits (a 8-K's
cover page routinely just says "see Exhibit 99.1" for the actual earnings
release/guidance numbers), and stores each as an immutable row in
raw_documents, all linked to one shared catalyst. It does not extract
events — that's the separate extraction-prompt step (extraction_prompt_v1.md)
run against the raw_documents this worker produces.

Revision note (v2 of this file): a code review round caught four real bugs
in the first version, all fixed here:
  1. Only primaryDocument was fetched, missing exhibits where the actual
     numbers usually live. Fixed: fetch the filing's index and pull primary
     + exhibits, sharing one catalyst_id via catalyst_documents.
  2. --dry-run wrote a placeholder into the real database and permanently
     blocked the real filing from ever being ingested (the "already seen
     this" check keyed off the same URL either way). Fixed: dry-run now
     fetches metadata and logs what it WOULD do, and writes nothing.
  3. canonical_first_public_at trusted SEC's acceptance timestamp, but SEC's
     own documentation confirms filings submitted after 5:30pm ET are
     disseminated the next business day — verified directly against
     https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
     before writing this fix. Fixed: canonical_first_public_at is now this
     pipeline's own observed time (conservative and honest for a
     15-minute-polling hobby system), with SEC's acceptance time kept
     separately as sec_acceptance_at — a raw fact, not treated as the
     public-availability time.
  4. content_hash was UNIQUE, so two distinct disclosures sharing identical
     boilerplate text would silently collapse into one row. Fixed: identity
     is now (sec_accession_number, document_component); content_hash stays
     indexed only to flag likely duplicates via duplicate_content_of_document_id.

Revision note (v3 of this file): a second review round (ChatGPT, against v2)
flagged that the mocked tests could be hiding live-API bugs in fix #1 and
#3. Checked directly against sec.gov rather than taken on faith -- both
were real, and worse than "imprecise":
  5. get_filing_index() requested
     ".../{accession-no-dashes}/{accession-with-dashes}-index.json" --
     fetched live, this 404s. The real per-filing directory listing lives
     at ".../{accession-no-dashes}/index.json" (no accession prefix). But
     that real endpoint's "type" field turned out to be a generic
     file-icon category (e.g. "text.gif"), NOT an EDGAR document type like
     "EX-99.1" -- confirmed by fetching a live filing's index.json and
     comparing it to that same filing's "-index-headers.html", which DOES
     carry the real per-document <TYPE>/<SEQUENCE>/<FILENAME> triplet. So
     the old code wasn't just going to 404: even pointed at the right URL,
     its exhibit-matching logic could never have matched anything, ever.
     Fixed: fetch "-index-headers.html" instead and parse the
     <TYPE>...<FILENAME>... entries directly.
  6. list_recent_target_filings() filtered candidate filings by SEC's
     acceptanceDateTime against the poll's lookback window. Combined with
     fix #3, a filing accepted Friday at 6pm but not disseminated until
     Monday could fall outside a lookback window and be silently dropped
     forever. Fixed in v3 with a buffer; fixed properly in v4 (see below).
  7. A single filing's ingestion failure was not caught inside the
     per-company loop in poll_once, so it propagated out and silently
     skipped every OTHER new filing for that company until the next poll
     cycle. Fixed: each filing is now caught and logged individually, with
     conn.rollback() so a failed statement doesn't poison the shared
     connection for the rest of the batch.
  8. source_published_at was being set to sec_acceptance_at -- silently
     re-introducing exactly the "acceptance time treated as publication
     time" conflation that fix #3 was written to remove, just one column
     over. Fixed: left NULL until there's an actual source-stated
     publication timestamp to put there.
  9. first_public_timestamp_precision was hardcoded to the steady-state
     poll interval (15 minutes), dishonest for a wide backfill lookback.
     Fixed: precision is now max(poll interval, observed - acceptance).

Revision note (v4 of this file): a THIRD review round (ChatGPT again,
against v3) went further than static reading this time -- it fetched
several more live SEC filings itself and found a real bug the earlier
fixes had missed, plus tightened up loose ends in fixes #6/#7/#9:
  10. **The single biggest finding, and the reason "document_component
      identity collision" moved from the README's deferred list to fixed
      here:** the reviewer found a REAL, live 8-K (UDR Inc., accession
      0000074208-26-000045, filed 2026-04-29) containing TWO EX-99.1
      documents -- one .htm, one .pdf -- and the same duplicate pattern for
      EX-99.2. Independently re-fetched that exact filing's own
      "-index-headers.html" to confirm before touching anything: real,
      confirmed. Under the old (sec_accession_number, document_component)
      identity, the second EX-99.1 would hit a UNIQUE violation, which (via
      fix #7's per-filing rollback) would cleanly undo the whole filing --
      but then retry and fail identically on every future poll, forever,
      since the collision is deterministic. That's a permanently
      un-ingestable filing, not a rare theoretical edge case. Fixed: SEC's
      own per-document SEQUENCE number (required in every document tag
      nest per SEC's Public Dissemination Technical Specification, and
      necessarily unique within one filing package) is now the real
      identity (schema.sql: sec_document_sequence); document_component
      stays as a purely descriptive label, no longer unique.
  11. The same live filing exposed a second real bug: one of the two
      EX-99.1 copies is a **.pdf**. get_document_text() was doing
      `response.text` unconditionally, which does NOT extract PDF text --
      it decodes binary PDF bytes as if they were character text, silently
      producing garbage into raw_content. There is no PDF-text-extraction
      step in this pipeline. Fixed: a relevant exhibit whose filename ends
      in a binary extension (SKIPPED_BINARY_EXTENSIONS) is skipped with a
      clear log message rather than ingested as corrupted text -- in every
      live example seen so far, an .htm/.txt/.xml copy of the same exhibit
      is filed alongside the .pdf one anyway. True PDF-text extraction (for
      the rarer case where PDF is the ONLY copy) is deliberately not built
      here -- flagged as future work in the README, not silently skipped.
  12. list_filing_documents()'s single whole-file regex assumed a
      <TYPE> value never contains whitespace, which is false for some real
      EDGAR form types (e.g. "SCHEDULE 13D" -- confirmed live). Not a
      correctness bug for this project's actual target forms (8-K / EX-99.x
      never contain a space), but fixing the identity bug above required
      parsing SEQUENCE per-document anyway, so the parser was restructured
      to split on <DOCUMENT> blocks first and extract TYPE/SEQUENCE/FILENAME
      from within each block independently -- which also fixes this for
      free and is more robust if a document nest is ever missing a field.
  13. list_filing_documents() silently fell back to "primary only" when no
      <DOCUMENT> entries matched at all. The reviewer argued (and SEC's own
      spec, which requires TYPE/SEQUENCE/FILENAME in every document tag
      nest, backs this up) that a real filing's own primary document is
      necessarily at least one <DOCUMENT> block -- so zero matches means
      the fetch or parse broke, not that the filing legitimately has no
      exhibits. Silently treating that as success just recreates the
      original "missing exhibits" bug under a new disguise. Fixed: raises
      FilingPackageParseError instead (also raised if the submissions API's
      own primary_document isn't found among the parsed entries at all),
      which poll_once's existing per-filing handler rolls back and retries
      next cycle -- failing visibly rather than silently ingesting an
      incomplete package.
  14. Confirmed but NOT actually fixed until now: a failed exhibit fetch
      inside ingest_filing was caught with log+continue, letting the
      filing commit anyway with that exhibit permanently missing -- the
      next poll's dedup check would see the accession already present and
      never retry the failed exhibit. Fixed: exhibit fetch failures now
      propagate, so poll_once's per-filing rollback (fix #7) discards the
      WHOLE partial filing and the next poll retries it completely, rather
      than a partial filing quietly living forever with a hole in it.
  15. list_recent_target_filings()'s lookback filter -- even after v3's
      dissemination-delay buffer -- was still an unnecessary heuristic on
      top of an already-bounded input: SEC's own submissions API caps each
      company's "recent" array at at least a year of filings or the 1000
      most recent, whichever is more, updated as filings disseminate. For
      a small hobby watchlist filtered to TARGET_FORMS, that's cheap to
      scan in full every time. Fixed: the time-based filter (and
      --lookback-hours) is removed entirely; every target-form filing in
      "recent" is considered every poll, and accession_already_ingested's
      database check (not any time window) is what decides what's new --
      removing the whole class of "silently missed a late-disseminated
      filing" bugs instead of just padding around it.
  16. insert_raw_document() computed `datetime.now()` freshly for EACH
      document, so primary/EX-99.1/EX-99.2 in one filing package could get
      slightly different source_observed_at / canonical_first_public_at
      values a few seconds apart, despite being discovered in the same
      polling event. Fixed: ingest_filing now captures one
      filing_observed_at timestamp and passes it to every insert for that
      filing's package.
  17. check_arm_entry_instrument (schema.sql) enforced "non-E arms need an
      instrument" but not the reverse ("arm E must NOT have one") -- so a
      cash-equivalent entry could still accidentally carry a security.
      Fixed with the missing half of that check.
  18. New schema gap found by the same review pass: an arm_entries row's
      candidate_id and instrument_id could belong to two DIFFERENT
      entities (e.g. a candidate about NVIDIA paired with an Apple
      instrument) -- both individually-valid foreign keys, same class of
      bug as the consistency triggers added in v3. Fixed with an entity-
      match check added to check_arm_entry_candidate_consistency.

Fair-access compliance (Section 6, v2.2.1): this worker identifies itself
with a descriptive User-Agent (REQUIRED — SEC blocks unidentified automated
traffic), self-limits well under the published 10 requests/second ceiling,
and only requests what it actually needs (a small watchlist's submissions
feed plus each new filing's own small package, not a bulk crawl). Before
running this for real, edit USER_AGENT below to include your real contact
info — SEC's guidance specifically expects an identifying string.

Network note: this file was developed and unit-tested in a sandboxed
environment with no outbound access to sec.gov FROM THE WORKER'S OWN HTTP
CODE, so the database-side logic is exercised by real tests against a real
Postgres instance (see tests/test_edgar_ingest_worker.py, which mocks the
SEC responses), but the worker's own requests-based HTTP calls have not.
The URL shapes and document-header format this worker assumes WERE
independently checked against several live sec.gov filings across three
review rounds (see the revision notes above, especially v4's #10-#13,
which came directly from fetching a live filing that broke the old
assumptions) -- but that was done with a separate, general-purpose
web-fetch tool, not this worker's own code path. Run
`python3 edgar_ingest_worker.py --once --dry-run` against the real network
and inspect its log output before trusting this unattended. Known,
deliberately-not-built limitation: a relevant exhibit filed ONLY as a PDF
(no .htm/.txt/.xml copy) is skipped, not text-extracted -- see fix #11.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("edgar_ingest_worker")

# ---------------------------------------------------------------------------
# Configuration — edit before running for real.
# ---------------------------------------------------------------------------

# REQUIRED by SEC's fair-access policy: identify yourself, don't ship a
# placeholder. Format SEC asks for: "Sample Company Name AdminContact@sample.com"
USER_AGENT = "DiffusionExperiment-PersonalResearch REPLACE_WITH_YOUR_EMAIL@example.com"

# Self-imposed ceiling, well under SEC's published 10 req/sec limit.
MAX_REQUESTS_PER_SECOND = 2.0
MIN_SECONDS_BETWEEN_REQUESTS = 1.0 / MAX_REQUESTS_PER_SECOND

TARGET_FORMS = {"8-K", "8-K/A"}  # amendments matter -- they're exactly what event_versions exists to represent

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
# in every live example seen). See the module docstring for the limitation
# this leaves: an exhibit filed ONLY as PDF is not ingested at all yet.
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

DB_DSN = "dbname=diffusion_experiment"


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


POLL_INTERVAL_SECONDS = 15 * 60


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
    this is raised instead so poll_once's existing per-filing rollback
    handles it the same way as any other ingestion failure -- visibly, and
    retried on the next poll."""


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
        three review rounds -- see the module docstring's revision notes."""
        cik_int = str(int(cik))
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/"
               f"{accession_with_dashes}-index-headers.html")
        return self.get(url).text

    def get_document_text(self, cik: str, accession_no_dashes: str, filename: str) -> str:
        cik_int = str(int(cik))
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{filename}")
        return self.get(url).text


# ---------------------------------------------------------------------------
# Ingestion logic
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
                "this pipeline has no PDF-text-extraction step (see the module "
                "docstring), so it cannot safely mark this filing as fully ingested."
            )
        for filename, _sequence in binary:
            log.info("Skipping relevant exhibit %s (%s) -- binary format not text-extracted by "
                      "this pipeline; a non-binary copy of the same exhibit type is present "
                      "in this filing (see fix #11 in the module docstring)", filename, item_type)
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


def insert_raw_document(conn, cik: str, filing: Filing, doc: FilingDocument, raw_text: str, observed_at: datetime) -> str:
    """Inserts one raw_documents row (primary or exhibit) — does NOT link it
    to a catalyst, since the primary document's insert has to happen BEFORE
    a catalyst can exist (catalysts.originating_document_id references it).

    Identity is (sec_accession_number, sec_document_sequence) -- NOT
    document_component/content_hash. A review round found a real, live 8-K
    with two EX-99.1 documents (an .htm and a .pdf copy of the same
    exhibit), which would collide on the OLD (accession, document_component)
    identity and be permanently un-ingestable; SEC's own per-document
    SEQUENCE is guaranteed unique within one filing package (fix #10).
    content_hash is still recorded and checked, but only to flag a likely
    duplicate, never to skip a distinct filing/component.

    observed_at is passed in from ingest_filing (one shared value per
    filing package) rather than computed fresh here per document, so
    primary/EX-99.1/EX-99.2 etc. don't drift by a few seconds relative to
    each other despite being discovered in the same polling event (fix #16)."""
    accession_no_dashes = filing.accession_number.replace("-", "")
    src_url = source_url_for(cik, accession_no_dashes, doc.filename)
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    duplicate_of = find_content_duplicate(conn, content_hash)

    # Conservative, honest timestamp choice (fix #3): SEC's own acceptance
    # time is NOT treated as when the document became public.
    sec_acceptance_at = datetime.fromisoformat(filing.acceptance_datetime.replace("Z", "+00:00"))

    # Fix #9: precision must be at least the poll interval, AND at least as
    # wide as the actual observed gap since acceptance (honest for backfill
    # runs that discover a filing long after it was accepted).
    observed_uncertainty = max(timedelta(seconds=POLL_INTERVAL_SECONDS), observed_at - sec_acceptance_at)

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
                # Fix #8: source_published_at is NOT sec_acceptance_at --
                # SEC's submissions API gives no genuine source-stated
                # publication timestamp.
                None, sec_acceptance_at, observed_at, observed_at,
                observed_at, "ingestion_poll_observed",
                observed_uncertainty,
            ),
        )
        document_id = cur.fetchone()[0]

    if duplicate_of:
        log.info("Ingested %s/%s (seq %s) -- flagged as content-duplicate of %s (kept as its own occurrence)",
                  filing.accession_number, doc.component_label, doc.sequence, duplicate_of)
    return document_id


def link_to_catalyst(conn, catalyst_id: str, document_id: str, role: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalyst_documents (catalyst_id, document_id, document_role) VALUES (%s, %s, %s)",
            (catalyst_id, document_id, role),
        )


def ingest_filing(conn, client: EdgarClient, cik: str, filing: Filing, entity_id: str | None = None, dry_run: bool = False) -> str | None:
    accession_no_dashes = filing.accession_number.replace("-", "")

    # If we've already recorded ANYTHING for this accession, skip the whole
    # filing -- no need to re-fetch the index. Safe to check existence
    # alone (rather than specifically the primary document) now that
    # ingestion is all-or-nothing per filing (fix #14): there is no
    # "partially ingested" state left to worry about missing.
    if accession_already_ingested(conn, filing.accession_number):
        return None

    if dry_run:
        # A prior version of this function wrote a placeholder INTO the
        # database here and committed it -- which then permanently blocked
        # the real filing, since the "already ingested" check keyed off the
        # same identity either way. Fixed: dry-run now fetches metadata
        # (to prove connectivity/parsing work) and logs intent, but writes
        # NOTHING to the database.
        try:
            index_headers_text = client.get_filing_index_headers(
                cik, accession_no_dashes, filing.accession_number,
            )
            docs = list_filing_documents(index_headers_text, filing.primary_document)
        except Exception:
            log.exception("[dry-run] Failed fetching/parsing filing index for %s", filing.accession_number)
            return None
        log.info("[dry-run] Would ingest %s form %s: %d document(s) (%s) -- writing nothing",
                  filing.accession_number, filing.form, len(docs), [d.component_label for d in docs])
        return None

    # Fix #16: one shared observation instant for every document in this
    # filing package, rather than a fresh datetime.now() per document.
    filing_observed_at = datetime.now(timezone.utc)

    index_headers_text = client.get_filing_index_headers(cik, accession_no_dashes, filing.accession_number)
    docs = list_filing_documents(index_headers_text, filing.primary_document)

    # catalysts.originating_document_id is NOT NULL, so the primary
    # document must be inserted first and the catalyst created from it;
    # exhibits are then inserted and linked to that same catalyst.
    primary_doc = next(d for d in docs if d.role == "primary")
    primary_text = client.get_document_text(cik, accession_no_dashes, primary_doc.filename)
    primary_document_id = insert_raw_document(conn, cik, filing, primary_doc, primary_text, filing_observed_at)

    with conn.cursor() as cur:
        # The watchlist already tells us which entity/CIK this filing came
        # from -- recorded directly instead of leaving it for extraction to
        # rediscover from document text later (a review round caught that
        # entity_id was being fetched from the watchlist and then never
        # actually used for anything but an error-log message).
        cur.execute(
            "INSERT INTO catalysts (originating_document_id, issuer_entity_id, issuer_cik) "
            "VALUES (%s, %s, %s) RETURNING catalyst_id",
            (primary_document_id, entity_id, cik),
        )
        catalyst_id = cur.fetchone()[0]
    link_to_catalyst(conn, catalyst_id, primary_document_id, "primary")

    for doc in docs:
        if doc.role == "primary":
            continue
        # Fix #14 (v4 revision note): a prior version caught a failed
        # exhibit fetch here with log+continue, letting the filing commit
        # anyway with that exhibit permanently missing -- the next poll's
        # dedup check would see the accession already present and never
        # retry it. Deliberately NOT caught here any more: this now
        # propagates up to poll_once, whose per-filing handler rolls back
        # the WHOLE filing (nothing has been committed yet -- conn.commit()
        # is only called once, below, after every document succeeds), so
        # the next poll retries the complete filing cleanly instead of
        # quietly keeping a partial one forever.
        exhibit_text = client.get_document_text(cik, accession_no_dashes, doc.filename)
        exhibit_document_id = insert_raw_document(conn, cik, filing, doc, exhibit_text, filing_observed_at)
        link_to_catalyst(conn, catalyst_id, exhibit_document_id, "exhibit")

    conn.commit()
    log.info("Ingested %s form %s -> catalyst_id=%s (%d document(s))",
              filing.accession_number, filing.form, catalyst_id, len(docs))
    return catalyst_id


def poll_once(conn, client: EdgarClient, watchlist: list[tuple[str, str]], dry_run: bool = False):
    """Fix #15 (v4 revision note): no time-based lookback filter any more.
    A review round argued that any acceptance/filing-date cutoff is an
    unnecessary heuristic layered on an already-bounded input -- SEC caps
    each company's submissions 'recent' array generously (see
    list_recent_target_filings), and exact-once correctness has always
    come from accession_already_ingested's database check, never from a
    time window. Scanning every target-form filing every poll and letting
    that check decide what's new removes the "silently missed a
    late-disseminated filing" bug class entirely instead of padding
    around it with a buffer (v3's DISSEMINATION_DELAY_BUFFER, now removed)."""
    for cik, entity_id in watchlist:
        try:
            submissions = client.get_submissions(cik)
            filings = list_recent_target_filings(submissions, TARGET_FORMS)
        except Exception:
            # One company's failure must not take down the whole poll —
            # log it and move on to the rest of the watchlist.
            log.exception("Failed polling CIK %s (entity_id=%s)", cik, entity_id)
            continue

        for filing in filings:
            try:
                ingest_filing(conn, client, cik, filing, entity_id=entity_id, dry_run=dry_run)
            except Exception:
                # A prior version let this exception propagate out of the
                # for-loop entirely, which silently skipped every OTHER new
                # filing for this company until the next poll -- much worse
                # than the per-company isolation above. A failed statement
                # also leaves the shared connection's transaction aborted,
                # which would poison every later query on it unless rolled
                # back here. This is also now what makes ingest_filing
                # all-or-nothing (fix #14): whatever partial work happened
                # for this one filing (e.g. primary + one exhibit before a
                # second exhibit failed) is discarded, not just the
                # triggering statement.
                conn.rollback()
                log.exception("Failed ingesting filing %s for CIK %s (entity_id=%s) -- "
                              "continuing with the rest of this poll's batch",
                              filing.accession_number, cik, entity_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Poll once and exit, instead of looping.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Fetch filing metadata and log what would be ingested, but write nothing to the database.")
    parser.add_argument("--dsn", default=DB_DSN,
                         help="Postgres connection string (default: %(default)r). Point this at a "
                              "disposable database for a small/reviewable dry run instead of the main one.")
    parser.add_argument("--only-ciks", default=None,
                         help="Comma-separated CIKs to scope this invocation to (e.g. '1045810,2488'), "
                              "without touching watchlist_membership itself -- for a small dry run "
                              "against a handful of companies rather than the full watchlist.")
    args = parser.parse_args()

    client = EdgarClient(USER_AGENT)
    conn = psycopg2.connect(args.dsn)
    try:
        watchlist = filter_watchlist(load_watchlist(conn), args.only_ciks)
        if not watchlist:
            log.error("Watchlist is empty (after --only-ciks filtering, if given) — "
                      "run build/seed_entities.py first, or check --only-ciks.")
            sys.exit(1)
        if args.once:
            poll_once(conn, client, watchlist, dry_run=args.dry_run)
        else:
            while True:
                poll_once(conn, client, watchlist, dry_run=args.dry_run)
                time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
