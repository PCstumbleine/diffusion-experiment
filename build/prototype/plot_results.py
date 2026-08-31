#!/usr/bin/env python3
"""
Renders the v7 multi-catalyst backtest as a set of charts: an event-driven
equity curve across all 47 completed catalyst events (NVIDIA + AMD +
Broadcom -> SMCI/DELL/HPE basket), a per-catalyst excess-return comparison
(v8: descriptive differences exist across catalysts, but formal
heterogeneity tests do not establish they're statistically real -- see
heterogeneity_tests.py), a pooled scatter of catalyst-reaction vs basket excess
return, and a dispersion strip plot showing all 47 individual excess
returns colored by catalyst. See build_prototype.py's docstring for full
caveats -- this is a rough, illustrative sanity check, not the final
experiment.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
from pathlib import Path
import json

from build_prototype import main as run_backtest, CATALYSTS, AFFECTED_TICKERS

BASE = Path(__file__).parent
CATALYST_COLORS = {"NVIDIA": "#76b900", "AMD": "#ed1c24", "BROADCOM": "#cc092f"}


def to_dt(s):
    return datetime.strptime(s, "%Y-%m-%d")


def main():
    tickers, events = run_backtest()
    spy_prices, spy_dates = tickers["SPY"]["prices"], tickers["SPY"]["dates"]
    qqq_prices = tickers["QQQ"]["prices"]
    completed = sorted([e for e in events if not e["pending"]], key=lambda e: e["exit_date"])

    full_start, full_end = spy_dates[0], spy_dates[-1]
    all_dates = [d for d in spy_dates if full_start <= d <= full_end]

    strategy_val, spy_window_val, qqq_window_val = 1.0, 1.0, 1.0
    strategy_curve, spy_window_curve, qqq_window_curve = [], [], []
    for d in all_dates:
        for e in completed:
            if e["exit_date"] == d:
                strategy_val *= (1.0 + e["basket_return"])
                spy_window_val *= (1.0 + e["spy_return"])
                qqq_window_val *= (1.0 + e["qqq_return"])
        strategy_curve.append(strategy_val)
        spy_window_curve.append(spy_window_val)
        qqq_window_curve.append(qqq_window_val)

    spy_buyhold_base = spy_prices[all_dates[0]]["adj_close"]
    spy_buyhold_curve = [spy_prices[d]["adj_close"] / spy_buyhold_base for d in all_dates]
    qqq_buyhold_base = qqq_prices[all_dates[0]]["adj_close"]
    qqq_buyhold_curve = [qqq_prices[d]["adj_close"] / qqq_buyhold_base for d in all_dates]
    dt_dates = [to_dt(d) for d in all_dates]

    fig = plt.figure(figsize=(13, 15))
    gs = fig.add_gridspec(4, 2, height_ratios=[2.2, 1.1, 1, 1])
    ax1 = fig.add_subplot(gs[0, :])
    ax2 = fig.add_subplot(gs[1, :])
    ax3 = fig.add_subplot(gs[2, :])
    ax4 = fig.add_subplot(gs[3, 0])
    ax5 = fig.add_subplot(gs[3, 1])

    basket_label = "/".join(AFFECTED_TICKERS)
    ax1.plot(dt_dates, strategy_curve,
              label=f"Diffusion strategy (3 catalysts -> {basket_label} basket,\n5-day hold, cash between events)",
              color="#1a1a1a", linewidth=1.8, drawstyle="steps-post")
    ax1.plot(dt_dates, spy_window_curve, label="SPY, same 47 event windows only",
              color="#7f7f7f", linewidth=1.4, linestyle="--", drawstyle="steps-post")
    ax1.plot(dt_dates, qqq_window_curve, label="QQQ, same 47 event windows only",
              color="#9467bd", linewidth=1.4, linestyle=":", drawstyle="steps-post")
    ax1.plot(dt_dates, spy_buyhold_curve, label="SPY buy-and-hold (full period)",
              color="#1f77b4", linewidth=1.6)
    ax1.plot(dt_dates, qqq_buyhold_curve, label="QQQ buy-and-hold (full period)",
              color="#2ca02c", linewidth=1.6)
    for e in completed:
        c = CATALYST_COLORS[e["catalyst"]]
        ax1.axvline(to_dt(e["entry_date"]), color=c, alpha=0.10, linewidth=5)

    ax1.set_title("The Diffusion Experiment -- multi-catalyst prototype (v8)\n"
                   f"NVIDIA + AMD + Broadcom earnings as catalysts (16 quarters each), {basket_label} basket",
                   fontsize=13)
    ax1.set_ylabel("Growth of $1")
    ax1.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax1.grid(alpha=0.25)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # Panel 2: mean excess return by catalyst (the key finding -- heterogeneity)
    with open(BASE / "backtest_results.json") as f:
        results = json.load(f)
    labels = list(CATALYSTS.keys()) + ["POOLED"]
    means_spy = [results["per_catalyst"][c]["mean_excess_spy"] * 100 for c in CATALYSTS] + \
                [results["pooled"]["mean_excess_spy"] * 100]
    ns = [results["per_catalyst"][c]["n"] for c in CATALYSTS] + [results["pooled"]["n"]]
    p_vals = [results["per_catalyst"][c]["p_spy"] for c in CATALYSTS] + [results["pooled"]["p_spy"]]
    colors = [CATALYST_COLORS[c] for c in CATALYSTS] + ["#444444"]
    bars = ax2.bar(labels, means_spy, color=colors)
    ax2.axhline(0, color="black", linewidth=0.8)
    for bar, n, p in zip(bars, ns, p_vals):
        h = bar.get_height()
        ax2.annotate(f"N={n}\np={p:.3f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                     xytext=(0, 4 if h >= 0 else -28), textcoords="offset points",
                     ha="center", fontsize=8.5)
    ymin, ymax = min(means_spy + [0]), max(means_spy + [0])
    yrange = ymax - ymin
    ax2.set_ylim(ymin - 0.28 * yrange, ymax + 0.32 * yrange)
    ax2.set_ylabel("Mean basket excess\nreturn vs SPY (%)")
    ax2.set_title("Mean excess return differs by catalyst descriptively --\n"
                   "but formal heterogeneity tests do NOT reach significance (see heterogeneity_tests.py, all p > 0.45)",
                   fontsize=10.5, pad=12)
    ax2.grid(alpha=0.25, axis="y")

    # Panel 3: dispersion strip plot of all individual excess returns, colored by catalyst
    y_jitter = {"NVIDIA": 0.15, "AMD": 0.0, "BROADCOM": -0.15}
    for c in CATALYSTS:
        sub = [e for e in completed if e["catalyst"] == c]
        xs = [e["basket_excess_spy"] * 100 for e in sub]
        ys = [y_jitter[c]] * len(xs)
        ax3.scatter(xs, ys, color=CATALYST_COLORS[c], label=c, s=50, alpha=0.8, zorder=3)
    ax3.axvline(0, color="black", linewidth=0.8)
    ax3.set_yticks([0.15, 0.0, -0.15])
    ax3.set_yticklabels(list(CATALYSTS.keys()))
    ax3.set_xlabel("Basket excess return vs SPY, per event (%)")
    ax3.set_title("All 47 individual catalyst events -- dispersion within and across catalysts", fontsize=10.5)
    ax3.grid(alpha=0.25, axis="x")

    # Panel 4: pooled scatter, catalyst reaction vs basket excess
    reactions = [e["catalyst_reaction"] * 100 for e in completed]
    excess = [e["basket_excess_spy"] * 100 for e in completed]
    cols = [CATALYST_COLORS[e["catalyst"]] for e in completed]
    ax4.scatter(reactions, excess, c=cols, s=40, alpha=0.85)
    ax4.axhline(0, color="black", linewidth=0.8)
    ax4.axvline(0, color="black", linewidth=0.8)
    r = results["pooled"]["corr_reaction_spy"]
    ax4.set_xlabel("Catalyst's own overnight\nearnings-day reaction (%)", fontsize=9)
    ax4.set_ylabel("Basket excess return\nvs SPY (%)", fontsize=9)
    ax4.set_title(f"Pooled: catalyst reaction vs basket excess (r={r:+.3f}, N={len(completed)})", fontsize=9.5)
    ax4.grid(alpha=0.25)
    ax4.tick_params(labelsize=8)

    # Panel 5: legend/summary panel as a simple text box (cross-catalyst overlap check)
    ax5.axis("off")
    overlap_n = results["cross_catalyst_overlap_count"]
    summary_text = (
        f"Cross-catalyst window overlaps: {overlap_n}\n"
        f"(entry/exit windows from different\ncatalysts landing on the same days)\n\n"
        f"Pooled N = {results['pooled']['n']} catalyst events\n"
        f"Pending (excluded): {results['n_pending']}\n\n"
        f"Legend:\n"
    )
    ax5.text(0.02, 0.98, summary_text, transform=ax5.transAxes, fontsize=10, va="top", ha="left")
    for i, c in enumerate(CATALYSTS):
        ax5.scatter([0.06], [0.30 - i * 0.12], color=CATALYST_COLORS[c], s=60, transform=ax5.transAxes)
        ax5.text(0.12, 0.30 - i * 0.12, c, transform=ax5.transAxes, fontsize=10, va="center")

    fig.tight_layout()
    out_path = BASE / "diffusion_prototype_result.png"
    fig.savefig(out_path, dpi=150)
    print(f"\nSaved chart to {out_path}")


if __name__ == "__main__":
    main()
