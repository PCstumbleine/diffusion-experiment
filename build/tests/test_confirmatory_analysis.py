"""
Section 8 Implementation Spec, FINAL (specs/section8-implementation-spec-final.md),
Section 10's required tests -- the catalyst-wide trade/abstention
invariant, the stable_entity_sort_key tie-break, abstention's contribution
to the statistical comparison (at the level of what this pass actually
implements: assert_decision_set_ready_for_comparison's per-arm status
reporting -- D_c/R_arm,c computation itself belongs to the not-yet-built
Section 7f statistical builder, out of scope here), and outcome validation.
"""
import math
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_entity

import confirmatory_analysis as ca
from confirmatory_analysis import (
    get_current_event_versions_for_catalyst,
    assert_trade_abstention_invariant,
    assert_decision_set_ready_for_comparison,
    stable_entity_sort_key,
    assert_outcome_ready_for_confirmation,
    assert_confirmatory_configuration_complete,
    CatalystDecisionResult,
    NO_ELIGIBLE_CANDIDATES,
    TRADED,
    ABSTAINED,
    InvalidStableEntityKeyError,
    UnresolvedRankingTieError,
    InvalidCandidateScoreError,
    TradeAbstentionInvariantError,
    ConfirmatoryConfigurationIncompleteError,
    OutcomeIntegrityError,
    QuoteUnobservableError,
    MissingDecisionError,
)

MODEL_A = "arm_a_llm"
MODEL_G = "arm_g_mechanical"
MODEL_VERSION = "v1"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def make_entity_with_cik(conn, name, cik):
    eid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (entity_id, legal_name, cik) VALUES (%s, %s, %s)",
            (eid, name, cik),
        )
    return eid


def make_catalyst(conn, issuer_entity_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_documents (source_name, document_type, raw_content, content_hash) "
            "VALUES ('test', '8-K', 'test content', %s) RETURNING document_id",
            (str(uuid.uuid4()),),
        )
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO catalysts (originating_document_id, issuer_entity_id) VALUES (%s, %s) "
            "RETURNING catalyst_id",
            (doc_id, issuer_entity_id),
        )
        return cur.fetchone()[0]


def make_event_version_under_catalyst(conn, catalyst_id, superseded_by=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO canonical_events (catalyst_id, event_category) VALUES (%s, 'guidance_revision') "
            "RETURNING canonical_event_id",
            (catalyst_id,),
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO event_versions (canonical_event_id, version_number, decision_at, superseded_by) "
            "VALUES (%s, 1, now(), %s) RETURNING event_version_id",
            (event_id, superseded_by),
        )
        return cur.fetchone()[0]


def make_event_version(conn, entity_id=None):
    """Single event_version under a fresh catalyst -- most tests only need one."""
    catalyst_id = make_catalyst(conn, entity_id)
    return catalyst_id, make_event_version_under_catalyst(conn, catalyst_id)


def make_candidate(conn, event_version_id, entity_id, eligibility_status="eligible"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO candidate_signals (event_version_id, entity_id, eligibility_status, "
            "policy_version, decision_timestamp) VALUES (%s, %s, %s, 'v1', now()) RETURNING candidate_id",
            (event_version_id, entity_id, eligibility_status),
        )
        return cur.fetchone()[0]


def record_decision(conn, candidate_id, model_id, score, rank, selected, abstained,
                     model_version=MODEL_VERSION):
    """score may be a normal number, or one of the strings "NaN"/"Infinity"/
    "-Infinity" -- psycopg2's Decimal adapter silently collapses a bound
    Decimal('Infinity')/Decimal('-Infinity') parameter to the literal 'NaN'
    (a confirmed, real psycopg2 quirk, checked directly against this
    database), so those three values must be written as a raw SQL literal
    cast instead of a bound parameter to actually land in the column."""
    if isinstance(score, str) and score in ("NaN", "Infinity", "-Infinity"):
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO model_candidate_decisions "
                f"(candidate_id, model_id, model_version, score, rank, selected, abstained, decision_at) "
                f"VALUES (%s, %s, %s, '{score}'::numeric, %s, %s, %s, now())",
                (candidate_id, model_id, model_version, rank, selected, abstained),
            )
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_candidate_decisions "
            "(candidate_id, model_id, model_version, score, rank, selected, abstained, decision_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, now())",
            (candidate_id, model_id, model_version, score, rank, selected, abstained),
        )


def make_instrument(conn, entity_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instruments (entity_id, exchange) VALUES (%s, 'TEST') RETURNING instrument_id",
            (entity_id,),
        )
        return cur.fetchone()[0]


def make_arm(conn, arm_code):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO experiments (name, scoring_epoch, cohort_type) "
            "VALUES ('t', 'e1', 'confirmatory') RETURNING experiment_id"
        )
        experiment_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO experiment_arms (experiment_id, arm_code, arm_label) VALUES (%s, %s, 't') "
            "RETURNING arm_id",
            (experiment_id, arm_code),
        )
        return cur.fetchone()[0]


