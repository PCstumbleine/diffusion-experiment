#!/usr/bin/env python3
"""
Fast historical prototype for The Diffusion Experiment -- v8, multi-catalyst
(revised after a ChatGPT review round on v7).

WHAT THIS IS
------------
A rough, illustrative sanity check -- NOT the rigorous, statistically
validated experiment the full spec (schema.sql / edgar_ingest_worker.py /
statistical_test.py) is built to run. It exists to answer one question
concretely: "if we trade a simple, real, historically-verified version of
the diffusion idea across many more events than before, does anything at
all show up?"

THE THESIS BEING SANITY-CHECKED
--------------------------------
Quarterly earnings disclosures from major AI-chip suppliers (a "catalyst"
filing) contain information relevant to companies that build AI servers
around that supplier's silicon but are not the ones reporting. The idea is
that this information takes a little time to fully diffuse into those
companies' stock prices. This script tests: buy each affected company at
the open of the first trading day after each catalyst's earnings release,
hold for a fixed 5-trading-day window, sell. Compare the result against
SPY and QQQ over the same period.

VERSION HISTORY (full detail in PROTOTYPE_README.md)
------------------------------------------------------
v1-v4: single catalyst (NVIDIA), single affected ticker (SMCI), 5 events.
v5: replaced SMCI alone with a predeclared 3-name basket (SMCI/DELL/HPE)
    and added QQQ as a second benchmark. Still 5 NVIDIA-only events.
v6: fixed a dividend-adjustment asymmetry (ChatGPT review round) -- basket
    members now use the same adjusted-open->adjusted-close method as the
    benchmarks, not raw open/close.
v7 (this version): extends from ONE catalyst (NVIDIA, 5 events) to THREE
    predeclared catalysts -- NVIDIA, AMD, and Broadcom (AVGO), the three
    largest suppliers of AI-server silicon, each with 16 quarters of
    earnings (48 catalyst events total, up to ~15-16 usable per catalyst
    once the most recent 1-2 per catalyst are excluded as "pending" for
    lack of forward price data). Full pre-registration protocol --
    written and committed BEFORE any of the new catalysts' price
    reactions were examined -- is in PREREGISTRATION_v7.md. The
    NVIDIA-guidance-vs-its-own-prior-guidance check from v3/v4 is NOT
    extended to AMD/Broadcom in this version (that would require
    separately researching guidance figures for 43 additional quarters,
    out of scope for this round) -- the "catalyst's own overnight
    earnings-day reaction" check from v4 is kept and generalized to all
    three catalysts instead, since it needs only price data and was
    ChatGPT's own recommended proxy.
v8: A ChatGPT review round independently rederived the exact pooled and
    per-catalyst figures from this same code/data (all matched), then ran
    formal cross-catalyst heterogeneity tests the v7 write-up lacked --
    finding that despite the descriptive difference in per-catalyst
    p-values, a chi-square test on the win/loss table (p=0.451), an exact
    permutation-conditional test (p~0.48), one-way ANOVA / Welch ANOVA /
    Kruskal-Wallis on the continuous excess returns (p=0.62/0.61/0.60), and
    a permutation F-test (p~0.63) all fail to reject "no heterogeneity
    across catalysts" -- i.e. "AMD's p-value is smaller than NVIDIA's" does
    NOT itself establish that the catalysts have statistically different
    effects. This was independently reverified (see heterogeneity_tests.py,
    all figures reproduced to 3-4 decimals) and the v7 README's framing
    ("the three catalysts do NOT behave alike") is corrected accordingly --
    it overstated a real but non-significant descriptive pattern as a
    confirmed difference. Also fixed: catalyst_reaction now uses adjusted
    prices (was raw open/close, inconsistent with compute_return()
    elsewhere -- flagged by the same review, no material numeric effect on
    this dataset). Also corrected: the README's cross-catalyst-overlap pair
    count, which wrongly said "47x46 pairs checked" -- the actual count of
    unique cross-catalyst pairs is 736 (15x16 + 15x16 + 16x16), not an
    ordered 47x46; the overlap result itself (0 overlaps) is unaffected.

DATA PROVENANCE (all real, independently verified before use)
---------------------------------------------------------------
- Catalyst earnings dates: NVIDIA confirmed via nvidianews.nvidia.com
  press releases (each release's own "today reported..." text checked
  directly); AMD confirmed via amd.com/ir.amd.com newsroom press-release
  URLs, which embed the release date; Broadcom (AVGO) confirmed directly
  against SEC EDGAR's own 8-K filing list for CIK 0001730168, using each
  8-K containing item 2.02 ("Results of Operations and Financial
  Condition") as the earnings-announcement date. Full list in
  PREREGISTRATION_v7.md, locked before any new price reactions were
  examined.
- SMCI, DELL, HPE, SPY, QQQ, NVDA, AMD, and AVGO daily OHLC price history
  (Aug 1, 2022 - Aug 28, 2026) pulled from Yahoo Finance's historical-
  prices pages via a live browser session, 2026-08-31. All 8 tickers
  cover exactly the same 1,024 trading days -- verified before any
  backtest logic ran.

CAVEATS THAT MATTER (read before trusting the numbers)
-------------------------------------------------------
- Even with 48 nominal catalyst events, this is NOT 48 independent draws.
  Three separate, real dependency structures are checked and reported
  explicitly below: (1) the three tickers within one catalyst event share
  that event (handled since v5 -- basket-level aggregation); (2) all
  catalyst events across all three companies occur within the same
  ~4-year AI-capex supercycle and share macro/sector regime risk (flagged
  by ChatGPT's last review round, not fixable with more of this same kind
  of data); (3) NEW in v7 -- entry/exit windows from DIFFERENT catalysts
  can literally overlap in calendar time (e.g., NVIDIA and AMD sometimes
  report within days of each other), meaning the same basket position
  gets attributed to two different catalysts' "effect" simultaneously.
  This script detects and reports how often that happens rather than
  assuming it away.
- The most recent 1-2 quarters per catalyst are excluded as "pending"
  where fewer than 5 trading days of forward price history exist yet.
- The holding period (5 trading days), the three catalysts, and the
  basket were all chosen and written down (PREREGISTRATION_v7.md) BEFORE
  any AMD/Broadcom-driven price reactions were examined.
- This script does not use any SEC filing text or LLM-based extraction --
  it hand-codes the known earnings dates directly. The real pipeline
  (edgar_ingest_worker.py etc.) is what would eventually replace that
  hand-coding with automated, timestamped ingestion at scale.
- Transaction costs, slippage, taxes, and shorting constraints are all
  ignored.
"""
import re
from datetime import datetime
from pathlib import Path
from math import comb

