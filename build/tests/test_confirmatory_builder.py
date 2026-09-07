"""
Section 7f Implementation Spec, FINAL (specs/section7f-implementation-spec-final.md),
Section 10's PENDING_NOT_MATURED, builder-level, and promotion-test-gating
tests.
"""
import math
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from conftest import make_entity

import confirmatory_analysis
import confirmatory_builder as cb
from confirmatory_builder import (
    classify_traded_catalyst_maturity,
    outcome_due_at,
    build_confirmatory_dataset,
    run_confirmatory_promotion_test,
    ConfirmatoryBuildReport,
    ConfirmatoryAnalysisNotAuthorizedError,
    RequiredOutcomeOverdueError,
    PENDING_NOT_MATURED,
)

UTC = timezone.utc
MODEL_A = ("arm_a_llm", "v1")
MODEL_G = ("arm_g_mechanical", "v1")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def make_experiment(conn, scoring_epoch="epoch-1"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO experiments (name, scoring_epoch, cohort_type) VALUES ('t', %s, 'confirmatory') "
            "RETURNING experiment_id",
            (scoring_epoch,),
        )
        return cur.fetchone()[0]


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


def admit(conn, experiment_id, scoring_epoch, catalyst_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO experiment_catalysts (experiment_id, scoring_epoch, catalyst_id) VALUES (%s, %s, %s)",
            (experiment_id, scoring_epoch, catalyst_id),
        )


def make_event_version(conn, catalyst_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO canonical_events (catalyst_id, event_category) VALUES (%s, 'guidance_revision') "
            "RETURNING canonical_event_id",
            (catalyst_id,),
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO event_versions (canonical_event_id, version_number, decision_at) VALUES (%s, 1, now()) "
            "RETURNING event_version_id",
            (event_id,),
        )
        return cur.fetchone()[0]


def make_candidate(conn, event_version_id, entity_id, eligibility_status="eligible"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO candidate_signals (event_version_id, entity_id, eligibility_status, "
            "policy_version, decision_timestamp) VALUES (%s, %s, %s, 'v1', now()) RETURNING candidate_id",
            (event_version_id, entity_id, eligibility_status),
        )
        return cur.fetchone()[0]


def record_decision(conn, candidate_id, model_id, model_version, score, rank, selected, abstained):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_candidate_decisions "
            "(candidate_id, model_id, model_version, score, rank, selected, abstained, decision_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) RETURNING decision_id",
            (candidate_id, model_id, model_version, score, rank, selected, abstained),
        )
        return cur.fetchone()[0]


def make_instrument(conn, entity_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instruments (entity_id, exchange) VALUES (%s, 'TEST') RETURNING instrument_id",
            (entity_id,),
        )
        return cur.fetchone()[0]