def make_quote(conn, instrument_id, bid, ask, quote_timestamp, provider="test_provider",
               session="regular", staleness=1.0, bid_size=None, ask_size=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO quote_snapshots (instrument_id, bid, ask, bid_size, ask_size, "
            "quote_timestamp, data_provider, trading_session, staleness_seconds) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING quote_snapshot_id",
            (instrument_id, bid, ask, bid_size, ask_size, quote_timestamp, provider, session, staleness),
        )
        return cur.fetchone()[0]


def make_entry(conn, arm_id, event_version_id, instrument_id, entry_timestamp, entry_quote_snapshot_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO arm_entries (arm_id, event_version_id, instrument_id, entry_timestamp, "
            "entry_quote_snapshot_id, notional, direction) VALUES (%s, %s, %s, %s, %s, 100.0, 'long') "
            "RETURNING entry_id",
            (arm_id, event_version_id, instrument_id, entry_timestamp, entry_quote_snapshot_id),
        )
        return cur.fetchone()[0]


def make_outcome(conn, entry_id, exit_timestamp, exit_quote_snapshot_id, entry_fee, exit_fee,
                 return_gross, return_net, exit_reason="horizon",
                 entry_price_source="nbbo_side_proxy", exit_price_source="nbbo_side_proxy",
                 return_method_version="shadow_nbbo_log_v1", fee_method_version="test_fee_v1",
                 horizon_label="1day"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arm_outcomes (
                entry_id, horizon_label, exit_timestamp, exit_quote_snapshot_id,
                return_gross, return_net_of_costs,
                entry_price_source, exit_price_source, entry_fee, exit_fee,
                return_method_version, fee_method_version, exit_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING outcome_id
            """,
            (entry_id, horizon_label, exit_timestamp, exit_quote_snapshot_id,
             return_gross, return_net, entry_price_source, exit_price_source, entry_fee, exit_fee,
             return_method_version, fee_method_version, exit_reason),
        )
        return cur.fetchone()[0]


@pytest.fixture
def configured(monkeypatch):
    """Simulates a fully-configured confirmatory setup so individual OTHER
    mechanics (recomputation, fee mismatch, quote size, provider capability)
    can be tested in isolation from the protective-stop gate -- NOT a claim
    that the real stop-trigger rule exists yet. The dedicated stop-gating
    test below deliberately does NOT use this fixture, to confirm the real
    (current) unconfigured default blocks everything regardless of
    otherwise-perfect data."""
    monkeypatch.setattr(ca, "MAX_QUOTE_LOOKUP_DELAY_SECONDS", 300)
    monkeypatch.setattr(ca, "MAX_QUOTE_STALENESS_SECONDS", 60)
    monkeypatch.setattr(ca, "CONFIRMATORY_DATA_PROVIDER", "test_provider")
    monkeypatch.setattr(ca, "CONFIRMATORY_PROVIDER_SIZE_CAPABLE", True)
    monkeypatch.setattr(ca, "FEE_METHOD_VERSION", "test_fee_v1")
    monkeypatch.setattr(ca, "compute_expected_fees", lambda **kwargs: (1.0, 1.0))
    monkeypatch.setattr(ca, "STOP_RULE_PROVENANCE_AVAILABLE", True)
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD", 2000.0)
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD_PROVENANCE", "preregistered_placeholder")
    return ca


NOW = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)


# ===========================================================================
# get_current_event_versions_for_catalyst
# ===========================================================================

def test_get_current_event_versions_for_catalyst_returns_only_non_superseded(conn):
    catalyst_id = make_catalyst(conn)
    old = make_event_version_under_catalyst(conn, catalyst_id)
    new = make_event_version_under_catalyst(conn, catalyst_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE event_versions SET superseded_by = %s WHERE event_version_id = %s", (new, old))
    result = get_current_event_versions_for_catalyst(conn, catalyst_id)
    assert result == [new]


def test_get_current_event_versions_for_catalyst_spans_multiple_canonical_events(conn):
    """The real Dry Run 002 shape: one catalyst, several canonical_events,
    each with its own event_version."""
    catalyst_id = make_catalyst(conn)
    ev1 = make_event_version_under_catalyst(conn, catalyst_id)
    ev2 = make_event_version_under_catalyst(conn, catalyst_id)
    result = set(get_current_event_versions_for_catalyst(conn, catalyst_id))
    assert result == {ev1, ev2}


# ===========================================================================
# Catalyst-wide invariant (assert_trade_abstention_invariant)
# ===========================================================================

def test_rank_1_score_positive_is_a_trade(conn):
    entity = make_entity(conn, "Co1")
    _catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score=0.9, rank=1, selected=True, abstained=False)

    result = assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)
    assert result == CatalystDecisionResult(status=TRADED, selected_candidate_id=candidate)


def test_rank_1_score_zero_or_negative_is_an_abstain(conn):
    """Freeze > 0, never >= 0 -- a score of exactly 0 counts as abstain."""
    entity = make_entity(conn, "Co2")
    _catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score=0, rank=1, selected=False, abstained=True)

    result = assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)
    assert result == CatalystDecisionResult(status=ABSTAINED)


def test_two_candidates_under_different_canonical_events_both_selected_true_fails_loudly(conn):
    """The real Dry Run 002 shape: ONE catalyst, TWO canonical_events, each
    with its own event_version/candidate -- ranking and trade/abstention
    semantics sit CATALYST-wide, not per event_version. Two selections for
    one catalyst violates max_positions_per_catalyst = 1."""
    entity1, entity2 = make_entity(conn, "Co3a"), make_entity(conn, "Co3b")
    catalyst_id = make_catalyst(conn)
    ev1 = make_event_version_under_catalyst(conn, catalyst_id)
    ev2 = make_event_version_under_catalyst(conn, catalyst_id)
    c1 = make_candidate(conn, ev1, entity1)
    c2 = make_candidate(conn, ev2, entity2)
    record_decision(conn, c1, MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c2, MODEL_A, score=0.5, rank=1, selected=True, abstained=False)

    with pytest.raises(TradeAbstentionInvariantError):
        assert_trade_abstention_invariant(conn, catalyst_id, MODEL_A, MODEL_VERSION)


def test_two_different_candidates_both_selected_true_fails(conn):
    entity1, entity2 = make_entity(conn, "Co4a"), make_entity(conn, "Co4b")
    _catalyst_id, ev = make_event_version(conn)
    c1 = make_candidate(conn, ev, entity1)
    c2 = make_candidate(conn, ev, entity2)
    record_decision(conn, c1, MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c2, MODEL_A, score=0.5, rank=2, selected=True, abstained=False)

    with pytest.raises(TradeAbstentionInvariantError):
        assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)


def test_two_different_candidates_both_abstained_true_fails(conn):
    entity1, entity2 = make_entity(conn, "Co5a"), make_entity(conn, "Co5b")
    _catalyst_id, ev = make_event_version(conn)
    c1 = make_candidate(conn, ev, entity1)
    c2 = make_candidate(conn, ev, entity2)
    record_decision(conn, c1, MODEL_A, score=-0.1, rank=1, selected=False, abstained=True)
    record_decision(conn, c2, MODEL_A, score=-0.5, rank=2, selected=False, abstained=True)

    with pytest.raises(TradeAbstentionInvariantError):
        assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)


def test_a_rank_greater_than_1_candidate_with_selected_true_fails(conn):
    entity1, entity2 = make_entity(conn, "Co6a"), make_entity(conn, "Co6b")
    _catalyst_id, ev = make_event_version(conn)
    c1 = make_candidate(conn, ev, entity1)
    c2 = make_candidate(conn, ev, entity2)
    record_decision(conn, c1, MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    # rank 2, but improperly marked selected=true
    record_decision(conn, c2, MODEL_A, score=0.5, rank=2, selected=True, abstained=False)

    with pytest.raises(TradeAbstentionInvariantError):
        assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)


def test_rank_1_selected_abstained_contradicts_score_sign_fails(conn):
    entity = make_entity(conn, "Co7")
    _catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    # score > 0 but marked as an abstention
    record_decision(conn, candidate, MODEL_A, score=0.9, rank=1, selected=False, abstained=True)

    with pytest.raises(TradeAbstentionInvariantError):
        assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)


def test_gapped_or_duplicated_rank_sequence_fails(conn):
    entity1, entity2, entity3 = make_entity(conn, "Co8a"), make_entity(conn, "Co8b"), make_entity(conn, "Co8c")
    _catalyst_id, ev = make_event_version(conn)
    c1 = make_candidate(conn, ev, entity1)
    c2 = make_candidate(conn, ev, entity2)
    c3 = make_candidate(conn, ev, entity3)
    # Stored ranks 1, 1, 3 -- a duplicate at 1 and a gap at 2.
    record_decision(conn, c1, MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c2, MODEL_A, score=0.5, rank=1, selected=False, abstained=False)
    record_decision(conn, c3, MODEL_A, score=0.1, rank=3, selected=False, abstained=False)

    with pytest.raises(TradeAbstentionInvariantError):
        assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)


def test_score_nan_at_what_would_otherwise_be_rank_1_rejected_before_ranking(conn):
    entity1, entity2 = make_entity(conn, "Co9a"), make_entity(conn, "Co9b")
    _catalyst_id, ev = make_event_version(conn)
    c1 = make_candidate(conn, ev, entity1)
    c2 = make_candidate(conn, ev, entity2)
    record_decision(conn, c1, MODEL_A, score="NaN", rank=1, selected=True, abstained=False)
    record_decision(conn, c2, MODEL_A, score=0.1, rank=2, selected=False, abstained=False)

    with pytest.raises(InvalidCandidateScoreError):
        assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)


def test_score_infinity_rejected_before_ranking(conn):
    entity = make_entity(conn, "Co10")
    _catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score="Infinity", rank=1, selected=True, abstained=False)

    with pytest.raises(InvalidCandidateScoreError):
        assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)


def test_zero_eligible_candidates_returns_no_eligible_candidates_not_an_error(conn):
    """No candidate_signals row at all under this catalyst's event_versions."""
    _catalyst_id, _ev = make_event_version(conn)
    result = assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)
    assert result == CatalystDecisionResult(status=NO_ELIGIBLE_CANDIDATES)


def test_zero_eligible_candidates_when_all_candidates_are_ineligible(conn):
    entity = make_entity(conn, "Co11")
    _catalyst_id, ev = make_event_version(conn, entity)
    make_candidate(conn, ev, entity, eligibility_status="ineligible")
    result = assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)
    assert result == CatalystDecisionResult(status=NO_ELIGIBLE_CANDIDATES)


def test_catalyst_with_no_event_versions_at_all_returns_no_eligible_candidates(conn):
    catalyst_id = make_catalyst(conn)
    result = assert_trade_abstention_invariant(conn, catalyst_id, MODEL_A, MODEL_VERSION)
    assert result == CatalystDecisionResult(status=NO_ELIGIBLE_CANDIDATES)


def test_missing_decision_from_the_given_model_raises_rather_than_miscounting(conn):
    """assert_trade_abstention_invariant's documented precondition
    (assert_full_coverage already passed) violated: an eligible candidate
    with no decision at all from this (model_id, model_version)."""
    entity = make_entity(conn, "Co12")
    _catalyst_id, ev = make_event_version(conn, entity)
    make_candidate(conn, ev, entity)  # no decision ever recorded

    with pytest.raises(MissingDecisionError):
        assert_trade_abstention_invariant(conn, _catalyst_id, MODEL_A, MODEL_VERSION)


# ===========================================================================
# assert_decision_set_ready_for_comparison
# ===========================================================================

def test_assert_full_coverage_is_enforced_before_the_invariant(conn):
    """Missing G's decision (Arm A decided, Arm G never did) must be caught
    by assert_full_coverage -- a completeness failure, distinct from
    MissingDecisionError (which only fires inside
    assert_trade_abstention_invariant itself when called standalone,
    bypassing assert_decision_set_ready_for_comparison)."""
    entity = make_entity(conn, "Co13")
    catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    # No arm_g_mechanical decision at all.

    with pytest.raises(ValueError, match="missing a"):
        assert_decision_set_ready_for_comparison(conn, catalyst_id, [(MODEL_A, MODEL_VERSION), (MODEL_G, MODEL_VERSION)])


def test_decision_set_ready_propagates_no_eligible_candidates(conn):
    catalyst_id, _ev = make_event_version(conn)
    result = assert_decision_set_ready_for_comparison(
        conn, catalyst_id, [(MODEL_A, MODEL_VERSION), (MODEL_G, MODEL_VERSION)]
    )
    assert result == NO_ELIGIBLE_CANDIDATES


def test_decision_set_ready_supports_independent_model_versions_per_pair(conn):
    """model_specs pairs are independent -- Arm A and Arm G need not share a
    version string."""
    entity = make_entity(conn, "Co14")
    catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score=0.9, rank=1, selected=True, abstained=False, model_version="a-v7")
    record_decision(conn, candidate, MODEL_G, score=0.2, rank=1, selected=True, abstained=False, model_version="g-v3")

    result = assert_decision_set_ready_for_comparison(
        conn, catalyst_id, [(MODEL_A, "a-v7"), (MODEL_G, "g-v3")]
    )
    assert result[(MODEL_A, "a-v7")].status == TRADED
    assert result[(MODEL_G, "g-v3")].status == TRADED


# ===========================================================================
# Abstention semantics -- all four trade/abstain states via
# assert_decision_set_ready_for_comparison, plus the zero-eligible-
# candidates exclusion as a fifth, distinct case.
# ===========================================================================

def _two_arm_result(conn, catalyst_id):
    return assert_decision_set_ready_for_comparison(
        conn, catalyst_id, [(MODEL_A, MODEL_VERSION), (MODEL_G, MODEL_VERSION)]
    )


def test_abstention_semantics_both_arms_trade(conn):
    entity = make_entity(conn, "Co15")
    catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, candidate, MODEL_G, score=0.3, rank=1, selected=True, abstained=False)

    result = _two_arm_result(conn, catalyst_id)
    assert result[(MODEL_A, MODEL_VERSION)].status == TRADED
    assert result[(MODEL_G, MODEL_VERSION)].status == TRADED


