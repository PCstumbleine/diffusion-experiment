"""
The Section 8 confirmatory test: is Arm A's advantage over Arm G bigger
than delta, after both are already net of costs?

    H0: mu(A - G) <= delta
    H1: mu(A - G) >  delta
    Promotion requires the CI's LOWER BOUND to exceed delta.

Inference is clustered at the catalyst level (Section 1's catalyst_id) via
a block bootstrap: one earnings release that spun off five separate trades
is one piece of evidence about whether the strategy works, not five —
resampling individual observations instead of catalysts would silently
treat those five as independent and understate uncertainty.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TestResult:
    point_estimate: float
    ci_low: float
    ci_high: float | None
    n_clusters: int
    n_observations: int
    delta: float
    reject_h0: bool  # True = promotion gate clears


def _group_by_catalyst(records: list[tuple[str, float]]) -> dict[str, np.ndarray]:
    by_catalyst: dict[str, list[float]] = {}
    for catalyst_id, diff in records:
        by_catalyst.setdefault(catalyst_id, []).append(diff)
    return {cid: np.array(vals) for cid, vals in by_catalyst.items()}


def catalyst_clustered_test(
    records: list[tuple[str, float]],
    delta: float,
    confidence_level: float = 0.95,
    n_bootstrap: int = 2000,
    alternative: str = "one_sided",
    rng: np.random.Generator | None = None,
) -> TestResult:
    """records: list of (catalyst_id, diff) where diff = A_net_return - G_net_return
    for one arm-A/arm-G pairing on one event, already net of costs (Section 8:
    delta is NOT re-derived from the cost model here, on purpose — that's
    the double-counting bug the fourth review round caught; costs are
    assumed already subtracted from the returns going into `diff`)."""
    if rng is None:
        rng = np.random.default_rng()
    if alternative not in ("one_sided", "two_sided"):
        raise ValueError("alternative must be 'one_sided' or 'two_sided'")

    by_catalyst = _group_by_catalyst(records)
    catalyst_ids = list(by_catalyst.keys())
    n_clusters = len(catalyst_ids)
    n_observations = len(records)
    if n_clusters < 2:
        raise ValueError("Need at least 2 catalyst clusters to cluster-bootstrap")

    cluster_arrays = [by_catalyst[cid] for cid in catalyst_ids]
    all_diffs = np.concatenate(cluster_arrays)
    point_estimate = float(all_diffs.mean())

    boot_means = np.empty(n_bootstrap)
    for b in range(n_bootstrap):
        sampled_idx = rng.integers(0, n_clusters, size=n_clusters)
        sampled = np.concatenate([cluster_arrays[i] for i in sampled_idx])
        boot_means[b] = sampled.mean()

    alpha = 1.0 - confidence_level
    if alternative == "one_sided":
        ci_low = float(np.percentile(boot_means, 100 * alpha))
        ci_high = None
    else:
        ci_low = float(np.percentile(boot_means, 100 * (alpha / 2)))
        ci_high = float(np.percentile(boot_means, 100 * (1 - alpha / 2)))

    return TestResult(
        point_estimate=point_estimate,
        ci_low=ci_low,
        ci_high=ci_high,
        n_clusters=n_clusters,
        n_observations=n_observations,
        delta=delta,
        reject_h0=ci_low > delta,
    )


def naive_iid_test(
    records: list[tuple[str, float]],
    delta: float,
    confidence_level: float = 0.95,
    n_bootstrap: int = 2000,
    rng: np.random.Generator | None = None,
) -> TestResult:
    """The WRONG comparison, kept here deliberately so tests can show why
    catalyst clustering matters: resamples individual observations,
    ignoring which catalyst produced them. Treats 5 trades from one
    earnings release as 5 independent pieces of evidence."""
    if rng is None:
        rng = np.random.default_rng()
    diffs = np.array([d for _, d in records])
    n = len(diffs)
    point_estimate = float(diffs.mean())
    boot_means = np.empty(n_bootstrap)
    for b in range(n_bootstrap):
        sampled = diffs[rng.integers(0, n, size=n)]
        boot_means[b] = sampled.mean()
    alpha = 1.0 - confidence_level
    ci_low = float(np.percentile(boot_means, 100 * alpha))
    return TestResult(
        point_estimate=point_estimate, ci_low=ci_low, ci_high=None,
        n_clusters=len({c for c, _ in records}), n_observations=n,
        delta=delta, reject_h0=ci_low > delta,
    )
