You're reviewing the results of the first live dry run of an
extraction-runner bridge for a financial-events research pipeline (an
AI-assisted trading research project, eventually meant to plug into a
brokerage's agentic-trading API — this is the research/backtesting stage,
no live trading yet). You've reviewed this pipeline's design and code
twice already in earlier rounds and found real bugs both times, which
were fixed and independently re-verified before this dry run happened.

This round is different in kind: this is the first time the pipeline ran
against **real** SEC filings (4 real companies, their real, most-recent
8-K filings) rather than synthetic test fixtures. The LLM-extraction step
was done by Claude itself (reading the real filing text and the exact
extraction prompt, by hand, the same way a human reviewer would) rather
than a real API call, specifically to avoid any real-money cost for the
developer — so the "does the LLM actually behave well on real messy
filing text" question is being answered by inspection this round, not by
a live model call, which is a limitation worth keeping in mind.

The attached bundle contains: the full dry-run report, the one Python
module with a confirmed real bug, the relevant schema/prompt fragments,
two real extraction JSON outputs, and — importantly — a "Part 0" section
where I (the reviewing AI, not the one that ran the dry run) explain
exactly what I independently verified before trusting any of this, plus
three PROPOSED fixes in Part 5 that have NOT been implemented yet.

## What I'd like from you

1. **Sanity-check my verification method (Part 0).** I don't have shell
   access to the developer's machine, only a file read/write bridge — so
   I verified the "84/84 tests passing" and "5 commits" claims via raw
   pytest cache files and the raw git reflog rather than running the
   commands myself. Is there a blind spot in that approach — some way
   these specific artifacts could look right while the underlying claim
   is still false? (For example: could `nodeids` show 84 entries from a
   stale prior collection rather than a fresh one? I checked its mtime
   fell within the dry run's real time window, but I'd like your read on
   whether that's actually sufficient.)

2. **Stress-test proposed fix 5a (the Eli Lilly `&`/`and` normalization
   fix).** Canonicalizing `&` → `and` before punctuation-stripping, and
   adding `"and"` to the trailing-suffix-stripping list. Does this create
   any realistic false-positive risk — two different real companies whose
   normalized names would now collide when they didn't before? Is there a
   safer way to close this gap that doesn't touch a general-purpose
   suffix list? Should `&` even be treated as equivalent to `"and"`, or
   are there real SEC-registered companies where that specifically breaks
   something (e.g., a company using `&` as a literal symbol distinct from
   the word)?

3. **Weigh in on proposed fix 5b** (making `reference_source` nullable in
   the JSON Schema, bumping the prompt version). This one seems
   mechanical to me — tell me if I'm missing a reason it was deliberately
   left non-nullable.

4. **Recommend one of the three options in 5c** (range-valued guidance —
   add explicit low/high fields, adopt a midpoint convention, or leave
   unusable for now) or propose a better one. This is a real,
   recurring-by-nature design decision (the report notes most large-cap
   guidance is given as a range, not a point), not a bug, so I'd like
   your reasoning, not just a pick.

5. **Anything else in the report that looks off**, even if nothing
   "failed" — you've been good at catching things the previous rounds'
   reviews missed (relationship directionality, the config-unaware
   idempotency check, the frozen-too-early `decision_at`), so the same
   scrutiny here is welcome. In particular Part 6 flags two open
   questions (no per-company filing-volume cap, and cover-page stubs not
   canonicalizing with their own exhibit) — feel free to weigh in on
   those too if you have a view.

Please structure your response the same way as your last two reviews:
ranked findings/recommendations with your reasoning, not just a verdict.
I will independently verify anything you flag against the actual code
before acting on it (same discipline as the last two rounds) — so it's
fine, and useful, to flag something even if you're not 100% sure it's
real.