def test_abstention_semantics_a_trades_g_abstains(conn):
    entity = make_entity(conn, "Co16")
    catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, candidate, MODEL_G, score=-0.1, rank=1, selected=False, abstained=True)

    result = _two_arm_result(conn, catalyst_id)
    assert result[(MODEL_A, MODEL_VERSION)].status == TRADED
    assert result[(MODEL_G, MODEL_VERSION)].status == ABSTAINED


def test_abstention_semantics_a_abstains_g_trades(conn):
    entity = make_entity(conn, "Co17")
    catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score=0, rank=1, selected=False, abstained=True)
    record_decision(conn, candidate, MODEL_G, score=0.4, rank=1, selected=True, abstained=False)

    result = _two_arm_result(conn, catalyst_id)
    assert result[(MODEL_A, MODEL_VERSION)].status == ABSTAINED
    assert result[(MODEL_G, MODEL_VERSION)].status == TRADED


def test_abstention_semantics_both_arms_abstain_still_counted_as_a_real_observation(conn):
    """D_c = R_A,c - R_G,c = 0 - 0 = 0, but this catalyst IS a real
    observation (Section 6) -- distinct from a NO_ELIGIBLE_CANDIDATES
    catalyst, which is excluded from the D_c set entirely. Verified here at
    the level this implementation pass actually reaches: both arms report a
    genuine ABSTAINED status (not NO_ELIGIBLE_CANDIDATES), which is exactly
    the information a Section 7f builder needs to assign R_arm,c = 0 to
    each and still include the catalyst as an observation."""
    entity = make_entity(conn, "Co18")
    catalyst_id, ev = make_event_version(conn, entity)
    candidate = make_candidate(conn, ev, entity)
    record_decision(conn, candidate, MODEL_A, score=-0.2, rank=1, selected=False, abstained=True)
    record_decision(conn, candidate, MODEL_G, score=0, rank=1, selected=False, abstained=True)

    result = _two_arm_result(conn, catalyst_id)
    assert result != NO_ELIGIBLE_CANDIDATES
    assert result[(MODEL_A, MODEL_VERSION)].status == ABSTAINED
    assert result[(MODEL_G, MODEL_VERSION)].status == ABSTAINED