def get_or_make_arm(conn, experiment_id, arm_code):
    """experiment_arms has UNIQUE(experiment_id, arm_code) -- one row per
    arm PER EXPERIMENT, shared across every catalyst in that experiment,
    unlike candidates/entities/instruments which are per-catalyst. Looked
    up (not cached in Python) so this is safe across the autouse clean_db
    truncation between tests."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT arm_id FROM experiment_arms WHERE experiment_id = %s AND arm_code = %s",
            (experiment_id, arm_code),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            "INSERT INTO experiment_arms (experiment_id, arm_code, arm_label) VALUES (%s, %s, 't') "
            "RETURNING arm_id",
            (experiment_id, arm_code),
        )
        return cur.fetchone()[0]


def make_quote(conn, instrument_id, bid, ask, quote_timestamp, provider="test_provider",
               session="regular", staleness=1.0):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO quote_snapshots (instrument_id, bid, ask, quote_timestamp, data_provider, "
            "trading_session, staleness_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "RETURNING quote_snapshot_id",
            (instrument_id, bid, ask, quote_timestamp, provider, session, staleness),
        )
        return cur.fetchone()[0]


def make_arm_entry(conn, arm_id, event_version_id, instrument_id, model_decision_id,
                    candidate_id, entry_timestamp, entry_quote_snapshot_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO arm_entries (arm_id, event_version_id, instrument_id, candidate_id, "
            "model_decision_id, entry_timestamp, entry_quote_snapshot_id, notional, direction) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 100.0, 'long') RETURNING entry_id",
            (arm_id, event_version_id, instrument_id, candidate_id, model_decision_id,
             entry_timestamp, entry_quote_snapshot_id),
        )
        return cur.fetchone()[0]


def make_arm_outcome(conn, entry_id, exit_timestamp, exit_quote_snapshot_id, entry_fee, exit_fee,
                      return_gross, return_net, exit_reason="horizon", horizon_label="1day"):
    # fee_method_version must match the REAL confirmatory_analysis.FEE_METHOD_VERSION
    # (wired in for real by fee_methodology.py's import, unlike
    # test_confirmatory_analysis.py's `configured` fixture which monkeypatches
    # it to a synthetic value) -- assert_outcome_ready_for_confirmation checks
    # for an exact match.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arm_outcomes (
                entry_id, horizon_label, exit_timestamp, exit_quote_snapshot_id,
                return_gross, return_net_of_costs,
                entry_price_source, exit_price_source, entry_fee, exit_fee,
                return_method_version, fee_method_version, exit_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, 'nbbo_side_proxy', 'nbbo_side_proxy', %s, %s,
                      'shadow_nbbo_log_v1', %s, %s)
            RETURNING outcome_id
            """,
            (entry_id, horizon_label, exit_timestamp, exit_quote_snapshot_id,
             return_gross, return_net, entry_fee, exit_fee,
             confirmatory_analysis.FEE_METHOD_VERSION, exit_reason),
        )
        return cur.fetchone()[0]


@pytest.fixture
def configured(monkeypatch):
    """Every deferred item confirmatory_analysis.py's real preflight
    (assert_confirmatory_configuration_complete, UNCHANGED by this pass)
    checks, plus this pass's own OUTCOME_PROCESSING_GRACE. Does NOT set
    CONFIRMATORY_ANALYSIS_TRIGGER -- that one stays genuinely unset except
    where a specific test explicitly monkeypatches it, per the promotion-
    gating tests below."""
    monkeypatch.setattr(confirmatory_analysis, "MAX_QUOTE_LOOKUP_DELAY_SECONDS", 300)
    monkeypatch.setattr(confirmatory_analysis, "MAX_QUOTE_STALENESS_SECONDS", 60)
    monkeypatch.setattr(confirmatory_analysis, "CONFIRMATORY_DATA_PROVIDER", "test_provider")
    monkeypatch.setattr(confirmatory_analysis, "CONFIRMATORY_PROVIDER_SIZE_CAPABLE", True)
    monkeypatch.setattr(confirmatory_analysis, "STOP_RULE_PROVENANCE_AVAILABLE", True)
    monkeypatch.setattr(confirmatory_analysis, "REFERENCE_EQUITY_USD", 2000.0)
    monkeypatch.setattr(confirmatory_analysis, "REFERENCE_EQUITY_USD_PROVENANCE", "preregistered_placeholder")
    # FEE_METHOD_VERSION / compute_expected_fees are already wired in for
    # real by fee_methodology.py's own import (imported transitively via
    # confirmatory_builder) -- no override needed here.
    monkeypatch.setattr(cb, "OUTCOME_PROCESSING_GRACE", timedelta(hours=2))
    return cb


ENTRY_TS = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)  # Monday 2026-01-05, well after that
    # session's real 14:30 UTC (9:30 EST) NYSE open -- verified empirically
    # against the pinned calendar, same discipline as test_trading_calendar.py.
    # outcome_due_at(ENTRY_TS) is therefore the NEXT session's open,
    # Tue 2026-01-06 14:30 UTC.


def _shadow_notional():
    return confirmatory_analysis.REFERENCE_EQUITY_USD * confirmatory_analysis.NOTIONAL_FRACTION  # 100.0


