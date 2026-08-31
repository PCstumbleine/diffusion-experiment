# The Diffusion Experiment — first build

This is the first real code from the spec (v2.2.1), built with Sonnet 5 and
tested against a live Postgres database in this session — not just written
and hoped to work.

## What's here

- `schema.sql` — the database structure. Ran successfully against Postgres 16.
  Includes a manual check (in the build log, not this folder) confirming the
  most important fix — a relationship valid since 2022 but not publicly known
  until 2026 correctly stays invisible to a 2024 query.
- `extraction_prompt_v1.md` — the prompt that reads a document and pulls out
  structured facts (event type, companies involved, raw numbers). Its JSON
  output schema was checked and parses correctly. It deliberately never
  computes statistics itself — see the file for why.
- `surprise_transform.py` — the small deterministic script that turns raw
  numbers into a standardized "surprise" value, replacing the formula that
  used to blow up near zero (e.g., EPS guidance moving from $0.01 to $0.05
  used to register as "+400%" — this fixes that).
- `edgar_ingest_worker.py` — the program that would poll SEC EDGAR for new
  filings and store them, including exhibits (an 8-K's cover page routinely
  just says "see Exhibit 99.1" for the actual earnings numbers). Its
  database-writing logic is tested; its own HTTP code has NOT been run
  against the live SEC API in this session (no outbound network access from
  this sandbox's Python process). The URL shapes and document-listing
  format it assumes WERE independently checked against live sec.gov pages
  (see "A second round of code review" below, which is exactly what caught
  a URL that 404s and a document-type field that never matched anything) —
  but that was done with a separate tool, not this worker's own code path.
  See the file's docstring for what to check before running it for real.
- `candidate_coverage.py` — a small but important check: before building
  Arm A vs. Arm G portfolios, confirms every eligible candidate actually
  has a decision from BOTH models, not just that they draw from the same pool.
- `tests/` — 52 automated tests, all passing (26 after the first review
  round, 36 after the second, 52 after the third — see below). Several
  specifically try to break exact bugs found by review rounds (the
  bitemporal leak, the surprise-formula blowup, the Arm A/G candidate-pool
  mixing, a `--dry-run` flag that used to permanently corrupt real data,
  exhibit documents being skipped, a live-SEC URL that 404s, a filing
  failure that used to take down a whole poll batch, a real filing with
  duplicate exhibit types that used to be permanently un-ingestable, and
  more — see "A round of code review", "A second round of code review",
  and "A third round of code review" below) rather than just checking
  things look right.

## A round of code review (after this build existed)

Once real code existed, it got a round of review from ChatGPT — a genuinely
different, and more valuable, exercise than reviewing the design document,
because it caught bugs that only exist once something is actually built.
Four were confirmed and fixed:

1. **The EDGAR worker only fetched the primary document, not exhibits.**
   An 8-K's cover page is often just "see Exhibit 99.1" — the actual
   earnings/guidance numbers live in the exhibit. Fixed: the worker now
   fetches the filing's index and pulls in relevant exhibits (EX-99.x),
   all linked to one shared catalyst.
2. **`--dry-run` was writing a placeholder into the real database and
   committing it**, which then permanently blocked the real filing from
   ever being ingested (the "already seen this" check keyed off the same
   identity either way). Confirmed as a real bug, fixed: dry-run now writes
   nothing at all.
3. **The timestamp for "when did this become public" trusted SEC's
   acceptance time**, but SEC's own documentation (checked directly)
   confirms filings submitted after 5:30pm ET disseminate the next
   business day — meaning the old code could be wrong by an entire weekend.
   Fixed: the pipeline's own observed time is used instead, which is
   conservative and honest rather than assuming precision the data doesn't
   support.
4. **`content_hash` was globally unique**, so two distinct disclosures that
   happened to share identical boilerplate text would silently collapse
   into one row and one would vanish. Fixed: identity is now the SEC
   accession number + which document within it; content hash is kept only
   to flag likely duplicates, never to drop them.

Several smaller but real issues were also fixed: `arm_entries` couldn't
represent four of the seven experimental arms (no field said which sector
ETF or matched security an entry actually bought); `candidate_signals`
allowed the same company to be registered twice for one event; a test's own
comment claimed something the schema didn't actually guarantee (fixed by
writing the actual completeness check, `candidate_coverage.py`, that the
comment assumed existed); the extraction prompt asked the model to judge
"transmission" partly from "your knowledge," directly contradicting its own
document-only extraction rule; and a few small schema inconsistencies
(a stale unused column, a missing constraint, an unused watchlist field).