BASE = Path(__file__).parent
RAW = BASE / "raw_data"

AFFECTED_TICKERS = ["SMCI", "DELL", "HPE"]
BENCHMARK_TICKERS = ["SPY", "QQQ"]

# Pre-registered per PREREGISTRATION_v7.md. Dates verified against primary
# sources (see docstring) BEFORE any of the new catalysts' price reactions
# were examined. No date omitted for its subsequent stock performance,
# surprise sign, competing news, or convenience.
CATALYSTS = {
    "NVIDIA": {
        "ticker": "NVDA",
        "dates": [
            ("2022-11-16", "Q3 FY23"), ("2023-02-22", "Q4 FY23"),
            ("2023-05-24", "Q1 FY24"), ("2023-08-23", "Q2 FY24"),
            ("2023-11-21", "Q3 FY24"), ("2024-02-21", "Q4 FY24"),
            ("2024-05-22", "Q1 FY25"), ("2024-08-28", "Q2 FY25"),
            ("2024-11-20", "Q3 FY25"), ("2025-02-26", "Q4 FY25"),
            ("2025-05-28", "Q1 FY26"), ("2025-08-27", "Q2 FY26"),
            ("2025-11-19", "Q3 FY26"), ("2026-02-25", "Q4 FY26"),
            ("2026-05-20", "Q1 FY27"), ("2026-08-26", "Q2 FY27"),
        ],
    },
    "AMD": {
        "ticker": "AMD",
        "dates": [
            ("2022-11-01", "Q3'22"), ("2023-01-31", "Q4'22"),
            ("2023-05-02", "Q1'23"), ("2023-08-01", "Q2'23"),
            ("2023-10-31", "Q3'23"), ("2024-01-30", "Q4'23"),
            ("2024-04-30", "Q1'24"), ("2024-07-30", "Q2'24"),
            ("2024-10-29", "Q3'24"), ("2025-02-04", "Q4'24"),
            ("2025-05-06", "Q1'25"), ("2025-08-05", "Q2'25"),
            ("2025-11-04", "Q3'25"), ("2026-02-03", "Q4'25"),
            ("2026-05-05", "Q1'26"), ("2026-08-04", "Q2'26"),
        ],
    },
    "BROADCOM": {
        "ticker": "AVGO",
        "dates": [
            ("2022-09-01", "Q3 FY22"), ("2022-12-08", "Q4 FY22"),
            ("2023-03-02", "Q1 FY23"), ("2023-06-01", "Q2 FY23"),
            ("2023-08-31", "Q3 FY23"), ("2023-12-07", "Q4 FY23"),
            ("2024-03-07", "Q1 FY24"), ("2024-06-12", "Q2 FY24"),
            ("2024-09-05", "Q3 FY24"), ("2024-12-12", "Q4 FY24"),
            ("2025-03-06", "Q1 FY25"), ("2025-06-05", "Q2 FY25"),
            ("2025-09-04", "Q3 FY25"), ("2025-12-11", "Q4 FY25"),
            ("2026-03-04", "Q1 FY26"), ("2026-06-03", "Q2 FY26"),
        ],
    },
}