def make_shared_candidate(conn, event_version_id, entity_name):
    """One candidate_signals row that BOTH arms will independently decide
    on -- assert_full_coverage requires every eligible candidate under an
    event_version to have a decision from both models, so a realistic
    per-catalyst fixture has exactly ONE such shared candidate (and its
    one entity/instrument), not two separately-decided candidates."""
    entity_id = make_entity(conn, entity_name)
    instrument_id = make_instrument(conn, entity_id)
    candidate_id = make_candidate(conn, event_version_id, entity_id)
    return entity_id, instrument_id, candidate_id


def decide_trade_with_valid_outcome(conn, experiment_id, arm_code, model_id, model_version,
                                     instrument_id, candidate_id, event_version_id, entry_ts,
                                     entry_ask=100.0, exit_bid=101.0):
    """Records model_id's decision (rank=1, selected) on the given existing
    candidate_id, plus a full, valid, correctly-recomputed arm_entries/
    arm_outcomes chain. Returns the expected return_net_of_costs."""
    decision_id = record_decision(conn, candidate_id, model_id, model_version,
                                   score=0.9, rank=1, selected=True, abstained=False)
    arm_id = get_or_make_arm(conn, experiment_id, arm_code)

    entry_quote = make_quote(conn, instrument_id, bid=99.5, ask=entry_ask,
                              quote_timestamp=entry_ts + timedelta(seconds=5))
    entry_id = make_arm_entry(conn, arm_id, event_version_id, instrument_id, decision_id,
                               candidate_id, entry_ts, entry_quote)

    exit_ts = outcome_due_at(entry_ts)
    exit_quote = make_quote(conn, instrument_id, bid=exit_bid, ask=exit_bid + 0.5,
                             quote_timestamp=exit_ts + timedelta(seconds=5))

    shadow_notional = _shadow_notional()
    entry_fee, exit_fee = confirmatory_analysis.compute_expected_fees(
        outcome_id="precompute", shadow_notional=shadow_notional, entry_ask=entry_ask, exit_bid=exit_bid,
    )
    q = shadow_notional / entry_ask
    c_entry = shadow_notional + entry_fee
    c_exit = q * exit_bid - exit_fee
    return_gross = math.log(exit_bid / entry_ask)
    return_net = math.log(c_exit / c_entry)

    make_arm_outcome(conn, entry_id, exit_ts, exit_quote, entry_fee, exit_fee, return_gross, return_net)
    return return_net


def decide_trade_with_excluded_outcome(conn, experiment_id, arm_code, model_id, model_version,
                                        instrument_id, candidate_id, event_version_id, entry_ts):
    """TRADED, with an outcome that fails assert_outcome_ready_for_confirmation
    via QuoteUnobservableError specifically (a crossed exit market) -- a
    legitimate exclusion, not a hard failure."""
    decision_id = record_decision(conn, candidate_id, model_id, model_version,
                                   score=0.9, rank=1, selected=True, abstained=False)
    arm_id = get_or_make_arm(conn, experiment_id, arm_code)

    entry_quote = make_quote(conn, instrument_id, bid=99.5, ask=100.0,
                              quote_timestamp=entry_ts + timedelta(seconds=5))
    entry_id = make_arm_entry(conn, arm_id, event_version_id, instrument_id, decision_id,
                               candidate_id, entry_ts, entry_quote)

    exit_ts = outcome_due_at(entry_ts)
    exit_quote = make_quote(conn, instrument_id, bid=200.0, ask=100.0,  # crossed: bid > ask
                             quote_timestamp=exit_ts + timedelta(seconds=5))
    make_arm_outcome(conn, entry_id, exit_ts, exit_quote, entry_fee=0.0, exit_fee=0.0,
                      return_gross=0.0, return_net=0.0)


def decide_trade_pending(conn, experiment_id, arm_code, model_id, model_version,
                          instrument_id, candidate_id, event_version_id, entry_ts):
    """TRADED, arm_entries row exists (real entry_timestamp), but no
    arm_outcomes row at all yet."""
    decision_id = record_decision(conn, candidate_id, model_id, model_version,
                                   score=0.9, rank=1, selected=True, abstained=False)
    arm_id = get_or_make_arm(conn, experiment_id, arm_code)
    entry_quote = make_quote(conn, instrument_id, bid=99.5, ask=100.0,
                              quote_timestamp=entry_ts + timedelta(seconds=5))
    make_arm_entry(conn, arm_id, event_version_id, instrument_id, decision_id,
                    candidate_id, entry_ts, entry_quote)


