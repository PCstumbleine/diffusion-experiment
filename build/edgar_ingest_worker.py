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
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

import db_config
import edgar_primitives
from edgar_primitives import (
    EdgarClient, FilingPackageParseError, Filing, FilingDocument,
    list_recent_target_filings, list_filing_documents, source_url_for,
    find_existing_document, accession_already_ingested, find_content_duplicate,
    load_watchlist, filter_watchlist, link_to_catalyst,
)
# ^ Historical Replay Phase 1B (spec Section 4.1): moved verbatim into
# edgar_primitives.py, imported/re-exported here so nothing else in this
# file -- or any existing importer of this module, e.g.
# tests/test_edgar_ingest_worker.py -- needs to change.

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
# The checked-in default stays a placeholder on purpose -- a real contact
# email is read from EDGAR_USER_AGENT at runtime instead, so it never lands
# in git history.
USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "DiffusionExperiment-PersonalResearch REPLACE_WITH_YOUR_EMAIL@example.com",
)

TARGET_FORMS = {"8-K", "8-K/A"}  # amendments matter -- they're exactly what event_versions exists to represent

POLL_INTERVAL_SECONDS = 15 * 60

DB_DSN = db_config.get_db_dsn()  # standardized default changed (Historical Replay
    # Phase 0, specs/historical-replay-phase0-implementation-spec-final.md
    # Section 1) -- this was previously the bare "dbname=diffusion_experiment"
    # literal, untested; now matches extraction_runner.py/seed_entities.py/the
    # whole test suite unless DIFFUSION_DB_DSN overrides it.


def parse_acceptance_datetime(filing: Filing) -> datetime:
    """SEC's own acceptanceDateTime, parsed once, consistently -- shared by
    insert_raw_document (sec_acceptance_at) and poll_once's --since filter
    below, so the two never drift apart on how they read the same field.
    Live's existing LENIENT parser -- unchanged by Historical Replay Phase
    1B; historical acquisition uses its own strict validator
    (historical_edgar_ingest.parse_acceptance_datetime_strict) instead."""
    return datetime.fromisoformat(filing.acceptance_datetime.replace("Z", "+00:00"))