**Flagged but deliberately not fixed yet, because it's bigger and more
delicate:** the statistical test currently treats every individual trade as
equally weighted evidence, when several trades from one catalyst should
arguably count as one; and it has no protection against different catalysts
whose outcome windows overlap in time (e.g. same-week events in one sector
sharing a market-wide shock). Both are real methodological gaps, but fixing
them well needs its own careful redesign and its own new null simulations —
not a rushed patch. Until that's done, this statistical test should not be
trusted as a real "promote to real money" decision, even though it's fine
for exploring pilot data.

## A second round of code review — this time it caught a live-API bug

A second review (again ChatGPT, this time against the fixed v3 code) made a
point of distinguishing what it could check statically from what it
couldn't run at all in its own sandbox (no `psycopg2`/Postgres there
either) — and specifically flagged that the EDGAR exhibit-fetching fix
(review round 1, fix #1) was tested only against a mocked filing-index
response, which could be hiding a real interface mismatch with the live
SEC API.

That claim was checked directly against sec.gov (the same standard applied
to the 5:30pm-ET dissemination claim in round 1) rather than taken on
faith, by fetching a real, live SEC filing's index pages. It was correct,
and worse than "imprecise" — **the exhibit-fetching code could never have
worked against the real API at all:**

1. **The URL the worker requested for a filing's index
   (`{accession}-index.json`) 404s on live sec.gov.** The real per-filing
   directory listing lives at plain `index.json` (no accession prefix) —
   confirmed by fetching both live. But that real endpoint's `"type"` field
   turned out to be a generic file-icon category (e.g. `"text.gif"`), not
   an EDGAR document type like `"EX-99.1"` — confirmed by comparing it
   against that same filing's `-index-headers.html`, which does carry the
   real `<TYPE>`/`<SEQUENCE>`/`<FILENAME>` triplet per document. So even
   pointed at the URL that actually works, the old exhibit-matching logic
   could never have matched anything. **Fixed:** the worker now fetches
   `-index-headers.html` and parses the real `<TYPE>`/`<FILENAME>` pairs
   directly. Net effect of the bug as shipped in v3: every real
   (non-dry-run) poll would raise an uncaught exception fetching the index
   for every filing — the worker could never have ingested anything from a
   live run.
2. **That exception wasn't isolated per filing.** It would propagate out of
   `poll_once`'s loop and silently skip every *other* new filing for that
   company too, until the next poll — much worse than the existing
   "one company's failure doesn't take down the whole poll" isolation.
   **Fixed:** each filing's ingestion is now caught and logged
   individually. Making that fix surfaced a related bug of its own: a
   failed statement left un-rolled-back on a shared connection poisons
   every later query on it ("current transaction is aborted") — the new
   per-filing handler now rolls back before continuing.
3. **The "is this filing new" filter used SEC's acceptance time**, which
   re-created the exact problem fix #3 (round 1) was written to solve, one
   level up: since after-5:30pm-ET filings can disseminate — i.e. become
   visible in the API at all — as late as the next business day, a filing
   accepted Friday evening but not visible until Monday could fall outside
   a tight lookback window and be silently dropped forever (this filter is
   the *only* place a filing is ever discovered; the database dedup check
   never gets a chance to run on something filtered out here). **Fixed:** a
   fixed multi-day buffer is now added on top of `--lookback-hours`
   specifically so this filter can never be the reason a newly-disseminated
   filing goes unprocessed — exact-once correctness still comes entirely
   from the database dedup check, never from this time window.
4. Two smaller timestamp mislabelings in the same area: `source_published_at`
   was silently being set to SEC's acceptance time (reintroducing the
   "acceptance == public" conflation fix #3 removed, one column over — now
   left `NULL`, since the submissions API gives no genuine source-stated
   publication time), and `first_public_timestamp_precision` was hardcoded
   to the steady-state 15-minute poll interval even on a wide backfill run
   that could discover a filing hours after it was actually accepted (now
   `max(poll interval, time actually elapsed since acceptance)`).