def decide_abstain(conn, model_id, model_version, candidate_id):
    record_decision(conn, candidate_id, model_id, model_version,
                     score=-0.1, rank=1, selected=False, abstained=True)


# ===========================================================================
# PENDING_NOT_MATURED (classify_traded_catalyst_maturity unit tests)
# ===========================================================================

def test_before_outcome_due_at_is_pending_not_matured(configured):
    due_at = outcome_due_at(ENTRY_TS)
    analysis_as_of = due_at - timedelta(minutes=1)
    assert classify_traded_catalyst_maturity(ENTRY_TS, analysis_as_of) == PENDING_NOT_MATURED


def test_exactly_at_the_maturity_plus_grace_boundary_is_still_pending(configured):
    due_at = outcome_due_at(ENTRY_TS)
    boundary = due_at + cb.OUTCOME_PROCESSING_GRACE
    assert classify_traded_catalyst_maturity(ENTRY_TS, boundary) == PENDING_NOT_MATURED


def test_one_second_past_the_boundary_with_no_outcome_row_is_a_hard_failure(configured):
    due_at = outcome_due_at(ENTRY_TS)
    boundary = due_at + cb.OUTCOME_PROCESSING_GRACE
    with pytest.raises(RequiredOutcomeOverdueError):
        classify_traded_catalyst_maturity(ENTRY_TS, boundary + timedelta(seconds=1))


def test_naive_entry_timestamp_raises(configured):
    with pytest.raises(ValueError):
        classify_traded_catalyst_maturity(datetime(2026, 1, 5, 13, 30), ENTRY_TS)


def test_naive_analysis_as_of_raises(configured):
    with pytest.raises(ValueError):
        classify_traded_catalyst_maturity(ENTRY_TS, datetime(2026, 1, 6, 13, 30))


def test_outcome_processing_grace_unset_is_fail_closed(monkeypatch):
    monkeypatch.setattr(cb, "OUTCOME_PROCESSING_GRACE", None)
    with pytest.raises(confirmatory_analysis.ConfirmatoryConfigurationIncompleteError):
        classify_traded_catalyst_maturity(ENTRY_TS, ENTRY_TS + timedelta(days=1))


def test_an_existing_outcome_row_proceeds_to_assert_outcome_ready_regardless_of_maturity_timing(conn, configured):
    """A traded catalyst whose entry_timestamp is far in the past (long
    past any maturity+grace boundary) but which HAS a real arm_outcomes
    row must proceed straight to assert_outcome_ready_for_confirmation --
    never PENDING_NOT_MATURED, never RequiredOutcomeOverdueError -- because
    the outcome already exists. Exercised through the full builder since
    that's the only place this branch decision (row exists vs. doesn't) is
    made."""
    experiment_id = make_experiment(conn)
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    _entity, instrument_id, candidate_id = make_shared_candidate(conn, ev, "Long Ago Co")

    long_ago_entry = datetime(2020, 1, 6, 14, 30, tzinfo=UTC)  # far past any boundary
    expected_return = decide_trade_with_valid_outcome(
        conn, experiment_id, "A", *MODEL_A, instrument_id, candidate_id, ev, long_ago_entry
    )
    decide_abstain(conn, *MODEL_G, candidate_id)

    report = build_confirmatory_dataset(
        conn, experiment_id, "epoch-1",
        analysis_as_of=datetime(2026, 1, 1, tzinfo=UTC),  # long after long_ago_entry's boundary
        model_specs={"A": MODEL_A, "G": MODEL_G},
    )
    assert report.included_count == 1
    assert report.pending_count == 0
    assert report.records[0][1] == pytest.approx(expected_return)


# ===========================================================================
# Builder-level
# ===========================================================================

