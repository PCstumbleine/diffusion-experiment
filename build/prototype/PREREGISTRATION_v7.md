# Pre-registration: v7 multi-catalyst extension

*Written and committed BEFORE fetching any AMD/Broadcom price data, any extended NVIDIA history beyond the existing 5 quarters, or looking at any reaction data for this extension. This exists specifically so the design can't later be second-guessed as picked after seeing which choices "worked" — the same discipline the spec series (and ChatGPT's last review round) calls for.*

## Why these three catalyst companies

NVIDIA, AMD, and Broadcom are the three largest suppliers of the silicon that goes into AI-server infrastructure: NVIDIA and AMD for GPUs/accelerators, Broadcom for custom AI ASICs and the networking silicon (Ethernet switches, optical interconnects) that ties AI clusters together. All three are widely covered as "read-throughs" for AI-infrastructure capex independent of this project. Critically, **all three are named, real suppliers to the existing affected basket**: Supermicro's own investor-relations announcements (already cited in this prototype's v1–v6 write-ups) reference both NVIDIA- and AMD-based server products, and Broadcom's networking/ASIC silicon is a standard component in the same AI-server builds DELL, HPE, and SMCI assemble. This is the same economic reasoning that justified NVIDIA and the SMCI/DELL/HPE basket in v1 and v5 — a named, verifiable supply relationship — extended to two more named suppliers, not three companies picked for having interesting-looking stock charts.

**What this does and doesn't fix.** Adding two more catalyst companies increases the number of distinct catalyst-clusters beyond "16 quarters of the same one company," which partially addresses ChatGPT's point that even 16 NVIDIA quarters share one continuous AI-investment regime. It does not fully solve it — NVIDIA, AMD, and Broadcom earnings all still occur within the same broad AI-capex supercycle and the same macro regime, so some shared latent-condition risk remains across catalysts, not just within one catalyst's ticker-events. This is disclosed up front, not discovered after the fact.

## The rule (fixed before any data is pulled)