def insert_raw_document(conn, cik: str, filing: Filing, doc: FilingDocument, raw_text: str, observed_at: datetime) -> str:
    """Thin LIVE-specific wrapper (Historical Replay Phase 1B, spec Section
    4.1): computes observed-time provenance, then delegates to the neutral
    persistence primitive edgar_primitives.insert_raw_document_record.

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
    # Conservative, honest timestamp choice (fix #3): SEC's own acceptance
    # time is NOT treated as when the document became public.
    sec_acceptance_at = parse_acceptance_datetime(filing)

    # Fix #9: precision must be at least the poll interval, AND at least as
    # wide as the actual observed gap since acceptance (honest for backfill
    # runs that discover a filing long after it was accepted).
    observed_uncertainty = max(timedelta(seconds=POLL_INTERVAL_SECONDS), observed_at - sec_acceptance_at)

    return edgar_primitives.insert_raw_document_record(
        conn, cik, filing, doc, raw_text,
        sec_acceptance_at=sec_acceptance_at,
        canonical_first_public_at=observed_at,
        first_public_timestamp_source="ingestion_poll_observed",
        first_public_timestamp_precision=observed_uncertainty,
        # Fix #8: source_published_at is NOT sec_acceptance_at -- SEC's
        # submissions API gives no genuine source-stated publication
        # timestamp (handled inside insert_raw_document_record itself).
        source_observed_at=observed_at,
        ingested_at=observed_at,
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


def poll_once(conn, client: EdgarClient, watchlist: list[tuple[str, str]], dry_run: bool = False,
              since: datetime | None = None, max_new_filings_per_company: int | None = None):
    """Fix #15 (v4 revision note): no AUTOMATIC time-based lookback filter.
    A review round argued that any acceptance/filing-date cutoff is an
    unnecessary heuristic layered on an already-bounded input -- SEC caps
    each company's submissions 'recent' array generously (see
    list_recent_target_filings), and exact-once correctness has always
    come from accession_already_ingested's database check, never from a
    time window. Scanning every target-form filing every poll and letting
    that check decide what's new removes the "silently missed a
    late-disseminated filing" bug class entirely instead of padding
    around it with a buffer (v3's DISSEMINATION_DELAY_BUFFER, now removed).
    That reasoning still holds for the DEFAULT (both args None below) --
    this fix does not reinstate hidden filtering.

    since / max_new_filings_per_company (added post-Dry-Run-001: scoping
    a poll to a handful of companies via --only-ciks still pulled in each
    company's ENTIRE historical backlog -- 411 documents for 4 companies
    -- since nothing capped filing volume) are EXPLICIT, operator-supplied
    controls, not automatic heuristics: both default to None (unlimited),
    so ordinary polling behavior is completely unchanged unless a caller
    asks for one of these."""
    for cik, entity_id in watchlist:
        try:
            submissions = client.get_submissions(cik)
            filings = list_recent_target_filings(submissions, TARGET_FORMS)
        except Exception:
            # One company's failure must not take down the whole poll —
            # log it and move on to the rest of the watchlist.
            log.exception("Failed polling CIK %s (entity_id=%s)", cik, entity_id)
            continue

        new_filings_ingested = 0
        for filing in filings:
            if since is not None:
                accepted_at = parse_acceptance_datetime(filing)
                if accepted_at < since:
                    log.info("Skipping filing %s for CIK %s (entity_id=%s) -- accepted %s, "
                              "before --since cutoff %s",
                              filing.accession_number, cik, entity_id,
                              accepted_at.isoformat(), since.isoformat())
                    continue

            if max_new_filings_per_company is not None and new_filings_ingested >= max_new_filings_per_company:
                log.info("Reached --max-new-filings-per-company=%d for CIK %s (entity_id=%s) -- "
                          "stopping further ingestion for this company this invocation. "
                          "Already-ingested filings are unaffected; this only limits how much "
                          "NEW work this one invocation does, not what exists on SEC.",
                          max_new_filings_per_company, cik, entity_id)
                break

            # Checked BEFORE ingest_filing purely so --max-new-filings-per-company
            # can tell a genuinely new filing apart from a dedup no-op even
            # under --dry-run (which always returns None below either way,
            # by design -- see fix #2's own note on ingest_filing -- so the
            # return value alone can't distinguish the two cases there).
            already_ingested = accession_already_ingested(conn, filing.accession_number)

            try:
                catalyst_id = ingest_filing(conn, client, cik, filing, entity_id=entity_id, dry_run=dry_run)
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
                continue

            if not already_ingested:
                new_filings_ingested += 1


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
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                         help="Skip any filing whose SEC acceptance timestamp is before this date "
                              "(UTC midnight). No default cutoff -- omit for normal unlimited polling; "
                              "this is an explicit operator control, not automatic lookback filtering.")
    parser.add_argument("--max-new-filings-per-company", type=int, default=None, metavar="N",
                         help="Stop ingesting NEW filings for a given CIK once N have been ingested in "
                              "this invocation (already-ingested/deduplicated filings never count against "
                              "this). No default cap -- omit for normal unlimited polling.")
    args = parser.parse_args()

    since_dt = None
    if args.since:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    client = EdgarClient(USER_AGENT)
    conn = psycopg2.connect(args.dsn)
    try:
        db_config.assert_database_purpose(conn, "forward")  # Historical Replay Phase 0,
        # Section 3 -- once, immediately after connecting, before any read/write.
        watchlist = filter_watchlist(load_watchlist(conn), args.only_ciks)
        if not watchlist:
            log.error("Watchlist is empty (after --only-ciks filtering, if given) — "
                      "run build/seed_entities.py first, or check --only-ciks.")
            sys.exit(1)
        if args.once:
            poll_once(conn, client, watchlist, dry_run=args.dry_run, since=since_dt,
                      max_new_filings_per_company=args.max_new_filings_per_company)
        else:
            while True:
                poll_once(conn, client, watchlist, dry_run=args.dry_run, since=since_dt,
                          max_new_filings_per_company=args.max_new_filings_per_company)
                time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