def test_full_scope_run_mixing_all_five_outcomes_produces_correct_counts(conn, configured):
    experiment_id = make_experiment(conn)
    analysis_as_of = ENTRY_TS + timedelta(hours=1)  # well before ENTRY_TS's own maturity boundary

    # 1. INCLUDED: A trades with a valid outcome, G abstains.
    c_included = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_included)
    ev_included = make_event_version(conn, c_included)
    _e1, instr1, cand1 = make_shared_candidate(conn, ev_included, "Included Co")
    expected_return = decide_trade_with_valid_outcome(
        conn, experiment_id, "A", *MODEL_A, instr1, cand1, ev_included, ENTRY_TS
    )
    decide_abstain(conn, *MODEL_G, cand1)

    # 2. EXCLUDED, attributable to A: A trades but its outcome is quote-
    #    unobservable (crossed market); G abstains.
    c_excl_a = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_excl_a)
    ev_excl_a = make_event_version(conn, c_excl_a)
    _e2, instr2, cand2 = make_shared_candidate(conn, ev_excl_a, "ExclA Co")
    decide_trade_with_excluded_outcome(conn, experiment_id, "A", *MODEL_A, instr2, cand2, ev_excl_a, ENTRY_TS)
    decide_abstain(conn, *MODEL_G, cand2)

    # 3. EXCLUDED, attributable to G: mirror of #2.
    c_excl_g = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_excl_g)
    ev_excl_g = make_event_version(conn, c_excl_g)
    _e3, instr3, cand3 = make_shared_candidate(conn, ev_excl_g, "ExclG Co")
    decide_abstain(conn, *MODEL_A, cand3)
    decide_trade_with_excluded_outcome(conn, experiment_id, "G", *MODEL_G, instr3, cand3, ev_excl_g, ENTRY_TS)

    # 4. NOT_POLICY_OBSERVATION: zero eligible candidates.
    c_not_policy = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_not_policy)
    make_event_version(conn, c_not_policy)  # no candidates at all

    # 5. PENDING: A trades, no outcome row yet; G abstains.
    c_pending = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_pending)
    ev_pending = make_event_version(conn, c_pending)
    _e5, instr5, cand5 = make_shared_candidate(conn, ev_pending, "Pending Co")
    decide_trade_pending(conn, experiment_id, "A", *MODEL_A, instr5, cand5, ev_pending, ENTRY_TS)
    decide_abstain(conn, *MODEL_G, cand5)

    report = build_confirmatory_dataset(
        conn, experiment_id, "epoch-1", analysis_as_of, model_specs={"A": MODEL_A, "G": MODEL_G}
    )

    assert report.total_catalysts_in_scope == 5
    assert report.catalysts_with_zero_eligible_candidates == 1
    assert report.pending_count == 1
    assert report.legitimate_exclusion_count == 2
    assert report.legitimate_exclusions_by_arm == {"A": 1, "G": 1, "both": 0}
    assert report.included_count == 1
    assert len(report.records) == 1
    assert report.records[0][0] == c_included
    assert report.records[0][1] == pytest.approx(expected_return)
    # Sanity invariant: every catalyst in scope lands in exactly one bucket.
    assert (report.catalysts_with_zero_eligible_candidates + report.pending_count
            + report.legitimate_exclusion_count + report.included_count) == report.total_catalysts_in_scope


def test_both_arms_excluded_attributes_to_both(conn, configured):
    experiment_id = make_experiment(conn)
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    _e, instrument_id, candidate_id = make_shared_candidate(conn, ev, "Both Excluded Co")
    decide_trade_with_excluded_outcome(conn, experiment_id, "A", *MODEL_A, instrument_id, candidate_id, ev, ENTRY_TS)
    decide_trade_with_excluded_outcome(conn, experiment_id, "G", *MODEL_G, instrument_id, candidate_id, ev, ENTRY_TS)

    report = build_confirmatory_dataset(
        conn, experiment_id, "epoch-1", ENTRY_TS + timedelta(hours=1), model_specs={"A": MODEL_A, "G": MODEL_G}
    )
    assert report.legitimate_exclusions_by_arm == {"A": 0, "G": 0, "both": 1}
    assert report.included_count == 0


