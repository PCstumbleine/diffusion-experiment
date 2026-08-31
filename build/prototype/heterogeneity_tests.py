#!/usr/bin/env python3
"""
Formal cross-catalyst heterogeneity tests -- added in v8 after a ChatGPT
review round on v7 pointed out that the v7 README's claim ("the three
catalysts do NOT behave alike") was a description of differing per-catalyst
p-values, not a demonstrated statistically significant difference between
catalysts. Those are not the same claim: one subgroup having a smaller
p-value than another does not itself establish that the subgroups' true
effects differ.

This script runs the tests that actually address that question directly:
  1. A chi-square test of independence on the 3x2 (catalyst x win/loss) table.
  2. A Monte Carlo approximation of the exact conditional (Fisher-Freeman-
     Halton-style) test on that same table.
  3. One-way ANOVA, Welch's ANOVA (unequal variances), and Kruskal-Wallis
     (rank-based, distribution-free) on the continuous per-event excess
     returns across the three catalyst groups.
  4. A permutation F-test (distribution-free significance for the ANOVA
     F-statistic) as a further check not dependent on normality.
  5. Leave-one-catalyst-out sign tests -- a descriptive robustness view of
     how much the pooled win-rate result depends on any single catalyst.
  6. Each catalyst's share of the pooled summed excess return.

All of these were independently reproduced against ChatGPT's own reported
figures before being folded into this project's deliverables -- see the v8
changelog entry in build_prototype.py's docstring and PROTOTYPE_README.md
for the reconciliation. Reads backtest_results.json (produced by
build_prototype.py); writes heterogeneity_results.json alongside it.
"""
import json
from pathlib import Path
from math import comb
import numpy as np
from scipy import stats

BASE = Path(__file__).parent
CATALYSTS = ["NVIDIA", "AMD", "BROADCOM"]


def sign_test_p(wins, n):
    k = min(wins, n - wins)
    return min(2 * sum(comb(n, i) for i in range(0, k + 1)) / (2 ** n), 1.0)


def welch_anova(groups):
    """Manual Welch's ANOVA (unequal-variance F-test); scipy has no built-in."""
    k = len(groups)
    ni = np.array([len(g) for g in groups])
    mi = np.array([g.mean() for g in groups])
    vi = np.array([g.var(ddof=1) for g in groups])
    wi = ni / vi
    grand_mean = np.sum(wi * mi) / np.sum(wi)
    numerator = np.sum(wi * (mi - grand_mean) ** 2) / (k - 1)
    denom_term = np.sum((1 - wi / np.sum(wi)) ** 2 / (ni - 1)) / (k ** 2 - 1)
    F = numerator / (1 + 2 * (k - 2) * denom_term)
    df1 = k - 1
    df2 = 1 / (3 * denom_term)
    p = 1 - stats.f.cdf(F, df1, df2)
    return F, df1, df2, p


def permutation_test(table_or_vals, kind, sizes, n_perm=100000, seed=42):
    rng = np.random.default_rng(seed)
    if kind == "chi2_table":
        wins, ns = table_or_vals
        labels = np.concatenate([[i] * n for i, n in enumerate(ns)])
        outcomes = np.concatenate([[1] * w + [0] * (n - w) for w, n in zip(wins, ns)])
        observed = stats.chi2_contingency(
            np.array([[w, n - w] for w, n in zip(wins, ns)]), correction=False
        )[0]
        count = 0
        for _ in range(n_perm):
            perm = rng.permutation(outcomes)
            t = np.array([[perm[labels == g].sum(), (labels == g).sum() - perm[labels == g].sum()]
                          for g in range(len(ns))])
            c2 = stats.chi2_contingency(t, correction=False)[0]
            if c2 >= observed - 1e-9:
                count += 1
        return observed, count / n_perm
    elif kind == "anova_f":
        all_vals = table_or_vals
        N, k = len(all_vals), len(sizes)
        idx = np.cumsum([0] + sizes)

        def f_stat_batch(vm):
            gm = vm.mean(axis=1, keepdims=True)
            ssb = np.zeros(vm.shape[0])
            ssw = np.zeros(vm.shape[0])
            for i in range(k):
                g = vm[:, idx[i]:idx[i + 1]]
                gmi = g.mean(axis=1)
                ssb += sizes[i] * (gmi - gm.ravel()) ** 2
                ssw += ((g - gmi[:, None]) ** 2).sum(axis=1)
            return (ssb / (k - 1)) / (ssw / (N - k))

        observed = f_stat_batch(all_vals[None, :])[0]
        batch, count = 5000, 0
        for start in range(0, n_perm, batch):
            b = min(batch, n_perm - start)
            pm = np.array([rng.permutation(all_vals) for _ in range(b)])
            count += (f_stat_batch(pm) >= observed - 1e-12).sum()
        return observed, count / n_perm


