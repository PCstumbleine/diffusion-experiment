"""
historical_edgar_ingest.py -- Historical Replay Phase 1B
(specs/historical-replay-phase1b-implementation-spec-final.md).

Acquires a historical SEC 8-K/8-K/A filing into the replay database as a
complete filing package (raw_documents + catalysts + catalyst_documents)
carrying historical (filing-day civil-day) public-time provenance, mirroring
the shape of edgar_ingest_worker.py's live ingest_filing exactly but never
sharing its module (Section 4's architecture diagram):

                       edgar_primitives.py
                      /                    \\
      edgar_ingest_worker.py          historical_edgar_ingest.py
             LIVE                            HISTORICAL

Phase 1B does NOT proceed past raw_documents/catalysts/catalyst_documents --
no extraction_runs, no extracted_events, no entity_relationships, no
historical eligibility/decision_at/Arm A/Arm G/returns. See spec Section 7.

Step 0 provenance (spec Section 0): the strict validators and the
filings.files shard adapter below are built against real payload fixtures
captured live from SEC's own submissions API for Apple Inc.
(CIK 0000320193, whose filings.files is non-empty) -- see
build/tests_historical_replay/fixtures/. Every acceptanceDateTime value
observed (2246 total, across filings.recent and the CIK0000320193-
submissions-001.json shard) matched the single serialization
'YYYY-MM-DDTHH:MM:SS.mmmZ' with no exceptions; every filingDate matched
'YYYY-MM-DD'; the shard JSON has the identical parallel-array structure as
filings.recent. No field name or format below is invented.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import edgar_primitives
from edgar_primitives import (
    Filing,
    FilingDocument,
    list_recent_target_filings,
    list_filing_documents,
    accession_already_ingested,
    load_watchlist,
    filter_watchlist,
    link_to_catalyst,
)

log = logging.getLogger("historical_edgar_ingest")

# ---------------------------------------------------------------------------
# Section 3, invariant 3: hard form gate -- 8-K/8-K/A only, no general
# "any SEC form" historical timestamp helper (Section 7's non-goals).
# ---------------------------------------------------------------------------

SUPPORTED_HISTORICAL_FORMS = {"8-K", "8-K/A"}


class UnsupportedHistoricalSECFormError(Exception):
    """Raised by ingest_historical_filing as the very FIRST check (spec
    Section 3, invariant 3) when filing.form is not in
    SUPPORTED_HISTORICAL_FORMS -- ALWAYS, unconditionally, even when the
    accession already exists in the database. Never short-circuited by
    accession_already_ingested returning True first: form validation is
    local and costs nothing, and the function's input-validation contract
    cannot depend on database state."""


class UnsupportedSECTimestampFormatError(Exception):
    """Raised by parse_acceptance_datetime_strict / parse_filing_date_strict
    when a raw SEC timestamp/date string does not match one of the exact
    serializations captured directly from a real SEC payload (Step 0) --
    never a fuzzy/best-effort parse (no dateutil-style guessing anywhere in
    this module), and never a silent assumption about an ambiguous
    timezone. Carries the observed value, its type, the expected form(s),
    and the accession number."""


class UnsupportedSECSubmissionsShapeError(Exception):
    """Raised when a filings.files descriptor entry, or the shard JSON it
    references, doesn't match the exact shape captured directly from a
    real SEC submissions.json (Step 0). Carries the observed keys/types and
    the CIK."""


# ---------------------------------------------------------------------------
# Section 2: historical public-time provenance -- filing-day civil-day
# policy (frozen). NOT a claim that EDGAR was actually uncertain for ~24h --
# a deliberately conservative methodological bound. Describe this interval
# as "the frozen conservative historical-replay uncertainty interval
# derived from SEC's authoritative filing day," never as "the exact period
# during which the filing became public."
# ---------------------------------------------------------------------------

SEC_TZ = ZoneInfo("America/New_York")

FIRST_PUBLIC_TIMESTAMP_SOURCE = "sec_filing_date_civil_day_v1"


def historical_public_time_interval(filing_date: date) -> tuple[datetime, timedelta]:
    """Returns (canonical_first_public_at, first_public_timestamp_precision)
    for a supported-form historical filing, per the frozen filing-day
    civil-day v1 policy. NOT a claim that EDGAR was actually uncertain for
    ~24 hours -- it's a deliberately conservative methodological bound:
    SEC gives us the disclosure day with certainty (filingDate is already
    next-business-day-adjusted at the source for after-cutoff submissions),
    but no source -- not the Filer Manual, not the webmaster FAQ, not the
    dissemination spec -- commits to an exact, guaranteed minute within
    that day for either the ordinary or the after-cutoff case.

    Because both bounds come from converting real Eastern local midnights
    through zoneinfo rather than adding a hardcoded timedelta(days=1), the
    resulting precision is honestly DST-aware: an ordinary filing day
    produces exactly 24 hours, the spring-forward transition day produces
    23 hours, and the fall-back transition day produces 25 hours. This is
    INTENTIONAL, not a bug -- it's the width of the actual Eastern civil
    day mapped into UTC. Do not "fix" this into a hardcoded
    timedelta(hours=24)."""
    lower_local = datetime.combine(filing_date, time.min, tzinfo=SEC_TZ)
    upper_local = datetime.combine(filing_date + timedelta(days=1), time.min, tzinfo=SEC_TZ)
    lower_utc = lower_local.astimezone(timezone.utc)
    upper_utc = upper_local.astimezone(timezone.utc)
    return upper_utc, (upper_utc - lower_utc)


# ---------------------------------------------------------------------------
# Section 5: strict runtime validation -- acceptanceDateTime, filingDate.
# Two-stage contract: (1) does the raw value match an explicitly supported
# serialization (established from Step 0's real fixtures)? If not, raise.
# (2) If it matches, does that serialization explicitly carry the semantic
# info needed? For the one real serialization observed here, the trailing
# literal 'Z' explicitly carries UTC semantics -- no ambiguity to resolve.
# ---------------------------------------------------------------------------

# The ONLY acceptanceDateTime serialization observed across 2246 real SEC
# values captured in Step 0 (filings.recent + the CIK0000320193-
# submissions-001.json shard, zero exceptions): 3-digit milliseconds, a
# literal trailing 'Z' (UTC, unambiguous -- stage 2 of the two-stage
# contract is satisfied by construction for this one supported form).
_ACCEPTANCE_DATETIME_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\.(?P<micro>\d{3})Z$"
)

_FILING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_acceptance_datetime_strict(filing: Filing) -> datetime:
    """Used ONLY to populate the audited sec_acceptance_at field -- NEVER
    to compute canonical_first_public_at (spec Section 2/4.2). Raises
    UnsupportedSECTimestampFormatError for anything not matching the exact
    serialization Step 0 actually captured -- no dateutil-style best-effort
    parsing."""
    value = filing.acceptance_datetime
    if not isinstance(value, str):
        raise UnsupportedSECTimestampFormatError(
            f"acceptanceDateTime for accession {filing.accession_number!r}: expected str, got "
            f"{type(value).__name__} (value={value!r}) -- expected the exact form "
            "'YYYY-MM-DDTHH:MM:SS.mmmZ' (the only serialization observed across 2246 real SEC "
            "acceptanceDateTime values captured in Historical Replay Phase 1B Step 0)."
        )
    match = _ACCEPTANCE_DATETIME_RE.match(value)
    if not match:
        raise UnsupportedSECTimestampFormatError(
            f"acceptanceDateTime for accession {filing.accession_number!r}: value={value!r} does "
            "not match the exact supported serialization 'YYYY-MM-DDTHH:MM:SS.mmmZ' (the only "
            "form observed across 2246 real SEC acceptanceDateTime values captured in Historical "
            "Replay Phase 1B Step 0 -- no fuzzy/best-effort parsing is attempted)."
        )
    return datetime(
        int(match["year"]), int(match["month"]), int(match["day"]),
        int(match["hour"]), int(match["minute"]), int(match["second"]),
        int(match["micro"]) * 1000,
        tzinfo=timezone.utc,  # the trailing literal 'Z' explicitly carries UTC -- unambiguous
    )


def parse_filing_date_strict(filing: Filing) -> date:
    """filingDate must parse as a valid ISO date; an unparseable value
    raises with the same value/type/accession detail as
    parse_acceptance_datetime_strict (spec Section 5)."""
    value = filing.filing_date
    if not isinstance(value, str):
        raise UnsupportedSECTimestampFormatError(
            f"filingDate for accession {filing.accession_number!r}: expected str, got "
            f"{type(value).__name__} (value={value!r})."
        )
    if not _FILING_DATE_RE.match(value):
        raise UnsupportedSECTimestampFormatError(
            f"filingDate for accession {filing.accession_number!r}: value={value!r} does not "
            "match the expected 'YYYY-MM-DD' form."
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UnsupportedSECTimestampFormatError(
            f"filingDate for accession {filing.accession_number!r}: value={value!r} is not a "
            f"valid ISO date ({exc})."
        ) from exc


# ---------------------------------------------------------------------------
# Section 6: filings.files -- older-history shard access.
# ---------------------------------------------------------------------------

# The exact filings.files descriptor shape observed in Step 0's real Apple
# Inc. (CIK 0000320193) fixture:
#   {"name": "CIK0000320193-submissions-001.json", "filingCount": 1246,
#    "filingFrom": "1994-01-26", "filingTo": "2015-07-20"}
_REQUIRED_FILES_DESCRIPTOR_KEYS_AND_TYPES = {
    "name": str,
    "filingCount": int,
    "filingFrom": str,
    "filingTo": str,
}

# The real shard JSON (fetched from https://data.sec.gov/submissions/<name>)
# has the IDENTICAL parallel-array structure as filings.recent -- confirmed
# directly in Step 0 (same key names, same per-index alignment). Only the
# keys this module actually consumes are validated here; the shard also
# carries many more keys (reportDate, act, fileNumber, filmNumber, items,
# core_type, size, isXBRL, isInlineXBRL, isXBRLNumeric,
# primaryDocDescription) that this module has no use for and does not
# validate.
_REQUIRED_SHARD_KEYS_AND_TYPES = {
    "accessionNumber": list,
    "filingDate": list,
    "acceptanceDateTime": list,
    "form": list,
    "primaryDocument": list,
}


def validate_files_descriptor(descriptor, cik: str) -> None:
    if not isinstance(descriptor, dict):
        raise UnsupportedSECSubmissionsShapeError(
            f"CIK {cik}: filings.files entry is not a dict (got {type(descriptor).__name__}): "
            f"{descriptor!r}"
        )
    missing = [k for k in _REQUIRED_FILES_DESCRIPTOR_KEYS_AND_TYPES if k not in descriptor]
    if missing:
        raise UnsupportedSECSubmissionsShapeError(
            f"CIK {cik}: filings.files entry missing required key(s) {missing} -- observed keys: "
            f"{sorted(descriptor.keys())}"
        )
    wrong_type = [
        k for k, t in _REQUIRED_FILES_DESCRIPTOR_KEYS_AND_TYPES.items()
        if not isinstance(descriptor[k], t)
    ]
    if wrong_type:
        raise UnsupportedSECSubmissionsShapeError(
            f"CIK {cik}: filings.files entry key(s) {wrong_type} have unexpected type(s) -- "
            f"observed: {[(k, type(descriptor[k]).__name__) for k in wrong_type]}"
        )


def validate_shard_shape(shard, cik: str) -> None:
    if not isinstance(shard, dict):
        raise UnsupportedSECSubmissionsShapeError(
            f"CIK {cik}: shard JSON is not a dict (got {type(shard).__name__})"
        )
    missing = [k for k in _REQUIRED_SHARD_KEYS_AND_TYPES if k not in shard]
    if missing:
        raise UnsupportedSECSubmissionsShapeError(
            f"CIK {cik}: shard JSON missing required key(s) {missing} -- observed keys: "
            f"{sorted(shard.keys())}"
        )
    wrong_type = [k for k, t in _REQUIRED_SHARD_KEYS_AND_TYPES.items() if not isinstance(shard[k], t)]
    if wrong_type:
        raise UnsupportedSECSubmissionsShapeError(
            f"CIK {cik}: shard JSON key(s) {wrong_type} have unexpected type(s) -- observed: "
            f"{[(k, type(shard[k]).__name__) for k in wrong_type]}"
        )
    lengths = {k: len(shard[k]) for k in _REQUIRED_SHARD_KEYS_AND_TYPES}
    if len(set(lengths.values())) > 1:
        raise UnsupportedSECSubmissionsShapeError(
            f"CIK {cik}: shard JSON parallel arrays have mismatched lengths: {lengths}"
        )


def list_shard_target_filings(shard, target_forms: set[str], cik: str) -> list[Filing]:
    """Validates the shard's shape first (validate_shard_shape), then
    extracts target-form Filing objects -- mirrors
    list_recent_target_filings' own per-index extraction exactly, since
    the shard has the identical structure (confirmed in Step 0)."""
    validate_shard_shape(shard, cik)
    forms = shard["form"]
    out: list[Filing] = []
    for i, form in enumerate(forms):
        if form not in target_forms:
            continue
        accession = shard["accessionNumber"][i]
        filing_date = shard["filingDate"][i]
        acceptance = shard["acceptanceDateTime"][i]
        primary_doc = shard["primaryDocument"][i]
        if acceptance is None:
            continue
        out.append(Filing(accession, form, filing_date, acceptance, primary_doc))
    return out


def enumerate_historical_filings(client, cik: str, target_forms: set[str] = SUPPORTED_HISTORICAL_FORMS) -> list[Filing]:
    """Historical filing enumeration (spec Section 4.2): filings.recent
    (list_recent_target_filings, imported unchanged from edgar_primitives)
    PLUS every filings.files shard, each validated strictly before
    extraction. Does NOT assume filings.recent and filings.files shards are
    non-overlapping or contiguous (spec Section 3, invariant 7) -- relies
    on accession_already_ingested for correctness, exactly like live
    relies on it for its own dedup."""
    submissions = client.get_submissions(cik)
    filings = list(list_recent_target_filings(submissions, target_forms))

    for descriptor in submissions.get("filings", {}).get("files", []):
        validate_files_descriptor(descriptor, cik)
        shard_url = f"https://data.sec.gov/submissions/{descriptor['name']}"
        shard = client.get(shard_url).json()
        filings.extend(list_shard_target_filings(shard, target_forms, cik))
    return filings


# ---------------------------------------------------------------------------
# Section 4.2: utc_now() seam, historical wrapper, ingest_historical_filing.
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """A trivial, deliberate seam -- used everywhere this module needs the
    real current instant, so tests can monkeypatch it for deterministic
    timestamp assertions instead of relying on real wall-clock timing."""
    return datetime.now(timezone.utc)


def insert_historical_raw_document(
    conn, cik: str, filing: Filing, doc: FilingDocument, raw_text: str, observed_at: datetime,
) -> str:
    """Thin HISTORICAL-specific wrapper, mirroring live's
    edgar_ingest_worker.insert_raw_document exactly in shape: computes the
    civil-day provenance triple (Section 2) -- a PURE function of
    filing.filing_date, so recomputing it per document call is harmless
    and produces the identical triple every time, exactly like live's
    parse_acceptance_datetime(filing) call inside insert_raw_document --
    then delegates to the neutral persistence primitive
    (edgar_primitives.insert_raw_document_record).

    observed_at is passed in from ingest_historical_filing (one shared
    value per filing package, obtained via exactly ONE utc_now() call) --
    mirrors live's fix #16 exactly (spec Section 3, invariant 2)."""
    filing_date = parse_filing_date_strict(filing)
    canonical_first_public_at, first_public_timestamp_precision = historical_public_time_interval(filing_date)
    # Audited raw fact ONLY -- never used to compute canonical/precision above.
    sec_acceptance_at = parse_acceptance_datetime_strict(filing)

    return edgar_primitives.insert_raw_document_record(
        conn, cik, filing, doc, raw_text,
        sec_acceptance_at=sec_acceptance_at,
        canonical_first_public_at=canonical_first_public_at,
        first_public_timestamp_source=FIRST_PUBLIC_TIMESTAMP_SOURCE,
        first_public_timestamp_precision=first_public_timestamp_precision,
        source_observed_at=observed_at,
        ingested_at=observed_at,
    )