def test_both_arms_abstain_is_a_valid_observation_with_d_c_zero(conn, configured):
    experiment_id = make_experiment(conn)
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    _e, _instr, candidate_id = make_shared_candidate(conn, ev, "Both Abstain Co")
    decide_abstain(conn, *MODEL_A, candidate_id)
    decide_abstain(conn, *MODEL_G, candidate_id)

    report = build_confirmatory_dataset(
        conn, experiment_id, "epoch-1", ENTRY_TS, model_specs={"A": MODEL_A, "G": MODEL_G}
    )
    assert report.included_count == 1
    assert report.records == [(catalyst_id, 0.0)]


def test_any_hard_failure_anywhere_in_scope_aborts_the_whole_build_with_no_partial_report(conn, configured):
    experiment_id = make_experiment(conn)

    # A normal, successfully-classifiable catalyst that would otherwise
    # contribute to the report.
    c_ok = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_ok)
    ev_ok = make_event_version(conn, c_ok)
    _e_ok, _instr_ok, cand_ok = make_shared_candidate(conn, ev_ok, "OK Co")
    decide_abstain(conn, *MODEL_A, cand_ok)
    decide_abstain(conn, *MODEL_G, cand_ok)

    # A catalyst that hard-fails: an eligible candidate with a decision from
    # A but none at all from G -- assert_full_coverage raises ValueError.
    c_bad = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_bad)
    ev_bad = make_event_version(conn, c_bad)
    _e_bad, _instr_bad, cand_bad = make_shared_candidate(conn, ev_bad, "Bad Co")
    record_decision(conn, cand_bad, *MODEL_A, score=0.5, rank=1, selected=True, abstained=False)
    # No arm_g_mechanical decision at all for cand_bad.

    with pytest.raises(ValueError, match="missing a"):
        build_confirmatory_dataset(
            conn, experiment_id, "epoch-1", ENTRY_TS, model_specs={"A": MODEL_A, "G": MODEL_G}
        )
    # The exception itself is the only observable outcome -- there is no
    # report object to inspect, by construction (build_confirmatory_dataset
    # only ever constructs+returns ConfirmatoryBuildReport on its final
    # line, after the entire loop completes without raising).


def test_two_builds_with_the_same_analysis_as_of_are_idempotent(conn, configured):
    experiment_id = make_experiment(conn)
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    _e, instrument_id, candidate_id = make_shared_candidate(conn, ev, "Idem Co")
    decide_trade_with_valid_outcome(conn, experiment_id, "A", *MODEL_A, instrument_id, candidate_id, ev, ENTRY_TS)
    decide_abstain(conn, *MODEL_G, candidate_id)

    analysis_as_of = ENTRY_TS + timedelta(days=2)
    report_1 = build_confirmatory_dataset(conn, experiment_id, "epoch-1", analysis_as_of, {"A": MODEL_A, "G": MODEL_G})
    report_2 = build_confirmatory_dataset(conn, experiment_id, "epoch-1", analysis_as_of, {"A": MODEL_A, "G": MODEL_G})
    assert report_1 == report_2


