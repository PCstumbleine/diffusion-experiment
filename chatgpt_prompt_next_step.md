Quick recap of where this stands, then a plan I'd like you to pressure-test
before I act on it — not a code review this time, a "is this the right
next move" check.

## Where things stand

Dry Run 001 (real EDGAR filings, 4 companies, no paid API — Claude itself
acted as the extractor under its existing subscription, reading the
filings and the extraction prompt by hand) found four real issues, which
you reviewed and I acted on:

1. Eli Lilly's own press release ("Eli Lilly and Company") failed to
   resolve against its own seeded entity ("ELI LILLY & Co") — fixed via a
   seeded alias (your recommendation over my original global-normalizer
   idea, which I verified would've broken already-working names like
   "Johnson & Johnson").
2. `reference_source` couldn't be null in the JSON Schema despite the
   prompt's own "use null when unstated" rule — fixed, plus your added
   invariant (a reference figure with no source gets dropped).
3. Range-valued guidance ("$85.0B to $87.0B") had nowhere to go but two
   null fields — fixed with explicit low/high columns, no midpoint
   computed anywhere, canonicalization fingerprint updated so two
   different ranges can't collide.
4. Cover-page stubs ("see Exhibit 99.1") were extracting as a contentless
   event — fixed at the prompt level, empty events array instead.

All four independently re-verified against the actual code (not taken on
the implementer's word) — 94 tests, real captured `pytest -v` output and
a JUnit XML this time, not the cache-file inference you correctly flagged
as weaker than I'd claimed last round. Every fix does what it says.

## The two things still open from your last review

You flagged these and I agreed, but neither has been acted on yet:

- **A filing-volume safety valve.** `edgar_ingest_worker.py` has no cap —
  scoping to 4 companies via `--only-ciks` still pulled each company's
  entire historical 8-K backlog (411 documents from just 4 companies).
  Free in dollar terms right now (Claude-as-extractor, not a paid API),
  but it means onboarding even one or two more companies to a future dry
  run repeats that same surprise.
- **Extraction recall/completeness was never actually tested.** Dry Run
  001's manual extraction deliberately extracted only *some* of the true
  events (2 of Lilly's 4 named acquisitions, 1 counterparty per NVIDIA
  sentence). That validated pipeline wiring — do hand-picked real facts
  survive validation and flow through correctly — but says nothing about
  whether a real extraction pass would actually find everything a
  document contains. This is the bigger gap of the two.

## My proposed next step (three parts, in this order)

**1. Add the filing-volume safety valve now** — a `--since` and/or
`--max-new-filings-per-company` flag on `edgar_ingest_worker.py`, default
unlimited so normal polling semantics don't silently change. Small,
low-risk, unblocks scaling later without another surprise.

**2. Test recall directly, before running anything new.** Dry Run 001
already saved the real fetched source text for all 9 documents
(`build/dry_run_extractions/sources/*.txt`) and Claude Code's actual
extraction JSON for each. Rather than trusting Claude Code to grade its
own completeness (which isn't independent — same model, just a different
session), I want an actually independent read: someone who did NOT
produce the original extraction reads the real filing text directly and
lists what `extraction_prompt_v1.md` says should be extracted, then that
list gets diffed against what was actually captured. The best candidate
document is Lilly's Q2 earnings release (`8b89a41e-...`) — the richest
one from the dry run, six distinct events, and it's the same filing the
Eli Lilly bug came from, so it's a good test of whether the fix actually
helps on the full document, not just the one sentence that was hand-picked
before.

**Here's what I actually want your read on**: should *I* (Claude, in
Cowork — a different session, but the same underlying model family as
the extractor) do this independent read, or should *you* do it, given the
raw filing text as an attachment? My instinct is that "Claude checking
Claude" has a real weakness — correlated blind spots, the same kind of
document a Claude-family model might skim past twice for the same reason.
You're a genuinely different model with different failure modes, which
seems like the more valuable independent check here, the same reason your
adversarial code reviews have caught things I didn't. But I don't want to
assume that without asking — is cross-model checking actually meaningfully
better for THIS kind of task (careful reading comprehension against a
fixed rubric), or does that reasoning not hold up? Would both of us doing
it independently and comparing be worth the extra round, or is that
overkill for what's still a fairly small, low-stakes check?

**3. Run Dry Run 002 after that** — re-poll the same 4 companies (mostly
just confirming no new filings slipped through unhandled) specifically to
confirm the four fixes behave correctly against live, freshly-fetched
data: does Eli Lilly's own filing actually resolve now, does a real
range-valued guidance disclosure actually get captured in the new
columns, does a real cover page actually produce zero events. Same report
format as Dry Run 001.

## What I'd like from you

1. Is this the right next step, or is there something more urgent I'm
   missing given everything so far?
2. The cross-model-checking question above — who should do the
   independent recall read, and why.
3. Anything wrong with the sequencing (safety valve → recall check → Dry
   Run 002), or should this be reordered?
4. Is a single document (Lilly's Q2 release) enough for a first recall
   check, or does one document tell us too little to act on either way?

Same as always — I'll independently verify anything you flag before
acting on it.
