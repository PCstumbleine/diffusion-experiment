"""
Historical Replay Phase 1B (specs/historical-replay-phase1b-implementation-spec-final.md),
Section 8's strict-validator tests. Built from Step 0's real captured
fixtures (build/tests_historical_replay/fixtures/) plus deliberately
malformed derivatives of those same real fixtures -- no synthetic-from-
scratch/placeholder-key payloads anywhere in this file.
"""
import copy
import json
import os

import pytest

from edgar_primitives import Filing
from historical_edgar_ingest import (
    parse_acceptance_datetime_strict,
    parse_filing_date_strict,
    validate_files_descriptor,
    validate_shard_shape,
    list_shard_target_filings,
    UnsupportedSECTimestampFormatError,
    UnsupportedSECSubmissionsShapeError,
    SUPPORTED_HISTORICAL_FORMS,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def real_submissions():
    return load_fixture("step0_apple_submissions_minimal.json")


@pytest.fixture(scope="module")
def real_shard():
    return load_fixture("step0_apple_shard_001_minimal.json")


def make_filing_from_recent(submissions, index):
    recent = submissions["filings"]["recent"]
    return Filing(
        accession_number=recent["accessionNumber"][index],
        form=recent["form"][index],
        filing_date=recent["filingDate"][index],
        acceptance_datetime=recent["acceptanceDateTime"][index],
        primary_document=recent["primaryDocument"][index],
    )


# ===========================================================================
# parse_acceptance_datetime_strict -- real fixture, valid case
# ===========================================================================

def test_parse_acceptance_datetime_strict_accepts_every_real_recent_value(real_submissions):
    """Every real acceptanceDateTime value in the captured fixture parses
    without raising -- not a single synthetic value."""
    recent = real_submissions["filings"]["recent"]
    for i in range(len(recent["form"])):
        filing = make_filing_from_recent(real_submissions, i)
        result = parse_acceptance_datetime_strict(filing)
        assert result.tzinfo is not None


def test_parse_acceptance_datetime_strict_accepts_every_real_shard_value(real_shard):
    for i in range(len(real_shard["form"])):
        filing = Filing(
            accession_number=real_shard["accessionNumber"][i],
            form=real_shard["form"][i],
            filing_date=real_shard["filingDate"][i],
            acceptance_datetime=real_shard["acceptanceDateTime"][i],
            primary_document=real_shard["primaryDocument"][i],
        )
        result = parse_acceptance_datetime_strict(filing)
        assert result.tzinfo is not None


# ===========================================================================
# parse_acceptance_datetime_strict -- malformed derivatives of the real value
# ===========================================================================

def _filing_with_acceptance(real_submissions, acceptance_value):
    filing = make_filing_from_recent(real_submissions, 0)
    filing.acceptance_datetime = acceptance_value
    return filing


def test_acceptance_datetime_missing_offset_raises(real_submissions):
    """A real value with the trailing 'Z' stripped -- no explicit
    timezone/offset information at all."""
    real_value = real_submissions["filings"]["recent"]["acceptanceDateTime"][0]
    assert real_value.endswith("Z")
    malformed = real_value[:-1]  # strip the 'Z'
    with pytest.raises(UnsupportedSECTimestampFormatError):
        parse_acceptance_datetime_strict(_filing_with_acceptance(real_submissions, malformed))


def test_acceptance_datetime_wrong_type_raises(real_submissions):
    with pytest.raises(UnsupportedSECTimestampFormatError):
        parse_acceptance_datetime_strict(_filing_with_acceptance(real_submissions, None))
    with pytest.raises(UnsupportedSECTimestampFormatError):
        parse_acceptance_datetime_strict(_filing_with_acceptance(real_submissions, 12345))


def test_acceptance_datetime_wrong_millisecond_digit_count_raises(real_submissions):
    """A real value with its 3-digit millisecond field truncated to 2
    digits -- structurally close to real, but not the exact form."""
    real_value = real_submissions["filings"]["recent"]["acceptanceDateTime"][0]
    # e.g. "2026-08-29T16:32:11.000Z" -> "2026-08-29T16:32:11.00Z"
    malformed = real_value.replace(".000Z", ".00Z")
    assert malformed != real_value
    with pytest.raises(UnsupportedSECTimestampFormatError):
        parse_acceptance_datetime_strict(_filing_with_acceptance(real_submissions, malformed))


def test_acceptance_datetime_missing_key_shape_raises_via_none(real_submissions):
    """Mirrors the real 'acceptance is None' case list_recent_target_filings
    already skips upstream -- but if a Filing somehow carries None here
    directly, the strict validator itself must still reject it, not treat
    None as a legitimate value."""
    with pytest.raises(UnsupportedSECTimestampFormatError):
        parse_acceptance_datetime_strict(_filing_with_acceptance(real_submissions, None))


# ===========================================================================
# parse_filing_date_strict
# ===========================================================================

def test_parse_filing_date_strict_accepts_every_real_value(real_submissions):
    recent = real_submissions["filings"]["recent"]
    for i in range(len(recent["form"])):
        filing = make_filing_from_recent(real_submissions, i)
        result = parse_filing_date_strict(filing)
        assert result.isoformat() == recent["filingDate"][i]


def test_parse_filing_date_strict_wrong_type_raises(real_submissions):
    filing = make_filing_from_recent(real_submissions, 0)
    filing.filing_date = None
    with pytest.raises(UnsupportedSECTimestampFormatError):
        parse_filing_date_strict(filing)


def test_parse_filing_date_strict_malformed_value_raises(real_submissions):
    real_value = real_submissions["filings"]["recent"]["filingDate"][0]
    filing = make_filing_from_recent(real_submissions, 0)
    filing.filing_date = real_value.replace("-", "/")  # e.g. "2026/08/29"
    with pytest.raises(UnsupportedSECTimestampFormatError):
        parse_filing_date_strict(filing)


# ===========================================================================
# filings.files descriptor validation
# ===========================================================================

def test_validate_files_descriptor_accepts_the_real_entry(real_submissions):
    descriptor = real_submissions["filings"]["files"][0]
    validate_files_descriptor(descriptor, cik="0000320193")  # must not raise


def test_validate_files_descriptor_missing_key_raises(real_submissions):
    descriptor = copy.deepcopy(real_submissions["filings"]["files"][0])
    del descriptor["name"]
    with pytest.raises(UnsupportedSECSubmissionsShapeError):
        validate_files_descriptor(descriptor, cik="0000320193")


def test_validate_files_descriptor_wrong_type_raises(real_submissions):
    descriptor = copy.deepcopy(real_submissions["filings"]["files"][0])
    descriptor["filingCount"] = str(descriptor["filingCount"])  # int -> str
    with pytest.raises(UnsupportedSECSubmissionsShapeError):
        validate_files_descriptor(descriptor, cik="0000320193")


def test_validate_files_descriptor_not_a_dict_raises():
    with pytest.raises(UnsupportedSECSubmissionsShapeError):
        validate_files_descriptor("not a dict", cik="0000320193")


# ===========================================================================
# Shard shape validation
# ===========================================================================

def test_validate_shard_shape_accepts_the_real_shard(real_shard):
    validate_shard_shape(real_shard, cik="0000320193")  # must not raise


def test_validate_shard_shape_missing_key_raises(real_shard):
    shard = copy.deepcopy(real_shard)
    del shard["acceptanceDateTime"]
    with pytest.raises(UnsupportedSECSubmissionsShapeError):
        validate_shard_shape(shard, cik="0000320193")


def test_validate_shard_shape_wrong_type_raises(real_shard):
    shard = copy.deepcopy(real_shard)
    shard["form"] = "not a list"
    with pytest.raises(UnsupportedSECSubmissionsShapeError):
        validate_shard_shape(shard, cik="0000320193")


def test_validate_shard_shape_mismatched_lengths_raises(real_shard):
    shard = copy.deepcopy(real_shard)
    shard["form"] = shard["form"][:-1]  # one shorter than the rest
    with pytest.raises(UnsupportedSECSubmissionsShapeError):
        validate_shard_shape(shard, cik="0000320193")


def test_validate_shard_shape_not_a_dict_raises():
    with pytest.raises(UnsupportedSECSubmissionsShapeError):
        validate_shard_shape(["not", "a", "dict"], cik="0000320193")


# ===========================================================================
# list_shard_target_filings -- extraction from the real shard
# ===========================================================================

def test_list_shard_target_filings_extracts_real_8k_and_8ka(real_shard):
    filings = list_shard_target_filings(real_shard, SUPPORTED_HISTORICAL_FORMS, cik="0000320193")
    forms_found = {f.form for f in filings}
    assert forms_found == {"8-K", "8-K/A"}
    # The real 8-K/A entry captured (accession 0000320193-96-000025).
    assert any(f.accession_number == "0000320193-96-000025" for f in filings)


def test_list_shard_target_filings_excludes_non_target_forms(real_shard):
    filings = list_shard_target_filings(real_shard, SUPPORTED_HISTORICAL_FORMS, cik="0000320193")
    assert all(f.form in SUPPORTED_HISTORICAL_FORMS for f in filings)
    # The real fixture also contains "4", "424B2", "FWP", "UPLOAD", "S-8",
    # "10-Q" rows -- confirm those are genuinely excluded, not silently let through.
    assert len(filings) < len(real_shard["form"])