HOLD_TRADING_DAYS = 5


def parse_yahoo_raw(path):
    """Parse a Yahoo-Finance-history text dump (tab separated, newest first,
    columns: Date Open High Low Close AdjClose Volume)."""
    rows = {}
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            date_str = parts[0].strip()
            try:
                d = datetime.strptime(date_str, "%b %d, %Y").date()
            except ValueError:
                continue
            try:
                open_ = float(parts[1])
                close_ = float(parts[4])
                adj_close = float(parts[5])
            except ValueError:
                continue
            rows[d.isoformat()] = {"open": open_, "close": close_, "adj_close": adj_close}
    ordered_dates = sorted(rows.keys())
    return rows, ordered_dates


def next_trading_day(ordered_dates, from_date_str, offset=1):
    if offset < 1:
        raise ValueError("offset must be >= 1")
    if not ordered_dates or from_date_str < ordered_dates[0]:
        return None
    idx = None
    for i, d in enumerate(ordered_dates):
        if d > from_date_str:
            idx = i
            break
    if idx is None:
        return None
    target = idx + (offset - 1)
    if target >= len(ordered_dates):
        return None
    return ordered_dates[target]


def adjusted_open(row):
    if row["close"] == 0:
        return row["open"]
    return row["open"] * (row["adj_close"] / row["close"])


def compute_return(prices, entry_date, exit_date):
    """Adjusted-open -> adjusted-close return, used uniformly for every
    instrument since v6 (dividend-fair)."""
    if entry_date not in prices or exit_date not in prices:
        return None
    entry_open = adjusted_open(prices[entry_date])
    exit_close = prices[exit_date]["adj_close"]
    return exit_close / entry_open - 1.0


def pearson(xs, ys):
    m = len(xs)
    if m == 0:
        return None
    mx, my = sum(xs) / m, sum(ys) / m
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / m
    vx = sum((x - mx) ** 2 for x in xs) / m
    vy = sum((y - my) ** 2 for y in ys) / m
    return cov / (vx ** 0.5 * vy ** 0.5) if vx > 0 and vy > 0 else None


def sign_test_p(wins, n):
    if n == 0:
        return None
    k = min(wins, n - wins)
    return min(2 * sum(comb(n, i) for i in range(0, k + 1)) / (2 ** n), 1.0)


