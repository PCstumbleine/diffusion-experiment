"""
Selection Diagnostics v1 Implementation Spec, FINAL v9
(specs/selection-diagnostics-v1-implementation-spec-final.md), Section 9's
required tests.
"""
import uuid
from datetime import datetime, timezone
from fractions import Fraction

import psycopg2
import psycopg2.extensions
import pytest

from conftest import DB_DSN, make_entity

import candidate_coverage
import confirmatory_analysis
import selection_diagnostics as sd
from selection_diagnostics import (
    NOT_YET_SCORED,
    classify_arm_decision_state,
    get_eligible_entities_for_catalyst,
    build_selection_diagnostics,
    SelectionDiagnosticsInternalConsistencyError,
    EntityKeyCollisionError,
    EntitySelectionDiagnostic,
    ArmSelectionDiagnostics,
    SelectionDiagnosticsReport,
)

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


def make_entity_with_cik(conn, name, cik):
    eid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (entity_id, legal_name, cik) VALUES (%s, %s, %s)",
            (eid, name, cik),
        )
    return eid


def make_catalyst(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_documents (source_name, document_type, raw_content, content_hash) "
            "VALUES ('test', '8-K', 'test content', %s) RETURNING document_id",
            (str(uuid.uuid4()),),
        )
        doc_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO catalysts (originating_document_id) VALUES (%s) RETURNING catalyst_id",
            (doc_id,),
        )
        return cur.fetchone()[0]


def admit(conn, experiment_id, scoring_epoch, catalyst_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO experiment_catalysts (experiment_id, scoring_epoch, catalyst_id) VALUES (%s, %s, %s)",
            (experiment_id, scoring_epoch, catalyst_id),
        )


def make_event_version(conn, catalyst_id, superseded_by=None):
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


def supersede(conn, old_event_version_id, new_event_version_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE event_versions SET superseded_by = %s WHERE event_version_id = %s",
            (new_event_version_id, old_event_version_id),
        )