def ingest_historical_filing(conn, client, cik: str, filing: Filing, entity_id: str | None = None) -> str | None:
    """Historical Replay Phase 1B, spec Section 4.2. Internal ordering is
    frozen exactly as specified: form gate first (unconditional, never
    short-circuited by an existing accession -- invariant 3), then the
    idempotence check (invariant 6, self-protecting inside this function),
    then a single fresh utc_now() call shared by every document in this
    filing's package (invariant 2), then fetch/insert/link mirroring
    ingest_filing's shape exactly, then exactly one conn.commit() at the
    end (invariant 5).

    This function NEVER calls conn.rollback() itself -- that is the
    caller's job, exactly like edgar_ingest_worker.poll_once's
    try/except Exception: conn.rollback(); continue around ingest_filing.
    Whatever drives historical acquisition per company must use that same
    pattern."""
    if filing.form not in SUPPORTED_HISTORICAL_FORMS:
        raise UnsupportedHistoricalSECFormError(
            f"form={filing.form!r} (accession {filing.accession_number!r}) is not a supported "
            f"historical form -- only {sorted(SUPPORTED_HISTORICAL_FORMS)} are supported."
        )

    if accession_already_ingested(conn, filing.accession_number):
        return None

    # ONE instant, shared by every document in THIS filing's package
    # (mirrors live fix #16) -- a later call for a later filing in the same
    # backfill run naturally gets a later instant, since utc_now() is
    # called fresh per call, not once for the whole run.
    historical_observed_at = utc_now()

    accession_no_dashes = filing.accession_number.replace("-", "")
    index_headers_text = client.get_filing_index_headers(cik, accession_no_dashes, filing.accession_number)
    docs = list_filing_documents(index_headers_text, filing.primary_document)

    # catalysts.originating_document_id is NOT NULL, so the primary
    # document must be inserted first and the catalyst created from it;
    # exhibits are then inserted and linked to that same catalyst --
    # mirrors ingest_filing's shape exactly.
    primary_doc = next(d for d in docs if d.role == "primary")
    primary_text = client.get_document_text(cik, accession_no_dashes, primary_doc.filename)
    primary_document_id = insert_historical_raw_document(
        conn, cik, filing, primary_doc, primary_text, historical_observed_at,
    )

    with conn.cursor() as cur:
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
        exhibit_text = client.get_document_text(cik, accession_no_dashes, doc.filename)
        exhibit_document_id = insert_historical_raw_document(
            conn, cik, filing, doc, exhibit_text, historical_observed_at,
        )
        link_to_catalyst(conn, catalyst_id, exhibit_document_id, "exhibit")

    conn.commit()  # the ONLY commit; no internal rollback on failure (invariant 5)
                   # -- that is the CALLER's responsibility, exactly like
                   # poll_once's try/except conn.rollback() around ingest_filing.
    log.info("Ingested historical filing %s form %s -> catalyst_id=%s (%d document(s))",
              filing.accession_number, filing.form, catalyst_id, len(docs))
    return catalyst_id