def test_two_builds_with_different_analysis_as_of_on_the_same_underlying_data_are_consistent(conn, configured):
    """Varying ONLY analysis_as_of on otherwise-identical underlying data:
    a catalyst that's already fully resolved (outcome row exists) must give
    the IDENTICAL classification/return regardless of analysis_as_of (its
    outcome doesn't depend on maturity timing at all, per
    classify_traded_catalyst_maturity's docstring), while a genuinely
    pending catalyst's classification is a well-defined, reproducible
    function of analysis_as_of (still pending before its boundary,
    deterministically overdue after it)."""
    experiment_id = make_experiment(conn)

    c_resolved = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_resolved)
    ev_resolved = make_event_version(conn, c_resolved)
    _er, instr_r, cand_r = make_shared_candidate(conn, ev_resolved, "Resolved Co")
    expected_return = decide_trade_with_valid_outcome(
        conn, experiment_id, "A", *MODEL_A, instr_r, cand_r, ev_resolved, ENTRY_TS
    )
    decide_abstain(conn, *MODEL_G, cand_r)

    c_pending = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_pending)
    ev_pending = make_event_version(conn, c_pending)
    _ep, instr_p, cand_p = make_shared_candidate(conn, ev_pending, "StillPending Co")
    decide_trade_pending(conn, experiment_id, "A", *MODEL_A, instr_p, cand_p, ev_pending, ENTRY_TS)
    decide_abstain(conn, *MODEL_G, cand_p)

    due_at = outcome_due_at(ENTRY_TS)
    boundary = due_at + cb.OUTCOME_PROCESSING_GRACE
    t1 = due_at - timedelta(minutes=30)     # well before c_pending's boundary
    t2 = boundary                            # still within c_pending's boundary (inclusive)

    report_1 = build_confirmatory_dataset(conn, experiment_id, "epoch-1", t1, {"A": MODEL_A, "G": MODEL_G})
    report_2 = build_confirmatory_dataset(conn, experiment_id, "epoch-1", t2, {"A": MODEL_A, "G": MODEL_G})

    for report in (report_1, report_2):
        assert report.pending_count == 1
        assert report.included_count == 1
        assert report.records == [(c_resolved, pytest.approx(expected_return))]


# ===========================================================================
# Promotion-test gating
# ===========================================================================

def _dummy_report(records):
    return ConfirmatoryBuildReport(
        experiment_id="e1", scoring_epoch="epoch-1", analysis_as_of=ENTRY_TS,
        total_catalysts_in_scope=len(records), catalysts_with_zero_eligible_candidates=0,
        pending_count=0, legitimate_exclusion_count=0,
        legitimate_exclusions_by_arm={"A": 0, "G": 0, "both": 0},
        included_count=len(records), records=records,
    )


def test_promotion_test_raises_while_trigger_is_unset():
    report = _dummy_report([("c1", 0.01), ("c2", 0.02)])
    assert cb.CONFIRMATORY_ANALYSIS_TRIGGER is None  # the real, current default
    with pytest.raises(ConfirmatoryAnalysisNotAuthorizedError):
        run_confirmatory_promotion_test(report)


def test_promotion_test_raises_while_trigger_is_set_but_not_yet_satisfied(monkeypatch):
    monkeypatch.setattr(cb, "CONFIRMATORY_ANALYSIS_TRIGGER", lambda report: report.included_count >= 1000)
    report = _dummy_report([("c1", 0.01), ("c2", 0.02)])
    with pytest.raises(ConfirmatoryAnalysisNotAuthorizedError):
        run_confirmatory_promotion_test(report)


def test_promotion_test_proceeds_only_once_trigger_is_set_and_satisfied(monkeypatch):
    monkeypatch.setattr(cb, "CONFIRMATORY_ANALYSIS_TRIGGER", lambda report: report.included_count >= 2)
    records = [(f"c{i}", 0.01) for i in range(5)]
    report = _dummy_report(records)

    result = run_confirmatory_promotion_test(report)

    # Confirms this is the REAL catalyst_clustered_test, actually wired to
    # confirmatory_analysis.py's frozen DELTA/CONFIRMATORY_N_BOOTSTRAP/
    # CONFIRMATORY_BOOTSTRAP_SEED, not a stub -- bit-for-bit reproducible
    # against calling it directly with the same frozen parameters.
    from statistical_test import catalyst_clustered_test
    expected = catalyst_clustered_test(
        records, delta=confirmatory_analysis.DELTA,
        n_bootstrap=confirmatory_analysis.CONFIRMATORY_N_BOOTSTRAP,
        rng=np.random.default_rng(confirmatory_analysis.CONFIRMATORY_BOOTSTRAP_SEED),
    )
    assert result.point_estimate == expected.point_estimate
    assert result.ci_low == expected.ci_low
    assert result.n_clusters == 5
    assert result.n_observations == 5
    assert result.delta == confirmatory_analysis.DELTA