def main():
    tickers = {}
    all_syms = set(AFFECTED_TICKERS) | set(BENCHMARK_TICKERS) | {c["ticker"] for c in CATALYSTS.values()}
    for sym in sorted(all_syms):
        prices, dates = parse_yahoo_raw(RAW / f"{sym}_yahoo_history_raw.txt")
        tickers[sym] = {"prices": prices, "dates": dates}
        print(f"Loaded {len(dates)} {sym} trading days ({dates[0]} to {dates[-1]})")
    print()

    ref_dates = tickers["SPY"]["dates"]

    events = []
    for catalyst_name, cinfo in CATALYSTS.items():
        cat_ticker = cinfo["ticker"]
        cat_prices = tickers[cat_ticker]["prices"]
        for event_date, label in cinfo["dates"]:
            entry_date = next_trading_day(ref_dates, event_date, offset=1)
            exit_date = next_trading_day(ref_dates, event_date, offset=HOLD_TRADING_DAYS)
            if entry_date is None:
                continue
            pending = exit_date is None
            rec = {
                "catalyst": catalyst_name, "catalyst_ticker": cat_ticker,
                "label": label, "event_date": event_date,
                "entry_date": entry_date, "exit_date": exit_date, "pending": pending,
            }
            if pending:
                events.append(rec)
                continue

            per_ticker = {}
            ok = True
            for sym in AFFECTED_TICKERS:
                r = compute_return(tickers[sym]["prices"], entry_date, exit_date)
                if r is None:
                    ok = False
                    break
                per_ticker[sym] = r
            if not ok:
                continue

            bench = {}
            for sym in BENCHMARK_TICKERS:
                r = compute_return(tickers[sym]["prices"], entry_date, exit_date)
                if r is None:
                    ok = False
                    break
                bench[sym] = r
            if not ok:
                continue

            if event_date not in cat_prices or entry_date not in cat_prices:
                continue
            # v8 fix: use adjusted prices here too, for consistency with compute_return()
            # elsewhere (flagged by a ChatGPT review round; spot-checked to have no material
            # effect on this dataset, but fixed structurally rather than left inconsistent).
            catalyst_reaction = (adjusted_open(cat_prices[entry_date])
                                 / cat_prices[event_date]["adj_close"]) - 1.0

            basket_return = sum(per_ticker.values()) / len(per_ticker)
            rec.update({
                "per_ticker": per_ticker,
                "spy_return": bench["SPY"], "qqq_return": bench["QQQ"],
                "basket_return": basket_return,
                "basket_excess_spy": basket_return - bench["SPY"],
                "basket_excess_qqq": basket_return - bench["QQQ"],
                "catalyst_reaction": catalyst_reaction,
            })
            events.append(rec)

    completed = [e for e in events if not e["pending"]]
    pending = [e for e in events if e["pending"]]

    print(f"Catalysts: {', '.join(CATALYSTS.keys())} (16 quarters each, pre-registered -- PREREGISTRATION_v7.md)")
    print(f"Basket: {', '.join(AFFECTED_TICKERS)} (unchanged since v5)")
    print(f"{len(completed)} completed catalyst events, {len(pending)} pending (insufficient forward data)\n")

    for c in CATALYSTS:
        sub = [e for e in completed if e["catalyst"] == c]
        print(f"  {c}: {len(sub)} completed events")
    print()

    # ---- Overlap detection: do windows from DIFFERENT catalysts overlap in calendar time? ----
    overlap_count = 0
    overlap_pairs = []
    for i, e1 in enumerate(completed):
        for e2 in completed[i + 1:]:
            if e1["catalyst"] == e2["catalyst"]:
                continue
            if e1["entry_date"] <= e2["exit_date"] and e2["entry_date"] <= e1["exit_date"]:
                overlap_count += 1
                overlap_pairs.append((e1["catalyst"], e1["label"], e2["catalyst"], e2["label"]))

    print(f"Cross-catalyst window overlaps: {overlap_count} pairs of events (from different catalysts) "
          f"whose 5-day windows overlap in calendar time.")
    if overlap_pairs:
        print("  These pairs are NOT independent evidence of two separate diffusion effects -- they are the "
              "same basket price action attributed to two catalysts at once:")
        for c1, l1, c2, l2 in overlap_pairs:
            print(f"    {c1} {l1}  <->  {c2} {l2}")
    print()

    # ---- Pooled statistics (all catalysts combined) ----
    def summarize(label, subset):
        n = len(subset)
        if n == 0:
            print(f"{label}: no completed events")
            return
        basket_excess_spy = [e["basket_excess_spy"] for e in subset]
        basket_excess_qqq = [e["basket_excess_qqq"] for e in subset]
        wins_spy = sum(1 for e in basket_excess_spy if e > 0)
        wins_qqq = sum(1 for e in basket_excess_qqq if e > 0)
        mean_spy = sum(basket_excess_spy) / n
        mean_qqq = sum(basket_excess_qqq) / n
        compounded = 1.0
        for e in subset:
            compounded *= (1.0 + e["basket_return"])
        compounded_spy = 1.0
        compounded_qqq = 1.0
        for e in subset:
            compounded_spy *= (1.0 + e["spy_return"])
            compounded_qqq *= (1.0 + e["qqq_return"])
        p_spy = sign_test_p(wins_spy, n)
        p_qqq = sign_test_p(wins_qqq, n)
        reactions = [e["catalyst_reaction"] for e in subset]
        corr_spy = pearson(reactions, basket_excess_spy)
        print(f"{label} (N={n}):")
        print(f"  Compounded basket: {(compounded-1)*100:+.2f}%   SPY(matched): {(compounded_spy-1)*100:+.2f}%   "
              f"QQQ(matched): {(compounded_qqq-1)*100:+.2f}%")
        print(f"  Mean basket excess vs SPY: {mean_spy*100:+.2f}%  ({wins_spy}/{n} wins, sign-test p={p_spy:.3f})")
        print(f"  Mean basket excess vs QQQ: {mean_qqq*100:+.2f}%  ({wins_qqq}/{n} wins, sign-test p={p_qqq:.3f})")
        print(f"  Corr(catalyst's own overnight reaction, basket excess vs SPY): "
              f"r={corr_spy:+.4f}" if corr_spy is not None else "  Corr: n/a")
        return {
            "n": n, "compounded_basket": compounded - 1, "compounded_spy": compounded_spy - 1,
            "compounded_qqq": compounded_qqq - 1, "mean_excess_spy": mean_spy, "mean_excess_qqq": mean_qqq,
            "wins_spy": wins_spy, "wins_qqq": wins_qqq, "p_spy": p_spy, "p_qqq": p_qqq, "corr_reaction_spy": corr_spy,
        }

    print("=" * 70)
    pooled_stats = summarize("POOLED (all 3 catalysts combined)", completed)
    print()
    per_catalyst_stats = {}
    for c in CATALYSTS:
        sub = [e for e in completed if e["catalyst"] == c]
        per_catalyst_stats[c] = summarize(f"{c} only", sub)
        print()

    for e in pending:
        print(f"PENDING (not counted): {e['catalyst']} {e['label']} entered {e['entry_date']}, "
              f"not yet {HOLD_TRADING_DAYS} trading days old")

    import json
    out = {
        "hold_trading_days": HOLD_TRADING_DAYS,
        "affected_tickers": AFFECTED_TICKERS,
        "benchmark_tickers": BENCHMARK_TICKERS,
        "catalysts": list(CATALYSTS.keys()),
        "n_completed": len(completed),
        "n_pending": len(pending),
        "cross_catalyst_overlap_count": overlap_count,
        "overlap_pairs": overlap_pairs,
        "pooled": pooled_stats,
        "per_catalyst": per_catalyst_stats,
        "events": completed,
        "pending_events": pending,
    }
    with open(BASE / "backtest_results.json", "w") as f:
        json.dump(out, f, indent=2, default=str)

    return tickers, events


if __name__ == "__main__":
    main()