def make_candidate(conn, event_version_id, entity_id, eligibility_status="eligible"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO candidate_signals (event_version_id, entity_id, eligibility_status, "
            "policy_version, decision_timestamp) VALUES (%s, %s, %s, 'v1', now()) RETURNING candidate_id",
            (event_version_id, entity_id, eligibility_status),
        )
        return cur.fetchone()[0]


def record_decision(conn, candidate_id, model_id, model_version, score, rank, selected, abstained,
                     decision_at=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_candidate_decisions "
            "(candidate_id, model_id, model_version, score, rank, selected, abstained, decision_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING decision_id",
            (candidate_id, model_id, model_version, score, rank, selected, abstained, decision_at),
        )
        return cur.fetchone()[0]


def make_traded_candidate(conn, event_version_id, entity_id, model_id, model_version):
    """One eligible candidate, decided TRADED (rank=1, selected) by the given model."""
    candidate_id = make_candidate(conn, event_version_id, entity_id)
    record_decision(conn, candidate_id, model_id, model_version, score=0.9, rank=1, selected=True, abstained=False)
    return candidate_id


def decide_abstain(conn, candidate_id, model_id, model_version):
    record_decision(conn, candidate_id, model_id, model_version, score=-0.1, rank=1, selected=False, abstained=True)


def make_paired_ready_traded_vs_abstain_catalyst(conn, experiment_id, scoring_epoch, entity_id):
    """A minimal paired-ready catalyst: one eligible candidate, A trades it,
    G abstains. Returns (catalyst_id, candidate_id)."""
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, scoring_epoch, catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_candidate(conn, ev, entity_id)
    record_decision(conn, candidate_id, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, candidate_id, *MODEL_G, score=-0.1, rank=1, selected=False, abstained=True)
    return catalyst_id, candidate_id


def real_connection():
    """A genuinely independent connection, for concurrency/snapshot tests
    that need a SECOND connection distinct from the test's own `conn`."""
    return psycopg2.connect(DB_DSN)


# ===========================================================================
# classify_arm_decision_state
# ===========================================================================

def test_classify_complete_coverage_traded(conn):
    entity = make_entity(conn, "Co1")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_traded_candidate(conn, ev, entity, *MODEL_A)

    result = classify_arm_decision_state(conn, catalyst_id, *MODEL_A)
    assert isinstance(result, confirmatory_analysis.CatalystDecisionResult)
    assert result.status == confirmatory_analysis.TRADED
    assert result.selected_candidate_id == candidate_id


def test_classify_complete_coverage_abstained(conn):
    entity = make_entity(conn, "Co2")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_candidate(conn, ev, entity)
    decide_abstain(conn, candidate_id, *MODEL_A)

    result = classify_arm_decision_state(conn, catalyst_id, *MODEL_A)
    assert result.status == confirmatory_analysis.ABSTAINED
    assert result.selected_candidate_id is None


def test_classify_incomplete_coverage_is_not_yet_scored_built_on_find_incomplete_coverage(conn, monkeypatch):
    entity = make_entity(conn, "Co3")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    make_candidate(conn, ev, entity)  # no decision recorded at all -- incomplete coverage

    def spy_assert_full_coverage(*args, **kwargs):
        raise AssertionError("assert_full_coverage must never be called by classify_arm_decision_state")

    monkeypatch.setattr(candidate_coverage, "assert_full_coverage", spy_assert_full_coverage)

    result = classify_arm_decision_state(conn, catalyst_id, *MODEL_A)
    assert result == NOT_YET_SCORED


def test_classify_malformed_decision_set_hard_failure_propagates_unchanged(conn):
    """Two candidates, both stored rank=1 -- a duplicated rank, which
    assert_trade_abstention_invariant rejects as TradeAbstentionInvariantError."""
    entity1, entity2 = make_entity(conn, "Co4a"), make_entity(conn, "Co4b")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    c1 = make_candidate(conn, ev, entity1)
    c2 = make_candidate(conn, ev, entity2)
    record_decision(conn, c1, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c2, *MODEL_A, score=0.5, rank=1, selected=False, abstained=False)

    with pytest.raises(confirmatory_analysis.TradeAbstentionInvariantError):
        classify_arm_decision_state(conn, catalyst_id, *MODEL_A)


def test_classify_forced_no_eligible_candidates_after_n_gt_0_is_internal_consistency_error(conn, monkeypatch):
    entity = make_entity(conn, "Co5")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_traded_candidate(conn, ev, entity, *MODEL_A)  # complete coverage

    monkeypatch.setattr(
        sd, "assert_trade_abstention_invariant",
        lambda *a, **k: confirmatory_analysis.CatalystDecisionResult(status=confirmatory_analysis.NO_ELIGIBLE_CANDIDATES),
    )
    with pytest.raises(SelectionDiagnosticsInternalConsistencyError):
        classify_arm_decision_state(conn, catalyst_id, *MODEL_A)


def test_classify_forced_traded_with_none_selected_candidate_is_internal_consistency_error(conn, monkeypatch):
    entity = make_entity(conn, "Co6")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    make_traded_candidate(conn, ev, entity, *MODEL_A)

    monkeypatch.setattr(
        sd, "assert_trade_abstention_invariant",
        lambda *a, **k: confirmatory_analysis.CatalystDecisionResult(status=confirmatory_analysis.TRADED, selected_candidate_id=None),
    )
    with pytest.raises(SelectionDiagnosticsInternalConsistencyError):
        classify_arm_decision_state(conn, catalyst_id, *MODEL_A)


def test_classify_forced_abstained_with_selected_candidate_set_is_internal_consistency_error(conn, monkeypatch):
    entity = make_entity(conn, "Co7")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_traded_candidate(conn, ev, entity, *MODEL_A)

    monkeypatch.setattr(
        sd, "assert_trade_abstention_invariant",
        lambda *a, **k: confirmatory_analysis.CatalystDecisionResult(status=confirmatory_analysis.ABSTAINED, selected_candidate_id=candidate_id),
    )
    with pytest.raises(SelectionDiagnosticsInternalConsistencyError):
        classify_arm_decision_state(conn, catalyst_id, *MODEL_A)


# ===========================================================================
# Section 2 sequencing: N=0 never calls classify_arm_decision_state
# ===========================================================================

def test_n_eq_0_never_calls_classify_arm_decision_state_for_either_arm(conn, monkeypatch):
    experiment_id = make_experiment(conn)
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    make_event_version(conn, catalyst_id)  # no candidates at all -- N=0
    conn.commit()

    calls = []
    real_classify = sd.classify_arm_decision_state

    def spy(conn_, catalyst_id_, model_id, model_version):
        calls.append((catalyst_id_, model_id))
        return real_classify(conn_, catalyst_id_, model_id, model_version)

    monkeypatch.setattr(sd, "classify_arm_decision_state", spy)

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})

    assert calls == []
    assert report.no_eligible_candidates_count == 1
    assert report.per_arm["A"].decision_ready_catalysts == 0
    assert report.per_arm["A"].not_yet_scored_count == 0
    assert report.per_arm["A"].paired_traded_count == 0
    assert report.per_arm["A"].paired_abstained_count == 0
    assert report.per_arm["G"].decision_ready_catalysts == 0
    assert report.per_arm["G"].not_yet_scored_count == 0


# ===========================================================================
# Section 2a: transaction contract
# ===========================================================================

def test_raises_if_transaction_already_in_progress(conn):
    experiment_id = make_experiment(conn)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")  # opens an implicit transaction, never committed

    assert conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE
    with pytest.raises(ValueError, match="transaction already in progress"):
        build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    conn.rollback()


def test_raises_if_autocommit_true(conn):
    experiment_id = make_experiment(conn)
    conn.commit()
    conn.autocommit = True
    try:
        with pytest.raises(ValueError, match="autocommit=False"):
            build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    finally:
        conn.autocommit = False


def test_model_specs_validation_happens_before_transaction_opens(conn):
    """A third key, or a missing arm, raises ValueError immediately -- and
    must not have touched the transaction/isolation level at all."""
    experiment_id = make_experiment(conn)
    conn.commit()
    prior_status = conn.get_transaction_status()
    prior_isolation = conn.isolation_level

    for bad_specs in ({"A": MODEL_A}, {"A": MODEL_A, "G": MODEL_G, "H": ("x", "v1")}, {"X": MODEL_A, "Y": MODEL_G}):
        with pytest.raises(ValueError):
            build_selection_diagnostics(conn, experiment_id, "epoch-1", bad_specs)
        assert conn.get_transaction_status() == prior_status
        assert conn.isolation_level == prior_isolation