A smaller, genuinely valid catch from the same review: `relationship_evidence`
still allowed a value called `model_inferred` ("you are inferring this
relationship yourself, not reading it from the text"), which directly
contradicted this same extraction prompt's own "if you cannot point to a
supporting span, do not extract the claim" rule — the same class of
contradiction round 1 already caught and removed for the transmission
field. Removed here too (extraction prompt bumped to 1.1.0); nothing was
extracted for real yet, so there's no legacy data using it.

One more gap was checked and fixed even though it wasn't about a live API:
the review pointed out that `arm_entries.candidate_id`,
`.model_decision_id`, `.event_version_id`, and `.entry_quote_snapshot_id`
were each individually valid foreign keys, but nothing stopped them from
being *mutually* inconsistent — e.g. a candidate belonging to a different
event than the entry's own, or a quote snapshot for a different instrument
than the entry is actually about. That's exactly the kind of corruption
that produces plausible-looking, silently wrong research data instead of
an obvious crash. Fixed with three trigger functions on `arm_entries` /
`arm_outcomes` that check cross-field consistency, each with a test that
confirms it rejects the inconsistent case and accepts the consistent one.

**Reviewed and deliberately not changed yet**, because it needs a small
design decision rather than an obvious fix: `candidate_coverage.py`'s
coverage check requires every model being compared to share one
`model_version` string, which can't express Arm A and Arm G genuinely
running different model versions (a real interface limitation, not urgent
while both arms happen to share a version in practice).

The other two items flagged in this same paragraph in the previous version
of this README — the `document_component` identity collision, and missing
HTTP retry tests — turned out not to be safe to leave deferred. See the
next section.

## A third round of code review — a deferred item turned out to be real, live data

A third review (ChatGPT again, against v4) went further than reading code:
it fetched several more live SEC filings on its own initiative and found a
real bug the previous two rounds had missed, which upgraded the one item
this README had called "low real-world likelihood, safe to defer."

**The document_component collision is not theoretical.** The reviewer
found a real, live 8-K — UDR Inc., accession `0000074208-26-000045`, filed
2026-04-29 — that contains *two* `EX-99.1` documents (one `.htm`, one
`.pdf`) and the same duplicate pattern for `EX-99.2`. That was independently
re-confirmed by fetching that exact filing's own `-index-headers.html`
directly rather than taken on faith. Under the identity scheme at the time
(`sec_accession_number` + the type label `document_component`), the second
`EX-99.1` would hit a database uniqueness violation — and because the
collision is deterministic, that filing would fail identically on every
future poll, forever. **Fixed:** identity is now `sec_accession_number` +
SEC's own per-document `SEQUENCE` number, which SEC's Public Dissemination
Technical Specification requires to be unique within one filing package;
`document_component` is kept only as a descriptive label.

That same live example exposed a second real bug: one of the duplicate
`EX-99.1` copies is a `.pdf`, and the code was calling `response.text` on
it unconditionally — which does not extract PDF text, it silently decodes
binary bytes as if they were characters. **Fixed:** a relevant exhibit
filed as `.pdf` is now skipped with a clear log message rather than
ingested as corrupted text (in every live example seen so far, an
`.htm`/`.txt`/`.xml` copy of the same exhibit is filed alongside it
anyway). An exhibit filed *only* as a PDF, with no text-format copy, is a
known, deliberately-not-built limitation — this pipeline has no PDF-text
extraction step yet.

Also fixed from the same round: the exhibit-index parser assumed a
document's `TYPE` field never contains whitespace, which is false for some
real EDGAR forms (confirmed live) — not a problem for this project's
actual target forms (8-K / EX-99.x), but the parser was restructured to
read each `<DOCUMENT>` block independently anyway (needed for the sequence
fix above), which fixes this as a side effect; a filing whose index
doesn't parse into any `<DOCUMENT>` entries at all now raises an explicit
error instead of silently "succeeding" as primary-only, since SEC's spec
guarantees every real filing has at least one such block for its own
primary document, so zero matches means something broke, not that there
are no exhibits; a failed exhibit fetch used to let the filing commit
anyway with that exhibit permanently missing (flagged but not fixed in
round 2) — ingestion is now genuinely all-or-nothing per filing, so a
partial failure rolls back completely and the next poll retries it
cleanly; the previous round's lookback buffer was replaced with no
time-based filtering at all, since SEC's own submissions API already
bounds each company's recent-filings list generously and the database
dedup check (not any time window) has always been what makes re-ingestion
safe; per-document timestamps within one filing package no longer drift by
a few seconds from each other; and two more schema consistency gaps were
closed — a cash-equivalent arm entry could still accidentally carry a
traded instrument, and a candidate for one company could be paired with a
traded instrument belonging to a *different* company (e.g. an NVIDIA
candidate entered against an Apple instrument) — both now enforced by
trigger, both tested. The previously-missing HTTP retry unit tests
(timeout, 503, 429, 403 — none need a database) were added too.

A follow-up pass on this same v4→v5 change (ChatGPT again, fetching yet
more live filings to specifically try to break the new sequence-based
parser) found two more small real gaps, both fixed: a `<DOCUMENT>` block
missing one of TYPE/SEQUENCE/FILENAME was silently skipped instead of
failing the whole filing (inconsistent with the fail-visibly approach used
everywhere else in this function); and a relevant exhibit that exists
*only* as a PDF (SEC's own rules allow this for some filing types, e.g.
8-K Item 6.10 asset-backed filings) would have been silently dropped
instead of failing visibly. Both now raise. A duplicate-SEQUENCE guard was
also added defensively, since SEC's spec requires SEQUENCE but doesn't, in
so many words, promise it's unique — this pipeline now enforces that
assumption itself rather than trusting it silently.

**Still reviewed and deliberately left open**, because it needs an actual
design decision, not a bug fix: nothing currently stops an Arm A entry from
referencing a decision that was actually recorded under `arm_g_mechanical`
(or vice versa) — `model_candidate_decisions.model_id` isn't tied to
`experiment_arms.arm_code` anywhere. The reviewer called this "important
but not a raw-ingestion blocker." Fixing it properly means deciding how an
arm declares which model(s) it trusts (a new column on `experiment_arms`,
most likely) rather than bolting on a trigger against today's schema, so
it's flagged here rather than rushed.

## How to actually run this yourself

You'll need Postgres installed and running, and Python with `psycopg2-binary`,
`requests`, and (for tests) `pytest` installed (`pip install psycopg2-binary
requests pytest`).

```
createdb diffusion_experiment
psql -d diffusion_experiment -f schema.sql
cd tests && python3 -m pytest -v
```

Before running `edgar_ingest_worker.py` for real: open it and (1) replace the
placeholder email in `USER_AGENT` with your real contact info — SEC requires
this, it's not optional — and (2) fill in `WATCHLIST` with the companies
(by CIK) you want to track, each paired with that company's `entity_id`
from your `entities` table.

## The statistical test (Section 8), and a real finding from testing it

`statistical_test.py` implements the actual pass/fail test: is Arm A's
advantage over Arm G bigger than delta, using a bootstrap that resamples
whole catalysts (not individual trades) so several trades from one news
event don't get counted as independent evidence.

Three simulations in `tests/test_statistical_null_simulations.py` check the
test itself before it's ever pointed at real data — feed it fake data with
a known right answer, see if it gets that answer:

- With no real effect at all, it should falsely say "there's an edge" about
  5% of the time (its stated confidence level), not much more.
- With a real, sizeable effect, it should actually detect it most of the
  time — 100% in this simulation.
- Ignoring the catalyst-clustering requirement should make the test
  noticeably easier to fool. Confirmed: under the same correlated data, the
  clustered test's false-positive rate stayed near 6%, while a naive version
  that ignores clustering was fooled **28% of the time** — a concrete
  demonstration of why Section 8 requires clustering, not just a
  theoretical concern.

**One genuine finding, not just confirmation:** with a modest number of
distinct catalysts (15–40, which is a realistic range early in Phase I-A),
the test runs slightly "hot" — a 7–9% false-positive rate against a 5%
target, rather than exactly 5%. This is a known property of this kind of
bootstrap with a limited number of clusters, not a bug in the code; it gets
closer to 5% as the number of distinct catalysts grows, but doesn't fully
disappear with the simple version implemented here. Practical takeaway:
the spec's existing sample-size requirement ("effective sample size in the
low hundreds") is about total observations — it should also specify a
minimum number of distinct catalysts (a reasonable starting target is 50+)
before trusting this test's result, since observation count alone doesn't
guarantee that.

## What's not built yet

The extraction prompt exists but isn't wired up to an actual LLM API call
yet — that's the next piece (send a document's text through the prompt,
parse the JSON response, insert into `extracted_events`). The underreaction
estimator's math (Section 4) isn't implemented as code yet — that comes
after there's real data flowing through the pipeline to test it against.
