"""
Null / power simulations for the Section 8 confirmatory test — the kind of
check both review rounds said was the right next adversarial step, now that
the design questions are settled: does the TEST ITSELF behave correctly
before it's ever pointed at real trading data?

Three questions, each answered by simulating data with a KNOWN ground
truth and checking the test recovers it:

1. When there is truly no effect, does the test falsely "detect" one about
   as often as its stated confidence level implies (~5% of the time at
   95%), not much more?
2. When there IS a real, sizeable effect, does the test actually detect it
   most of the time (power), rather than being too conservative to ever
   promote a genuinely working strategy?
3. Does catalyst-level clustering actually matter — i.e., does ignoring it
   (treating correlated observations from the same catalyst as independent)
   produce a test that's fooled much more often than the clustered version?

These are Monte Carlo checks, so exact numbers vary run to run — the
assertions use wide tolerances appropriate to that, not tight ones.
"""
import sys
import os

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from statistical_test import catalyst_clustered_test, naive_iid_test


def make_independent_catalyst_data(rng, n_catalysts, obs_per_catalyst, true_mean, noise_std):
    """Each catalyst's observations are independent draws around true_mean
    — no within-catalyst correlation, the 'easy' case."""
    records = []
    for c in range(n_catalysts):
        for _ in range(obs_per_catalyst):
            records.append((f"catalyst_{c}", rng.normal(true_mean, noise_std)))
    return records


def make_correlated_catalyst_data(rng, n_catalysts, obs_per_catalyst, true_mean,
                                    catalyst_shock_std, idio_std):
    """Each catalyst gets ONE shared random shock; every observation from
    that catalyst inherits it plus a little independent noise. This is the
    realistic case the spec worries about — several trades derived from one
    disclosure aren't independent evidence."""
    records = []
    for c in range(n_catalysts):
        shock = rng.normal(0, catalyst_shock_std)
        for _ in range(obs_per_catalyst):
            records.append((f"catalyst_{c}", true_mean + shock + rng.normal(0, idio_std)))
    return records


def test_false_positive_rate_near_nominal_under_true_null():
    rng = np.random.default_rng(1)
    delta = 0.0
    n_replications = 200
    rejections = 0
    for _ in range(n_replications):
        records = make_independent_catalyst_data(
            rng, n_catalysts=25, obs_per_catalyst=4, true_mean=0.0, noise_std=0.04,
        )
        result = catalyst_clustered_test(records, delta=delta, n_bootstrap=500, rng=rng)
        rejections += result.reject_h0
    false_positive_rate = rejections / n_replications
    # Nominal is 5%; Monte Carlo noise over 200 reps means this needs a wide
    # band, but it should not be wildly inflated (e.g. 20-30%, which would
    # indicate a broken test).
    assert false_positive_rate <= 0.12, (
        f"False-positive rate {false_positive_rate:.3f} is too high for a nominal "
        "5% test -- the test would promote strategies with no real edge too often."
    )


def test_power_is_reasonable_when_a_real_effect_exists():
    rng = np.random.default_rng(2)
    delta = 0.0
    true_effect = 0.03      # a real, meaningful edge
    noise_std = 0.04
    n_replications = 150
    detections = 0
    for _ in range(n_replications):
        records = make_independent_catalyst_data(
            rng, n_catalysts=25, obs_per_catalyst=4, true_mean=true_effect, noise_std=noise_std,
        )
        result = catalyst_clustered_test(records, delta=delta, n_bootstrap=500, rng=rng)
        detections += result.reject_h0
    power = detections / n_replications
    assert power >= 0.5, (
        f"Power {power:.3f} is too low -- a real, sizeable effect should be detected "
        "in a clear majority of runs at this sample size, or the test is too conservative to ever promote anything."
    )


def test_clustering_prevents_a_much_worse_false_positive_rate():
    """The comparison that justifies Section 8's clustering requirement:
    under a TRUE null but with strong within-catalyst correlation, the
    naive (unclustered) test should be fooled far more often than the
    catalyst-clustered one."""
    rng = np.random.default_rng(3)
    delta = 0.0
    n_replications = 120
    clustered_rejections = 0
    naive_rejections = 0
    for _ in range(n_replications):
        records = make_correlated_catalyst_data(
            rng, n_catalysts=15, obs_per_catalyst=15, true_mean=0.0,
            catalyst_shock_std=0.05, idio_std=0.01,
        )
        clustered = catalyst_clustered_test(records, delta=delta, n_bootstrap=400, rng=rng)
        naive = naive_iid_test(records, delta=delta, n_bootstrap=400, rng=rng)
        clustered_rejections += clustered.reject_h0
        naive_rejections += naive.reject_h0

    clustered_fp_rate = clustered_rejections / n_replications
    naive_fp_rate = naive_rejections / n_replications

    # The whole point: naive should be substantially worse than clustered
    # under this correlation structure, and clustered should stay close to nominal.
    assert clustered_fp_rate <= 0.15, (
        f"Clustered test false-positive rate {clustered_fp_rate:.3f} should stay "
        "reasonably close to the 5% nominal rate even under within-catalyst correlation."
    )
    assert naive_fp_rate > clustered_fp_rate, (
        f"Expected the naive (unclustered) test to be fooled more often than the "
        f"clustered one under correlated data, but naive={naive_fp_rate:.3f} <= "
        f"clustered={clustered_fp_rate:.3f}. If this holds up, it means Section 8's "
        "clustering requirement isn't actually doing anything against this kind of "
        "correlation and needs a second look."
    )