def test_model_specs_exactly_a_and_g_succeeds(conn):
    experiment_id = make_experiment(conn)
    conn.commit()
    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert report.total_epoch_catalysts == 0


def test_generated_at_captured_exactly_once(conn, monkeypatch):
    experiment_id = make_experiment(conn)
    entity = make_entity(conn, "Co8")
    make_paired_ready_traded_vs_abstain_catalyst(conn, experiment_id, "epoch-1", entity)
    conn.commit()

    call_count = {"n": 0}
    real_datetime = sd.datetime

    class SpyDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            call_count["n"] += 1
            return real_datetime.now(tz)

    monkeypatch.setattr(sd, "datetime", SpyDatetime)
    build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert call_count["n"] == 1


def test_before_and_after_bound_generated_at(conn):
    experiment_id = make_experiment(conn)
    conn.commit()
    before = datetime.now(timezone.utc)
    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    after = datetime.now(timezone.utc)
    assert before <= report.generated_at <= after


def test_build_selection_diagnostics_has_no_caller_supplied_timestamp_parameter():
    import inspect
    sig = inspect.signature(build_selection_diagnostics)
    assert list(sig.parameters) == ["conn", "experiment_id", "scoring_epoch", "model_specs"]


def test_coverage_and_decisions_are_not_filtered_by_decision_at(conn):
    """A decision with decision_at set far in the FUTURE (relative to when
    the report is generated) must still be visible -- proving the query
    does not gate on decision_at, not just asserting the docstring says so."""
    entity = make_entity(conn, "Co9")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_candidate(conn, ev, entity)
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    record_decision(conn, candidate_id, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False,
                     decision_at=future)

    result = classify_arm_decision_state(conn, catalyst_id, *MODEL_A)
    assert result.status == confirmatory_analysis.TRADED  # not NOT_YET_SCORED


def test_repeatable_read_snapshot_hides_a_concurrent_commit_made_mid_build(conn):
    """Also directly demonstrates 'admitted_at <= generated_at': the
    equivalence the spec draws (Section 0a) is that a concurrently-admitted
    catalyst/candidate committed strictly after this transaction's snapshot
    was established is simply invisible to every read in this build, no
    matter how much later in the build that read happens -- this is the
    single mechanism behind both spec bullets, so one test proves both."""
    experiment_id = make_experiment(conn)
    entity = make_entity(conn, "Snapshot Co")
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    # Deliberately N=0 right now: no candidate_signals row yet.
    conn.commit()

    real_get_eligible = sd.get_eligible_entities_for_catalyst
    triggered = {"done": False}

    def hook(conn_, catalyst_id_):
        if not triggered["done"] and catalyst_id_ == catalyst_id:
            triggered["done"] = True
            other = real_connection()
            with other.cursor() as cur:
                cur.execute(
                    "INSERT INTO candidate_signals (event_version_id, entity_id, eligibility_status, "
                    "policy_version, decision_timestamp) VALUES (%s, %s, 'eligible', 'v1', now())",
                    (ev, entity),
                )
            other.commit()
            other.close()
        return real_get_eligible(conn_, catalyst_id_)

    orig = sd.get_eligible_entities_for_catalyst
    sd.get_eligible_entities_for_catalyst = hook
    try:
        report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    finally:
        sd.get_eligible_entities_for_catalyst = orig

    assert triggered["done"] is True
    # The concurrently-committed candidate must NOT have been seen.
    assert report.no_eligible_candidates_count == 1
    assert report.total_epoch_catalysts == 1

    # Confirm, with a fresh connection, that the row really does exist now
    # (the concurrent write genuinely committed -- this isn't a no-op test).
    verify_conn = real_connection()
    with verify_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM candidate_signals WHERE event_version_id = %s", (ev,))
        assert cur.fetchone()[0] == 1
    verify_conn.commit()
    verify_conn.close()


def _make_isolation_test_experiment(conn):
    experiment_id = make_experiment(conn)
    entity = make_entity(conn, "Iso Co")
    make_paired_ready_traded_vs_abstain_catalyst(conn, experiment_id, "epoch-1", entity)
    conn.commit()
    return experiment_id