def main():
    with open(BASE / "backtest_results.json") as f:
        results = json.load(f)
    events = results["events"]
    by_cat = {c: [e for e in events if e["catalyst"] == c] for c in CATALYSTS}
    excess = {c: np.array([e["basket_excess_spy"] for e in by_cat[c]]) for c in CATALYSTS}
    wins = {c: int((excess[c] > 0).sum()) for c in CATALYSTS}
    ns = {c: len(excess[c]) for c in CATALYSTS}

    print("Per-catalyst win/N and mean excess (sanity check against backtest_results.json):")
    for c in CATALYSTS:
        print(f"  {c}: {wins[c]}/{ns[c]}  mean={excess[c].mean() * 100:+.4f}%  "
              f"sign-p={sign_test_p(wins[c], ns[c]):.4f}")

    table = np.array([[wins[c], ns[c] - wins[c]] for c in CATALYSTS])
    chi2, p_chi2, dof, _ = stats.chi2_contingency(table, correction=False)
    print(f"\n1. Chi-square test of independence (catalyst x win/loss):")
    print(f"   chi2={chi2:.4f}, dof={dof}, p={p_chi2:.4f}")

    obs_chi2, p_exact_mc = permutation_test(
        ([wins[c] for c in CATALYSTS], [ns[c] for c in CATALYSTS]), "chi2_table", None
    )
    print(f"\n2. Monte Carlo exact-conditional test (100k permutations of win/loss labels):")
    print(f"   observed chi2={obs_chi2:.4f}, p={p_exact_mc:.4f}")

    groups = [excess[c] for c in CATALYSTS]
    f_stat, p_anova = stats.f_oneway(*groups)
    print(f"\n3a. One-way ANOVA on continuous excess returns: F={f_stat:.4f}, p={p_anova:.4f}")
    F_w, df1_w, df2_w, p_w = welch_anova(groups)
    print(f"3b. Welch ANOVA (unequal variances): F={F_w:.4f}, df=({df1_w:.1f},{df2_w:.2f}), p={p_w:.4f}")
    kw_stat, p_kw = stats.kruskal(*groups)
    print(f"3c. Kruskal-Wallis (rank-based): H={kw_stat:.4f}, p={p_kw:.4f}")

    all_vals = np.concatenate(groups)
    sizes = [ns[c] for c in CATALYSTS]
    obs_f, p_perm = permutation_test(all_vals, "anova_f", sizes)
    print(f"\n4. Permutation F-test (100k permutations, distribution-free): "
          f"observed F={obs_f:.4f}, p={p_perm:.4f}")

    print("\n5. Leave-one-catalyst-out sign tests (pooled win rate excluding one catalyst):")
    loo = {}
    for excl in CATALYSTS:
        remaining = [c for c in CATALYSTS if c != excl]
        w = sum(wins[c] for c in remaining)
        n = sum(ns[c] for c in remaining)
        p = sign_test_p(w, n)
        loo[excl] = {"wins": w, "n": n, "p": p}
        print(f"   Exclude {excl}: {w}/{n} wins, sign-test p={p:.4f}")

    print("\n6. Each catalyst's share of the pooled summed excess return:")
    pooled_sum = sum(excess[c].sum() for c in CATALYSTS)
    contrib = {}
    for c in CATALYSTS:
        s = float(excess[c].sum())
        share = s / pooled_sum * 100
        contrib[c] = {"sum_excess": s, "share_pct": share}
        print(f"   {c}: sum={s * 100:+.4f}%  share={share:.1f}%")

    n1, n2, n3 = ns["NVIDIA"], ns["AMD"], ns["BROADCOM"]
    cross_pairs = n1 * n2 + n1 * n3 + n2 * n3
    print(f"\nCross-catalyst unique pair count check: {cross_pairs} "
          f"(={n1}x{n2} + {n1}x{n3} + {n2}x{n3}) -- this is the correct count "
          f"of unique cross-catalyst pairs the overlap check in build_prototype.py "
          f"examines, not an ordered 47x46.")

    out = {
        "chi2_test": {"chi2": float(chi2), "dof": int(dof), "p": float(p_chi2)},
        "exact_conditional_mc": {"chi2": float(obs_chi2), "p": float(p_exact_mc), "n_perm": 100000},
        "anova": {"F": float(f_stat), "p": float(p_anova)},
        "welch_anova": {"F": float(F_w), "df1": float(df1_w), "df2": float(df2_w), "p": float(p_w)},
        "kruskal_wallis": {"H": float(kw_stat), "p": float(p_kw)},
        "permutation_f_test": {"F": float(obs_f), "p": float(p_perm), "n_perm": 100000},
        "leave_one_catalyst_out": loo,
        "excess_return_contribution": contrib,
        "cross_catalyst_unique_pairs": cross_pairs,
        "conclusion": (
            "None of the formal heterogeneity tests (chi-square, exact-conditional, "
            "ANOVA, Welch ANOVA, Kruskal-Wallis, permutation F-test) reject the null "
            "of no difference across catalysts (all p > 0.45). The v7 README's claim "
            "that 'the three catalysts do NOT behave alike' is a real descriptive "
            "pattern -- AMD's own sign-test p-value is smaller, and it contributes "
            "~62% of the pooled summed excess return vs NVIDIA's ~37% and Broadcom's "
            "~0.3% -- but it is not a statistically established difference in effects "
            "at this sample size. Corrected framing: the pooled result should not be "
            "read as evidence of a common cross-catalyst mechanism, AND it should not "
            "be read as evidence that the catalysts are confirmed to differ either -- "
            "the honest state is that this sample cannot distinguish a common weak "
            "effect from a catalyst-specific (plausibly AMD-specific) effect from pure "
            "noise."
        ),
    }
    with open(BASE / "heterogeneity_results.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved heterogeneity_results.json")


if __name__ == "__main__":
    main()