def test_abstention_semantics_zero_eligible_candidates_is_a_fifth_distinct_case(conn):
    catalyst_id, _ev = make_event_version(conn)
    result = _two_arm_result(conn, catalyst_id)
    assert result == NO_ELIGIBLE_CANDIDATES


# ===========================================================================
# stable_entity_sort_key
# ===========================================================================

class _FakeEntity:
    def __init__(self, entity_id, cik, legal_name):
        self.entity_id = entity_id
        self.cik = cik
        self.legal_name = legal_name


def test_stable_entity_sort_key_none_cik_falls_back_to_name():
    e = _FakeEntity("e1", None, "Eaton Corp plc")
    assert stable_entity_sort_key(e) == "NAME:eaton"


def test_stable_entity_sort_key_cik_is_zero_padded_to_ten_digits():
    e = _FakeEntity("e1", "1045810", "NVIDIA Corporation")
    assert stable_entity_sort_key(e) == "CIK:0001045810"


def test_stable_entity_sort_key_cik_whitespace_is_stripped():
    e1 = _FakeEntity("e1", " 1045810 ", "NVIDIA Corporation")
    e2 = _FakeEntity("e2", "1045810", "NVIDIA Corporation")
    assert stable_entity_sort_key(e1) == stable_entity_sort_key(e2) == "CIK:0001045810"