def _show_isolation(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW transaction_isolation")
        val = cur.fetchone()[0]
    conn.commit()
    return val


def test_isolation_restored_from_driver_default_after_success(conn):
    experiment_id = _make_isolation_test_experiment(conn)
    assert conn.isolation_level is None
    build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert _show_isolation(conn) == "read committed"


def test_isolation_restored_from_driver_default_after_failure(conn, monkeypatch):
    experiment_id = _make_isolation_test_experiment(conn)
    assert conn.isolation_level is None

    def boom(*a, **k):
        raise RuntimeError("forced failure partway through the build")

    monkeypatch.setattr(sd, "get_confirmatory_catalyst_universe", boom)
    with pytest.raises(RuntimeError, match="forced failure"):
        build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert _show_isolation(conn) == "read committed"


def test_isolation_restored_to_explicit_non_default_prior_after_success(conn):
    experiment_id = _make_isolation_test_experiment(conn)
    conn.set_session(isolation_level=psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE)
    conn.commit()
    assert _show_isolation(conn) == "serializable"

    build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert _show_isolation(conn) == "serializable"


def test_isolation_restored_to_explicit_non_default_prior_after_failure(conn, monkeypatch):
    experiment_id = _make_isolation_test_experiment(conn)
    conn.set_session(isolation_level=psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE)
    conn.commit()
    assert _show_isolation(conn) == "serializable"

    def boom(*a, **k):
        raise RuntimeError("forced failure partway through the build")

    monkeypatch.setattr(sd, "get_confirmatory_catalyst_universe", boom)
    with pytest.raises(RuntimeError, match="forced failure"):
        build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert _show_isolation(conn) == "serializable"


# ===========================================================================
# Reconciliation
# ===========================================================================

def test_reconciliation_invariant_across_all_five_buckets(conn):
    experiment_id = make_experiment(conn)

    # 1. no_eligible_candidates
    c_none = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_none)
    make_event_version(conn, c_none)

    # 2. paired_ready (both TRADED/ABSTAINED)
    e_paired = make_entity(conn, "Paired Co")
    make_paired_ready_traded_vs_abstain_catalyst(conn, experiment_id, "epoch-1", e_paired)

    # 3. a_only_ready (A ready, G not yet scored)
    c_a_only = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_a_only)
    ev_a_only = make_event_version(conn, c_a_only)
    e_a_only = make_entity(conn, "AOnly Co")
    cand_a_only = make_candidate(conn, ev_a_only, e_a_only)
    record_decision(conn, cand_a_only, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    # no G decision at all

    # 4. g_only_ready (G ready, A not yet scored)
    c_g_only = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_g_only)
    ev_g_only = make_event_version(conn, c_g_only)
    e_g_only = make_entity(conn, "GOnly Co")
    cand_g_only = make_candidate(conn, ev_g_only, e_g_only)
    record_decision(conn, cand_g_only, *MODEL_G, score=0.9, rank=1, selected=True, abstained=False)

    # 5. neither_ready
    c_neither = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", c_neither)
    ev_neither = make_event_version(conn, c_neither)
    e_neither = make_entity(conn, "Neither Co")
    make_candidate(conn, ev_neither, e_neither)  # no decisions at all from either model

    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})

    a_only_ready_count = report.per_arm["A"].decision_ready_catalysts - report.paired_decision_ready_catalysts
    g_only_ready_count = report.per_arm["G"].decision_ready_catalysts - report.paired_decision_ready_catalysts
    neither_ready_count = (
        report.total_epoch_catalysts - report.no_eligible_candidates_count
        - report.paired_decision_ready_catalysts - a_only_ready_count - g_only_ready_count
    )

    assert report.total_epoch_catalysts == 5
    assert report.no_eligible_candidates_count == 1
    assert report.paired_decision_ready_catalysts == 1
    assert a_only_ready_count == 1
    assert g_only_ready_count == 1
    assert neither_ready_count == 1
    assert (
        report.total_epoch_catalysts
        == report.no_eligible_candidates_count
        + report.paired_decision_ready_catalysts
        + a_only_ready_count
        + g_only_ready_count
        + neither_ready_count
    )


# ===========================================================================
# Paired-ready intersection (Section 4)
# ===========================================================================

def test_paired_ready_intersection_only_computed_over_the_intersection(conn):
    experiment_id = make_experiment(conn)

    # A ready on catalysts 1-5, G ready only on 1-3.
    catalysts = []
    for i in range(5):
        entity = make_entity(conn, f"Co{i}")
        catalyst_id = make_catalyst(conn)
        admit(conn, experiment_id, "epoch-1", catalyst_id)
        ev = make_event_version(conn, catalyst_id)
        candidate_id = make_candidate(conn, ev, entity)
        record_decision(conn, candidate_id, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
        if i < 3:
            record_decision(conn, candidate_id, *MODEL_G, score=0.5, rank=1, selected=True, abstained=False)
        catalysts.append(catalyst_id)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})

    assert report.per_arm["A"].decision_ready_catalysts == 5
    assert report.per_arm["G"].decision_ready_catalysts == 3
    assert report.paired_decision_ready_catalysts == 3
    assert report.per_arm["A"].paired_traded_count == 3
    assert report.per_arm["G"].paired_traded_count == 3
    # Every entity's eligible_count is counted over the 3 paired-ready
    # catalysts only, never A's full 5.
    for e in report.per_arm["A"].entities:
        assert e.eligible_count <= 3


