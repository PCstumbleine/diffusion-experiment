# The Diffusion Experiment

An AI-assisted trading research project testing whether corporate-event information (earnings, guidance revisions, supply/customer disclosures) diffuses predictably into economically-linked companies' stock prices — and whether an LLM-driven strategy (Arm A) adds anything over a mechanical baseline (Arm G) given the same candidate pool. The eventual goal is a system pluggable into Robinhood's Agentic Trading/MCP feature, though that integration is explicitly blocked until a prospective promotion gate clears (see `specs/diffusion-experiment-spec-v2.2.1.md`, §14 and the promotion-gate table in `specs/diffusion-experiment-spec-v2.1.md`).

This project has been built through a two-AI review loop throughout: Claude builds and fixes, ChatGPT reviews independently, and every load-bearing claim from either side gets independently re-verified before being trusted or acted on — not just read and accepted. That discipline shows up throughout the version history below (e.g. the v6 dividend-adjustment fix, the v8 statistical-heterogeneity correction, and the candidate-universe corrections), and is worth preserving in how this repo gets used going forward.

## Repository layout

- **`specs/`** — the design-document lineage: original v2 spec → v2.1 (closed 24 gaps found by adversarial review) → v2.2 → v2.2.1 (current, terminal design doc — "the next artifacts in this series should be code, not more specification text"). Read v2.2.1 first; earlier versions are kept for provenance, not as current guidance.
- **`audits/`** — `underreaction-audit.md` (the original 30-question adversarial audit that drove most of the spec's revisions), `second-pass-response.md`, and a synthesis review (`diffusion-experiment-audit-review-2026-08-31.md`) mapping the audit to the project's state as of that date, with a 30-day testing plan and a "proceed with major changes, not yet toward Robinhood" recommendation.
- **`build/`** — the first real code from the spec: `schema.sql` (Postgres DDL, tested against a live database), `extraction_prompt_v1.md` (the LLM extraction prompt), `surprise_transform.py`, `edgar_ingest_worker.py` (SEC EDGAR polling — ingestion only; see caveat below), `candidate_coverage.py`, `statistical_test.py`, and `tests/` (52 passing tests). See `build/README.md` for the full code-review history (three rounds, specific bugs found and fixed).
- **`build/prototype/`** — a fast, real-data historical sanity check (not the rigorous experiment the full spec runs), now at v8: `PREREGISTRATION_v7.md` (locked before any AMD/Broadcom data was pulled), `build_prototype.py`, `heterogeneity_tests.py` (formal statistical tests added after a review round caught an overstated finding), `plot_results.py`, results JSON, the rendered chart, and the raw Yahoo Finance price data used. See `PROTOTYPE_README.md` for the full, honestly-reported result — a weak, statistically inconclusive pooled pattern, not evidence of a working strategy.
- **`docs/`** — `CANDIDATE_UNIVERSE_V1.md`, scoping the live pipeline's watchlist (108 companies across 9 sectors) for the next phase of work.

## Current status (as of this export)

**Do not trade on any of this.** The prototype result is explicitly "interesting example, no credible inference." The live pipeline's most important missing piece, confirmed by directly reading the code rather than the design docs: `entity_relationships` and `candidate_signals` are only ever written to inside test fixtures — there is no production code yet that calls the extraction prompt against a real LLM, resolves entity names, and writes relationships/candidates to the database. That bridge (raw filing → LLM extraction → entity resolution → relationship upsert → candidate generation) is the top priority, ahead of finishing the watchlist or any infrastructure/hosting decisions.

Robinhood/execution integration stays explicitly blocked until a prospective Arm A vs. Arm G comparison clears the spec's own promotion gate (`specs/diffusion-experiment-spec-v2.1.md`, §9-10).

## Working in this repo

This was exported from a Claude session's working directory, not authored with git from the start — so the first commit here should be treated as an initial import, not a clean history of how it was actually built (that history exists in the originating conversation, not in git). From here forward, treat this repo as the source of truth: further work (the extraction-runner bridge, live pipeline deployment, further prototype rounds) should branch and commit from this point rather than living only in a chat session again.