@pytest.mark.parametrize("bad_cik", ["", "   ", "abc", "0"])
def test_stable_entity_sort_key_rejects_malformed_cik(bad_cik):
    e = _FakeEntity("e1", bad_cik, "Some Co")
    with pytest.raises(InvalidStableEntityKeyError):
        stable_entity_sort_key(e)


def test_stable_entity_sort_key_rejects_cik_with_too_many_digits():
    e = _FakeEntity("e1", "123456789012", "Some Co")  # 12 digits
    with pytest.raises(InvalidStableEntityKeyError):
        stable_entity_sort_key(e)


def test_stable_entity_sort_key_rejects_null_cik_with_empty_normalized_name():
    e = _FakeEntity("e1", None, "Inc")  # normalizes to "" (pure corporate suffix)
    with pytest.raises(InvalidStableEntityKeyError):
        stable_entity_sort_key(e)


def test_equal_scores_two_different_ciks_deterministic_ordering_stable_across_insertion_order(conn):
    """Simulated-rebuild insertion-order change: build an equivalent
    candidate set twice, inserting entities/candidates in the opposite
    order the second time (and using a fresh, non-colliding CIK pair each
    time, since entities.cik is globally unique) -- confirms the resulting
    rank ordering (by relative CIK magnitude, i.e. by name here since the
    names are consistent across both builds) is identical both times:
    insertion order (which drives entity_id/candidate_id's random UUID
    values) must not affect the outcome, only score + stable_entity_sort_key
    do."""
    def build(order, low_cik, high_cik):
        # Beta always gets the numerically LOWER cik -- "Beta Co" must rank
        # first at equal scores in both builds if insertion order is truly
        # irrelevant.
        names_ciks = [("Alpha Co", high_cik), ("Beta Co", low_cik)]
        if order == "reversed":
            names_ciks = list(reversed(names_ciks))
        catalyst_id, ev = make_event_version(conn)
        for name, cik in names_ciks:
            entity_id = make_entity_with_cik(conn, name, cik)
            candidate_id = make_candidate(conn, ev, entity_id)
            record_decision(conn, candidate_id, MODEL_A, score=0.5, rank=None, selected=False, abstained=False)
        return catalyst_id

    def ranked_names(catalyst_id):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cs.candidate_id, e.cik, e.legal_name, mcd.score
                FROM candidate_signals cs
                JOIN entities e ON e.entity_id = cs.entity_id
                JOIN model_candidate_decisions mcd ON mcd.candidate_id = cs.candidate_id
                WHERE cs.event_version_id = ANY(%s::uuid[]) AND mcd.model_id = %s AND mcd.model_version = %s
                """,
                (get_current_event_versions_for_catalyst(conn, catalyst_id), MODEL_A, MODEL_VERSION),
            )
            rows = cur.fetchall()
        entities = [_FakeEntity(None, cik, None) for _cid, cik, _name, _score in rows]
        keyed = sorted(zip(rows, entities), key=lambda pair: (-pair[0][3], stable_entity_sort_key(pair[1])))
        return [name for (_cid, _cik, name, _score), _e in keyed]

    catalyst_1 = build("forward", low_cik="0000000150", high_cik="0000000200")
    catalyst_2 = build("reversed", low_cik="0000000650", high_cik="0000000700")
    order_1 = ranked_names(catalyst_1)
    order_2 = ranked_names(catalyst_2)
    assert order_1 == order_2 == ["Beta Co", "Alpha Co"]


def test_equal_scores_two_cik_null_entities_different_names_deterministic_ordering(conn):
    catalyst_id, ev = make_event_version(conn)
    e_alpha = make_entity(conn, "Alpha Industries")
    e_zulu = make_entity(conn, "Zulu Industries")
    c_alpha = make_candidate(conn, ev, e_alpha)
    c_zulu = make_candidate(conn, ev, e_zulu)
    record_decision(conn, c_alpha, MODEL_A, score=0.5, rank=1, selected=True, abstained=False)
    record_decision(conn, c_zulu, MODEL_A, score=0.5, rank=2, selected=False, abstained=False)

    # "alpha industries" < "zulu industries" lexicographically -> Alpha is rank 1.
    result = assert_trade_abstention_invariant(conn, catalyst_id, MODEL_A, MODEL_VERSION)
    assert result == CatalystDecisionResult(status=TRADED, selected_candidate_id=c_alpha)


def test_equal_scores_identical_stable_entity_sort_key_raises_unresolved_tie(conn):
    """The same entity appearing via two event_versions of one catalyst,
    same CIK, same score -- a genuine unresolved tie, not resolvable by the
    tie-break."""
    entity = make_entity_with_cik(conn, "Same Co", "1234567890")
    catalyst_id = make_catalyst(conn)
    ev1 = make_event_version_under_catalyst(conn, catalyst_id)
    ev2 = make_event_version_under_catalyst(conn, catalyst_id)
    c1 = make_candidate(conn, ev1, entity)
    c2 = make_candidate(conn, ev2, entity)
    record_decision(conn, c1, MODEL_A, score=0.5, rank=1, selected=True, abstained=False)
    record_decision(conn, c2, MODEL_A, score=0.5, rank=2, selected=False, abstained=False)

    with pytest.raises(UnresolvedRankingTieError):
        assert_trade_abstention_invariant(conn, catalyst_id, MODEL_A, MODEL_VERSION)


# ===========================================================================
# assert_confirmatory_configuration_complete
# ===========================================================================

def test_configuration_complete_raises_in_the_real_unconfigured_default_state():
    with pytest.raises(ConfirmatoryConfigurationIncompleteError):
        assert_confirmatory_configuration_complete()


def test_configuration_complete_passes_once_every_deferred_item_is_set(configured):
    assert_confirmatory_configuration_complete()  # must not raise


def test_configuration_complete_fails_when_provider_lacks_size_capability(configured, monkeypatch):
    """Two distinct levels of size/depth failure (Section 7e): a provider
    incapable of establishing normalized size fails the GLOBAL preflight,
    distinct from a per-catalyst quote-size exclusion."""
    monkeypatch.setattr(ca, "CONFIRMATORY_PROVIDER_SIZE_CAPABLE", False)
    with pytest.raises(ConfirmatoryConfigurationIncompleteError, match="size"):
        assert_confirmatory_configuration_complete()


def test_configuration_complete_fails_when_stop_provenance_unavailable(configured, monkeypatch):
    monkeypatch.setattr(ca, "STOP_RULE_PROVENANCE_AVAILABLE", False)
    with pytest.raises(ConfirmatoryConfigurationIncompleteError, match="stop"):
        assert_confirmatory_configuration_complete()


def test_configuration_complete_fails_when_frozen_preregistration_values_are_tampered_with(configured, monkeypatch):
    monkeypatch.setattr(ca, "DELTA", 0.01)
    with pytest.raises(ConfirmatoryConfigurationIncompleteError):
        assert_confirmatory_configuration_complete()


def test_configuration_complete_fails_for_preregistered_placeholder_outside_envelope(configured, monkeypatch):
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD", 5000.0)
    with pytest.raises(ConfirmatoryConfigurationIncompleteError, match=r"\$1,000-3,000"):
        assert_confirmatory_configuration_complete()


def test_configuration_complete_fails_for_actual_funded_equity_outside_envelope_without_acknowledgment(configured, monkeypatch):
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD", 5000.0)
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD_PROVENANCE", "actual_funded_equity")
    with pytest.raises(ConfirmatoryConfigurationIncompleteError, match="discrepancy"):
        assert_confirmatory_configuration_complete()


def test_configuration_complete_passes_for_actual_funded_equity_outside_envelope_when_acknowledged(configured, monkeypatch):
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD", 5000.0)
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD_PROVENANCE", "actual_funded_equity")
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD_DISCREPANCY_ACKNOWLEDGED", True)
    assert_confirmatory_configuration_complete()  # must not raise


# ===========================================================================
# assert_outcome_ready_for_confirmation
# ===========================================================================

def _build_valid_outcome(conn, entry_ask=100.0, exit_bid=101.0, entry_fee=1.0, exit_fee=1.0,
                          exit_reason="horizon", entry_ask_size=None, exit_bid_size=None,
                          provider="test_provider"):
    """A traded arm's real chain: entity -> instrument -> arm -> entry ->
    quote snapshots -> outcome, with return_gross/return_net_of_costs
    computed exactly per the Section 7d formula so the "happy path" is
    genuinely internally correct, not just internally consistent."""
    entity = make_entity(conn, "Broadcom-like Co")
    _catalyst_id, ev = make_event_version(conn, entity)
    instrument_id = make_instrument(conn, entity)
    arm_id = make_arm(conn, "A")

    entry_ts = NOW
    exit_ts = NOW + timedelta(days=1)
    entry_quote_ts = entry_ts + timedelta(seconds=5)
    exit_quote_ts = exit_ts + timedelta(seconds=5)

    entry_quote = make_quote(conn, instrument_id, bid=99.5, ask=entry_ask, quote_timestamp=entry_quote_ts,
                              provider=provider, ask_size=entry_ask_size)
    exit_quote = make_quote(conn, instrument_id, bid=exit_bid, ask=101.5, quote_timestamp=exit_quote_ts,
                             provider=provider, bid_size=exit_bid_size)

    entry_id = make_entry(conn, arm_id, ev, instrument_id, entry_ts, entry_quote)

    shadow_notional = 2000.0 * 0.05  # matches the `configured` fixture's REFERENCE_EQUITY_USD
    q = shadow_notional / entry_ask
    c_entry = shadow_notional + entry_fee
    c_exit = q * exit_bid - exit_fee
    return_gross = math.log(exit_bid / entry_ask)
    return_net = math.log(c_exit / c_entry)

    outcome_id = make_outcome(
        conn, entry_id, exit_ts, exit_quote, entry_fee=entry_fee, exit_fee=exit_fee,
        return_gross=return_gross, return_net=return_net, exit_reason=exit_reason,
    )
    return outcome_id


def test_valid_outcome_passes_confirmation(conn, configured):
    outcome_id = _build_valid_outcome(conn)
    assert_outcome_ready_for_confirmation(conn, outcome_id)  # must not raise


def test_recomputed_return_mismatch_is_a_hard_failure_even_with_valid_snapshot_ids(conn, configured):
    """Fabricate a stored return_net_of_costs that does NOT match what the
    referenced entry/exit snapshots and fees actually imply -- the
    snapshot IDs themselves are individually valid."""
    outcome_id = _build_valid_outcome(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE arm_outcomes SET return_net_of_costs = return_net_of_costs + 1.0 WHERE outcome_id = %s",
            (outcome_id,),
        )
    with pytest.raises(OutcomeIntegrityError):
        assert_outcome_ready_for_confirmation(conn, outcome_id)


def test_insufficient_quote_size_for_q_is_a_legitimate_exclusion_not_a_hard_failure(conn, configured):
    """q = SHADOW_NOTIONAL_USD / entry_ask = 100 / 100 = 1.0 share here --
    an entry ask_size smaller than that is a genuinely unobservable market
    outcome, distinguished by exception TYPE from a hard integrity
    failure (Section 7f: a future builder tells these apart by type)."""
    outcome_id = _build_valid_outcome(conn, entry_ask=100.0, entry_ask_size=0.5)
    with pytest.raises(QuoteUnobservableError):
        assert_outcome_ready_for_confirmation(conn, outcome_id)


def test_sufficient_quote_size_passes(conn, configured):
    outcome_id = _build_valid_outcome(conn, entry_ask=100.0, entry_ask_size=10.0, exit_bid_size=10.0)
    assert_outcome_ready_for_confirmation(conn, outcome_id)  # must not raise


def test_provider_incapable_of_normalized_size_fails_the_global_preflight_instead(configured, monkeypatch):
    """The OTHER of the two distinct size/depth failure levels (Section
    7e): tested here at assert_confirmatory_configuration_complete, not
    per-outcome -- an incapable provider makes the experiment unconfigured,
    full stop, regardless of which catalysts happen to need market data."""
    monkeypatch.setattr(ca, "CONFIRMATORY_PROVIDER_SIZE_CAPABLE", False)
    with pytest.raises(ConfirmatoryConfigurationIncompleteError):
        assert_confirmatory_configuration_complete()


def test_fee_mismatch_fails_even_when_return_recomputes_internally_consistently(conn, configured, monkeypatch):
    """A wrong-but-internally-consistent fee pair (both entry_fee/exit_fee
    AND return_gross/return_net_of_costs computed from those SAME wrong
    fees, so the return "recomputes consistently" from what's stored) must
    still fail, because the fee values themselves don't match the frozen
    fee methodology (configured fixture's compute_expected_fees always
    returns (1.0, 1.0))."""
    wrong_entry_fee, wrong_exit_fee = 5.0, 5.0
    outcome_id = _build_valid_outcome(conn, entry_fee=wrong_entry_fee, exit_fee=wrong_exit_fee)
    with pytest.raises(OutcomeIntegrityError):
        assert_outcome_ready_for_confirmation(conn, outcome_id)


def test_horizon_and_protective_stop_both_fail_while_stop_provenance_is_unimplemented(conn, monkeypatch):
    """Deliberately does NOT use the `configured` fixture's
    STOP_RULE_PROVENANCE_AVAILABLE=True override -- this is the one test
    that must exercise the REAL (current) unimplemented default. Every
    other confirmatory-config item is set so this is isolating the stop
    gate specifically, not incidentally failing for some other reason."""
    monkeypatch.setattr(ca, "MAX_QUOTE_LOOKUP_DELAY_SECONDS", 300)
    monkeypatch.setattr(ca, "MAX_QUOTE_STALENESS_SECONDS", 60)
    monkeypatch.setattr(ca, "CONFIRMATORY_DATA_PROVIDER", "test_provider")
    monkeypatch.setattr(ca, "CONFIRMATORY_PROVIDER_SIZE_CAPABLE", True)
    monkeypatch.setattr(ca, "FEE_METHOD_VERSION", "test_fee_v1")
    monkeypatch.setattr(ca, "compute_expected_fees", lambda **kwargs: (1.0, 1.0))
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD", 2000.0)
    monkeypatch.setattr(ca, "REFERENCE_EQUITY_USD_PROVENANCE", "preregistered_placeholder")
    # STOP_RULE_PROVENANCE_AVAILABLE left at its real default: False.

    horizon_outcome = _build_valid_outcome(conn, exit_reason="horizon")
    stop_outcome = _build_valid_outcome(conn, exit_reason="protective_stop")

    with pytest.raises(ConfirmatoryConfigurationIncompleteError):
        assert_outcome_ready_for_confirmation(conn, horizon_outcome)
    with pytest.raises(ConfirmatoryConfigurationIncompleteError):
        assert_outcome_ready_for_confirmation(conn, stop_outcome)


def test_crossed_market_quote_is_unobservable_not_a_policy_edge_case(conn, configured):
    outcome_id = _build_valid_outcome(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE quote_snapshots SET bid = ask + 1
            WHERE quote_snapshot_id = (
                SELECT entry_quote_snapshot_id FROM arm_entries
                WHERE entry_id = (SELECT entry_id FROM arm_outcomes WHERE outcome_id = %s)
            )
            """,
            (outcome_id,),
        )
    with pytest.raises(QuoteUnobservableError):
        assert_outcome_ready_for_confirmation(conn, outcome_id)


def test_wrong_data_provider_on_a_snapshot_is_unobservable(conn, configured):
    outcome_id = _build_valid_outcome(conn, provider="some_other_provider")
    with pytest.raises(QuoteUnobservableError):
        assert_outcome_ready_for_confirmation(conn, outcome_id)