- **Catalysts:** NVIDIA, AMD, Broadcom (AVGO).
- **Window per catalyst: the 16 most recent quarterly earnings releases as of August 2026**, however far back that reaches for each company's own fiscal calendar. No quarter is omitted because of its subsequent stock performance, the sign of any surprise, competing news, or data-availability convenience. If fewer than 16 clean quarters turn out to be available for a company once dates are verified against primary sources, the shortfall is disclosed, not backfilled with a differently-chosen quarter.
- **Affected basket: unchanged — SMCI, DELL, HPE, equal-weighted.** Not re-optimized or re-selected now that there's more data to try against.
- **Benchmarks: unchanged — SPY and QQQ**, matched open→close per the v2/v6 methodology.
- **Holding period: unchanged — 5 trading days**, entry at the first session's open after the release.
- **Entry-timing rule for historical releases:** do not assume every historical release was after-market-close the way the 5 most recent NVIDIA releases were (a refinement ChatGPT flagged). For each catalyst event, entry is the first regular session's open strictly after the disclosure's effective public timestamp — same-day open if disclosed before/during market hours, next session's open if disclosed after close. This will be determined per-event from the same primary sources used to verify the date itself.
- **Return calculation:** adjusted-open → adjusted-close for every instrument (the v6 fix), applied uniformly across the extended window.
- **Competing-news handling:** flagged in the write-up per event, exactly as v1–v6 have done for SMCI-specific news. Flags are descriptive; no event is excluded from the primary analysis on the basis of a competing-news flag alone (per spec-v2.1 Section 8's rule that censoring, if used, must be a pre-registered mechanical rule applied uniformly — not a discretionary per-event judgment call, which this prototype is not yet equipped to do rigorously).
- **Unit of inference:** each catalyst *event* (one company, one quarter) is one observation. With up to 3 companies × 16 quarters, that's up to 48 catalyst events — still not fully independent (shared regime risk, as above), but a genuine increase over the current N=4, reported honestly rather than treated as 48 independent draws.

## Locked catalyst dates (verified against primary sources before any price data for the new events was pulled)

**NVIDIA** (16 quarters, own fiscal calendar; existing 5 confirmed in earlier versions, 11 more confirmed now via nvidianews.nvidia.com press releases):
Nov 16, 2022 (Q3 FY23) · Feb 22, 2023 (Q4 FY23) · May 24, 2023 (Q1 FY24) · Aug 23, 2023 (Q2 FY24) · Nov 21, 2023 (Q3 FY24) · Feb 21, 2024 (Q4 FY24) · May 22, 2024 (Q1 FY25) · Aug 28, 2024 (Q2 FY25) · Nov 20, 2024 (Q3 FY25) · Feb 26, 2025 (Q4 FY25) · May 28, 2025 (Q1 FY26) · Aug 27, 2025 (Q2 FY26) · Nov 19, 2025 (Q3 FY26) · Feb 25, 2026 (Q4 FY26) · May 20, 2026 (Q1 FY27) · Aug 26, 2026 (Q2 FY27).

**AMD** (16 quarters, calendar-year fiscal quarters; verified via amd.com/ir.amd.com newsroom press-release URLs, which embed the release date):
Nov 1, 2022 (Q3'22) · Jan 31, 2023 (Q4'22) · May 2, 2023 (Q1'23) · Aug 1, 2023 (Q2'23) · Oct 31, 2023 (Q3'23) · Jan 30, 2024 (Q4'23) · Apr 30, 2024 (Q1'24) · Jul 30, 2024 (Q2'24) · Oct 29, 2024 (Q3'24) · Feb 4, 2025 (Q4'24) · May 6, 2025 (Q1'25) · Aug 5, 2025 (Q2'25) · Nov 4, 2025 (Q3'25) · Feb 3, 2026 (Q4'25) · May 5, 2026 (Q1'26) · Aug 4, 2026 (Q2'26).

**Broadcom (AVGO)** (16 quarters, fiscal year ending late October/early November; verified directly against SEC EDGAR's own 8-K filing list for CIK 0001730168, using each 8-K containing item 2.02 "Results of Operations and Financial Condition" as the earnings-announcement date — a primary-source method, not a third-party aggregator):
Sep 1, 2022 (Q3 FY22) · Dec 8, 2022 (Q4 FY22) · Mar 2, 2023 (Q1 FY23) · Jun 1, 2023 (Q2 FY23) · Aug 31, 2023 (Q3 FY23) · Dec 7, 2023 (Q4 FY23) · Mar 7, 2024 (Q1 FY24) · Jun 12, 2024 (Q2 FY24) · Sep 5, 2024 (Q3 FY24) · Dec 12, 2024 (Q4 FY24) · Mar 6, 2025 (Q1 FY25) · Jun 5, 2025 (Q2 FY25) · Sep 4, 2025 (Q3 FY25) · Dec 11, 2025 (Q4 FY25) · Mar 4, 2026 (Q1 FY26) · Jun 3, 2026 (Q2 FY26).

That's 48 catalyst events total, spanning roughly September 2022 through August 2026. These dates are now locked before any of the corresponding price reactions have been looked at. Entry-timing note: NVIDIA and AMD both report after market close as a matter of established practice across this whole window (confirmed for the 5 most recent NVIDIA events in earlier versions; assumed consistent for the rest since NVIDIA's own release language ("today reported... second quarter ended...") appears unchanged across all fetched releases). Broadcom's item-2.02 8-Ks in this list are also consistently same-day-as-announcement per its own historical pattern. Per the pre-registration rule above, entry is therefore the next trading session's open for every event in this set — this is disclosed as an assumption, not independently re-verified release-time-of-day for all 48 events, given the practical scope of this round.

## What happens after this document is written

Price history is now pulled for the full set of 8 tickers (SMCI, DELL, HPE, SPY, QQQ, NVDA, AMD, AVGO) over the combined date range these 48 events require (roughly August 2022 – August 2026), before `build_prototype.py` is generalized and rerun.