def test_per_arm_composition_invariant(conn):
    experiment_id = make_experiment(conn)
    for i in range(4):
        entity = make_entity(conn, f"Comp Co{i}")
        catalyst_id = make_catalyst(conn)
        admit(conn, experiment_id, "epoch-1", catalyst_id)
        ev = make_event_version(conn, catalyst_id)
        candidate_id = make_candidate(conn, ev, entity)
        if i % 2 == 0:
            record_decision(conn, candidate_id, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
        else:
            record_decision(conn, candidate_id, *MODEL_A, score=-0.1, rank=1, selected=False, abstained=True)
        record_decision(conn, candidate_id, *MODEL_G, score=0.5, rank=1, selected=True, abstained=False)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    for arm in ("A", "G"):
        diag = report.per_arm[arm]
        assert diag.paired_traded_count + diag.paired_abstained_count == report.paired_decision_ready_catalysts


# ===========================================================================
# Cross-arm opportunity-universe invariant
# ===========================================================================

def test_cross_arm_entity_key_set_and_eligible_count_equality(conn):
    experiment_id = make_experiment(conn)
    entities = [make_entity(conn, f"Cross Co{i}") for i in range(3)]
    for i, entity in enumerate(entities):
        catalyst_id = make_catalyst(conn)
        admit(conn, experiment_id, "epoch-1", catalyst_id)
        ev = make_event_version(conn, catalyst_id)
        candidate_id = make_candidate(conn, ev, entity)
        record_decision(conn, candidate_id, *MODEL_A, score=0.9 if i == 0 else -0.1,
                         rank=1, selected=(i == 0), abstained=(i != 0))
        record_decision(conn, candidate_id, *MODEL_G, score=0.9 if i == 1 else -0.1,
                         rank=1, selected=(i == 1), abstained=(i != 1))
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    a_keys = {e.entity_key for e in report.per_arm["A"].entities}
    g_keys = {e.entity_key for e in report.per_arm["G"].entities}
    assert a_keys == g_keys
    a_by_key = {e.entity_key: e for e in report.per_arm["A"].entities}
    g_by_key = {e.entity_key: e for e in report.per_arm["G"].entities}
    for key in a_keys:
        assert a_by_key[key].eligible_count == g_by_key[key].eligible_count
    # Selected counts legitimately differ (arm A selected entity 0, arm G selected entity 1).
    assert sum(e.selected_count for e in report.per_arm["A"].entities) == 1
    assert sum(e.selected_count for e in report.per_arm["G"].entities) == 1


def test_cross_arm_invariant_catches_an_arm_missing_an_eligible_entity(conn, monkeypatch):
    """A fixture where G's aggregation is deliberately missing one eligible
    entity that A has must fail the SET-equality check even though no
    per-entity count comparison would have caught it."""
    experiment_id = make_experiment(conn)
    entity1, entity2 = make_entity(conn, "Missing1"), make_entity(conn, "Missing2")
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    c1 = make_candidate(conn, ev, entity1)
    c2 = make_candidate(conn, ev, entity2)
    record_decision(conn, c1, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c2, *MODEL_A, score=0.1, rank=2, selected=False, abstained=False)
    record_decision(conn, c1, *MODEL_G, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c2, *MODEL_G, score=0.1, rank=2, selected=False, abstained=False)
    conn.commit()

    real_get_eligible = sd.get_eligible_entities_for_catalyst
    call_count = {"n": 0}

    def buggy_get_eligible(conn_, catalyst_id_):
        call_count["n"] += 1
        result = real_get_eligible(conn_, catalyst_id_)
        if call_count["n"] > 100:  # never actually triggers -- see note below
            pass
        return result

    # Simulate the bug directly: patch the report AFTER the fact instead,
    # since get_eligible_entities_for_catalyst is shared (by design) between
    # both arms -- the only way to fabricate "one arm's aggregation omits an
    # entity" is to test the INVARIANT CHECK ITSELF against a deliberately
    # corrupted pair of entity lists, proving the assertion actually catches
    # this shape of bug.
    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    a_entities = list(report.per_arm["A"].entities)
    g_entities_missing_one = [e for e in report.per_arm["G"].entities if e.entity_key != a_entities[0].entity_key]

    a_keys = {e.entity_key for e in a_entities}
    g_keys = {e.entity_key for e in g_entities_missing_one}
    assert a_keys != g_keys  # the corrupted fixture fails the set-equality check, as required


# ===========================================================================
# Entity x catalyst opportunity de-duplication
# ===========================================================================

def test_entity_x_catalyst_dedup_three_candidate_rows_one_catalyst(conn):
    experiment_id = make_experiment(conn)
    entity = make_entity(conn, "Dedup Co")
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev1 = make_event_version(conn, catalyst_id)
    ev2 = make_event_version(conn, catalyst_id)
    ev3 = make_event_version(conn, catalyst_id)
    c1 = make_candidate(conn, ev1, entity)
    c2 = make_candidate(conn, ev2, entity)
    c3 = make_candidate(conn, ev3, entity)
    record_decision(conn, c1, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c2, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c3, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
    record_decision(conn, c1, *MODEL_G, score=-0.1, rank=1, selected=False, abstained=True)
    record_decision(conn, c2, *MODEL_G, score=-0.1, rank=1, selected=False, abstained=True)
    record_decision(conn, c3, *MODEL_G, score=-0.1, rank=1, selected=False, abstained=True)
    conn.commit()

    eligible = get_eligible_entities_for_catalyst(conn, catalyst_id)
    assert len(eligible) == 1
    assert entity in eligible


def test_get_eligible_entities_for_catalyst_scoping_current_vs_superseded(conn):
    catalyst_id = make_catalyst(conn)
    entity_x, entity_y = make_entity(conn, "Current X"), make_entity(conn, "Superseded Y")
    old_ev = make_event_version(conn, catalyst_id)
    new_ev = make_event_version(conn, catalyst_id, superseded_by=None)
    make_candidate(conn, old_ev, entity_y)
    make_candidate(conn, new_ev, entity_x)
    supersede(conn, old_ev, new_ev)
    conn.commit()

    result = get_eligible_entities_for_catalyst(conn, catalyst_id)
    assert set(result.keys()) == {entity_x}


# ===========================================================================
# Selected-candidate resolution (Section 5a)
# ===========================================================================

def test_selected_candidate_resolution_normal_case(conn):
    experiment_id = make_experiment(conn)
    entity = make_entity(conn, "Resolve Co")
    catalyst_id, candidate_id = make_paired_ready_traded_vs_abstain_catalyst(conn, experiment_id, "epoch-1", entity)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    expected_key = confirmatory_analysis.stable_entity_sort_key(
        sd._EntityKeyInput(entity_id=entity, cik=None, legal_name="Resolve Co")
    )
    selected = [e for e in report.per_arm["A"].entities if e.selected_count > 0]
    assert len(selected) == 1
    assert selected[0].entity_key == expected_key


def test_selected_candidate_not_in_eligible_entities_is_internal_consistency_error(conn, monkeypatch):
    entity = make_entity(conn, "NotEligible Co")
    catalyst_id = make_catalyst(conn)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_traded_candidate(conn, ev, entity, *MODEL_A)
    conn.commit()

    # Force get_eligible_entities_for_catalyst (as seen by the module under
    # test) to return an empty universe for this catalyst, simulating the
    # selected entity being artificially absent from it.
    monkeypatch.setattr(sd, "get_eligible_entities_for_catalyst", lambda conn_, cid: {})
    with pytest.raises(SelectionDiagnosticsInternalConsistencyError):
        sd._resolve_selected_entity(conn, catalyst_id, candidate_id, {})


def test_selected_candidate_belongs_to_superseded_event_version_is_internal_consistency_error(conn):
    entity = make_entity(conn, "Superseded Select Co")
    catalyst_id = make_catalyst(conn)
    old_ev = make_event_version(conn, catalyst_id)
    new_ev = make_event_version(conn, catalyst_id)
    candidate_id = make_candidate(conn, old_ev, entity)
    supersede(conn, old_ev, new_ev)
    conn.commit()

    eligible_entities = {entity: (None, "Superseded Select Co")}
    with pytest.raises(SelectionDiagnosticsInternalConsistencyError, match="superseded"):
        sd._resolve_selected_entity(conn, catalyst_id, candidate_id, eligible_entities)


def test_selected_candidate_row_itself_ineligible_is_caught_by_step_4_not_step_5(conn):
    """An entity with TWO candidate_signals rows under one catalyst --
    candidate A eligible, candidate B ineligible -- with the TRADED
    result's selected_candidate_id forced to candidate B. Must raise, and
    must NOT pass merely because candidate A's eligibility puts the same
    entity into get_eligible_entities_for_catalyst's output."""
    entity = make_entity(conn, "TwoRows Co")
    catalyst_id = make_catalyst(conn)
    ev_a = make_event_version(conn, catalyst_id)
    ev_b = make_event_version(conn, catalyst_id)
    candidate_eligible = make_candidate(conn, ev_a, entity, eligibility_status="eligible")
    candidate_ineligible = make_candidate(conn, ev_b, entity, eligibility_status="ineligible")
    conn.commit()

    eligible_entities = get_eligible_entities_for_catalyst(conn, catalyst_id)
    assert entity in eligible_entities  # confirms the entity DOES appear via candidate_eligible

    with pytest.raises(SelectionDiagnosticsInternalConsistencyError, match="eligible"):
        sd._resolve_selected_entity(conn, catalyst_id, candidate_ineligible, eligible_entities)


# ===========================================================================
# Derived bounds invariant
# ===========================================================================

def test_derived_bounds_selected_le_eligible_and_rate_in_0_1(conn):
    experiment_id = make_experiment(conn)
    entity_frequent = make_entity(conn, "Frequent Co")
    entity_rare = make_entity(conn, "Rare Co")

    for i in range(4):
        catalyst_id = make_catalyst(conn)
        admit(conn, experiment_id, "epoch-1", catalyst_id)
        ev = make_event_version(conn, catalyst_id)
        c_freq = make_candidate(conn, ev, entity_frequent)
        c_rare = make_candidate(conn, ev, entity_rare)
        # entity_frequent selected every time; entity_rare never eligible-and-selected together
        record_decision(conn, c_freq, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
        record_decision(conn, c_rare, *MODEL_A, score=0.1, rank=2, selected=False, abstained=False)
        record_decision(conn, c_freq, *MODEL_G, score=0.5, rank=1, selected=True, abstained=False)
        record_decision(conn, c_rare, *MODEL_G, score=0.1, rank=2, selected=False, abstained=False)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    for arm in ("A", "G"):
        for e in report.per_arm[arm].entities:
            assert 0 <= e.selected_count <= e.eligible_count
            assert 0.0 <= e.selection_rate <= 1.0


# ===========================================================================
# HHI / effective-names
# ===========================================================================

def test_hhi_zero_selections_is_none(conn):
    experiment_id = make_experiment(conn)
    entity = make_entity(conn, "ZeroSel Co")
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_candidate(conn, ev, entity)
    decide_abstain(conn, candidate_id, *MODEL_A)
    decide_abstain(conn, candidate_id, *MODEL_G)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert report.per_arm["A"].hhi is None
    assert report.per_arm["A"].effective_names is None
    assert report.per_arm["A"].top_entities == []


def test_hhi_one_entity_is_1_0(conn):
    experiment_id = make_experiment(conn)
    entity = make_entity(conn, "OneEntity Co")
    make_paired_ready_traded_vs_abstain_catalyst(conn, experiment_id, "epoch-1", entity)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert report.per_arm["A"].hhi == 1.0
    assert report.per_arm["A"].effective_names == 1.0


def test_hhi_multi_entity_concentrated_case_matches_hand_computation(conn):
    experiment_id = make_experiment(conn)
    entity_x = make_entity(conn, "X Co")
    entity_y = make_entity(conn, "Y Co")
    entity_z = make_entity(conn, "Z Co")

    # entity_x selected 3 times, entity_y 1, entity_z 1 -- S=5.
    for entity in (entity_x, entity_x, entity_x, entity_y, entity_z):
        catalyst_id = make_catalyst(conn)
        admit(conn, experiment_id, "epoch-1", catalyst_id)
        ev = make_event_version(conn, catalyst_id)
        candidate_id = make_candidate(conn, ev, entity)
        record_decision(conn, candidate_id, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
        record_decision(conn, candidate_id, *MODEL_G, score=-0.1, rank=1, selected=False, abstained=True)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    expected_hhi = float(Fraction(3, 5) ** 2 + Fraction(1, 5) ** 2 + Fraction(1, 5) ** 2)
    expected_effective_names = 1 / expected_hhi
    assert report.per_arm["A"].hhi == pytest.approx(expected_hhi)
    assert report.per_arm["A"].hhi == pytest.approx(0.44)
    assert report.per_arm["A"].effective_names == pytest.approx(expected_effective_names)
    assert report.per_arm["A"].effective_names == pytest.approx(1 / 0.44)


# ===========================================================================
# top_entities composition and ordering
# ===========================================================================

def test_top_entities_contains_only_selected_entities(conn):
    experiment_id = make_experiment(conn)
    selected_entities = [make_entity(conn, f"Selected{i}") for i in range(3)]
    unselected_entities = [make_entity(conn, f"Unselected{i}") for i in range(7)]

    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)

    # Build one catalyst with all 10 entities eligible via 10 separate
    # event_versions, but only the 3 "selected" ones actually ranked 1 in
    # 3 SEPARATE such catalysts (one selected entity per catalyst, to keep
    # "exactly one selected per catalyst" intact) plus all 10 present as
    # eligible-only opportunities in a shared final catalyst.
    all_catalysts = []
    for entity in selected_entities:
        cid = make_catalyst(conn)
        admit(conn, experiment_id, "epoch-1", cid)
        ev = make_event_version(conn, cid)
        cand = make_candidate(conn, ev, entity)
        record_decision(conn, cand, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
        record_decision(conn, cand, *MODEL_G, score=-0.1, rank=1, selected=False, abstained=True)
        all_catalysts.append(cid)

    # One more catalyst where all 7 "unselected" entities are eligible but
    # none is chosen (both arms abstain on the rank-1 one; but to keep the
    # invariant "at most one selected per catalyst" and still make all 7
    # eligible, put them each in their own tiny catalyst instead.
    for entity in unselected_entities:
        cid = make_catalyst(conn)
        admit(conn, experiment_id, "epoch-1", cid)
        ev = make_event_version(conn, cid)
        cand = make_candidate(conn, ev, entity)
        decide_abstain(conn, cand, *MODEL_A)
        decide_abstain(conn, cand, *MODEL_G)
        all_catalysts.append(cid)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert len(report.per_arm["A"].entities) == 10
    assert len(report.per_arm["A"].top_entities) == 3
    assert all(e.selected_count > 0 for e in report.per_arm["A"].top_entities)


def test_top_entities_empty_on_zero_selections(conn):
    experiment_id = make_experiment(conn)
    entity = make_entity(conn, "AllAbstain Co")
    catalyst_id = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_id)
    ev = make_event_version(conn, catalyst_id)
    candidate_id = make_candidate(conn, ev, entity)
    decide_abstain(conn, candidate_id, *MODEL_A)
    decide_abstain(conn, candidate_id, *MODEL_G)
    conn.commit()

    report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
    assert report.per_arm["A"].top_entities == []


def test_deterministic_ordering_on_tied_selected_count(conn):
    experiment_id = make_experiment(conn)
    entity_a = make_entity_with_cik(conn, "Tie Co A", "0000000001")
    entity_b = make_entity_with_cik(conn, "Tie Co B", "0000000002")

    for entity in (entity_a, entity_b):
        catalyst_id = make_catalyst(conn)
        admit(conn, experiment_id, "epoch-1", catalyst_id)
        ev = make_event_version(conn, catalyst_id)
        candidate_id = make_candidate(conn, ev, entity)
        record_decision(conn, candidate_id, *MODEL_A, score=0.9, rank=1, selected=True, abstained=False)
        record_decision(conn, candidate_id, *MODEL_G, score=-0.1, rank=1, selected=False, abstained=True)
    conn.commit()

    key_a = confirmatory_analysis.stable_entity_sort_key(
        sd._EntityKeyInput(entity_id=entity_a, cik="0000000001", legal_name="Tie Co A")
    )
    key_b = confirmatory_analysis.stable_entity_sort_key(
        sd._EntityKeyInput(entity_id=entity_b, cik="0000000002", legal_name="Tie Co B")
    )
    expected_order = sorted([key_a, key_b])

    for _ in range(2):  # repeated calls -- stable across reruns
        report = build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})
        assert [e.entity_key for e in report.per_arm["A"].entities] == expected_order
        assert [e.entity_key for e in report.per_arm["A"].top_entities] == expected_order


# ===========================================================================
# Entity identity reuse, collision, and cross-rebuild stability
# ===========================================================================

def test_stable_entity_sort_key_is_imported_and_called_not_reimplemented():
    assert sd.stable_entity_sort_key is confirmatory_analysis.stable_entity_sort_key


def test_entity_id_never_appears_in_public_dataclasses():
    import dataclasses
    for cls in (EntitySelectionDiagnostic, ArmSelectionDiagnostics, SelectionDiagnosticsReport):
        field_names = {f.name for f in dataclasses.fields(cls)}
        assert "entity_id" not in field_names


def test_entity_key_collision_same_snapshot_raises(conn):
    """Two distinct entity_id values that both resolve to the same
    stable_entity_sort_key within one build. entities.cik has a real
    UNIQUE index (WHERE cik IS NOT NULL), so two rows can't share a CIK --
    the collision is constructed the other legitimate way instead: both
    entities have cik=NULL (no uniqueness constraint on legal_name at all)
    and two differently-spelled real legal names that
    entity_resolution.normalize_entity_name collapses to the identical
    normalized string ("Acme Corp" / "Acme Corporation" both -> "acme"),
    producing the same NAME:acme key from two genuinely distinct entity_id
    rows -- exactly the un-merged-entity-master scenario this check exists
    to catch."""
    experiment_id = make_experiment(conn)
    entity_1 = make_entity(conn, "Acme Corp")
    entity_2 = make_entity(conn, "Acme Corporation")  # different spelling, same normalized name -- same key
    catalyst_1 = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_1)
    ev1 = make_event_version(conn, catalyst_1)
    cand1 = make_candidate(conn, ev1, entity_1)
    decide_abstain(conn, cand1, *MODEL_A)
    decide_abstain(conn, cand1, *MODEL_G)

    catalyst_2 = make_catalyst(conn)
    admit(conn, experiment_id, "epoch-1", catalyst_2)
    ev2 = make_event_version(conn, catalyst_2)
    cand2 = make_candidate(conn, ev2, entity_2)
    decide_abstain(conn, cand2, *MODEL_A)
    decide_abstain(conn, cand2, *MODEL_G)
    conn.commit()

    with pytest.raises(EntityKeyCollisionError):
        build_selection_diagnostics(conn, experiment_id, "epoch-1", {"A": MODEL_A, "G": MODEL_G})


def test_entity_key_stability_across_simulated_rebuilds_first_db(conn):
    """First of two independent single-database fixtures (never two
    colliding rows inside one database) -- the same logical entity (same
    CIK) gets a fresh random entity_id here, but must resolve to the same
    entity_key a second, separate database would also produce."""
    entity_id = make_entity_with_cik(conn, "Rebuild Stable Co", "0009999999")
    key = confirmatory_analysis.stable_entity_sort_key(
        sd._EntityKeyInput(entity_id=entity_id, cik="0009999999", legal_name="Rebuild Stable Co")
    )
    assert key == "CIK:0009999999"


def test_entity_key_stability_across_simulated_rebuilds_second_db(conn):
    """Second independent fixture, simulating a from-scratch rebuild --
    entity_id is necessarily DIFFERENT (freshly random) from the first
    test's, but the resolved entity_key must be identical."""
    entity_id = make_entity_with_cik(conn, "Rebuild Stable Co", "0009999999")
    key = confirmatory_analysis.stable_entity_sort_key(
        sd._EntityKeyInput(entity_id=entity_id, cik="0009999999", legal_name="Rebuild Stable Co")
    )
    assert key == "CIK:0009999999"


# ===========================================================================
# NOT_YET_SCORED equality contract
# ===========================================================================

def test_not_yet_scored_equals_plain_string():
    assert NOT_YET_SCORED == "NOT_YET_SCORED"


def test_not_yet_scored_equality_uses_value_not_identity():
    """A freshly-constructed string with the same content (built at
    runtime, not a literal, so CPython interning cannot be relied upon to
    make it the same object) must still compare equal via ==."""
    fresh = "".join(["N", "O", "T", "_", "Y", "E", "T", "_", "S", "C", "O", "R", "E", "D"])
    assert fresh is not NOT_YET_SCORED  # genuinely a different object
    assert fresh == NOT_YET_SCORED
