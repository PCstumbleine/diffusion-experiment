ChatGPT reviewed Dry Run 001, and I independently verified its findings the
same way as the previous two rounds before sending this. One of its
findings corrected something I had gotten wrong myself — worth knowing
going in, since it changes what "verified" means for the test-suite claim
below.

## Correction to how the dry run's own verification was done

I claimed "84/84 tests passing" was independently verified from
`.pytest_cache/v/cache/nodeids` (84 entries) and `lastfailed` (`{}`).
ChatGPT pointed out that's weaker than I said, and I checked pytest's
actual source to confirm: `cache/nodeids` is a set that's loaded from the
*previous* cache and only ever added to — `self.cached_nodeids =
set(config.cache.get("cache/nodeids", []))`, then
`self.cached_nodeids.update(item.nodeid for item in items)` — never reset.
Seeing 84 there proves 84 *distinct* tests are known to exist across some
history of runs, not that all 84 executed (and passed) in one specific
invocation. `lastfailed` being `{}` is consistent with zero failures, but
it's also only rewritten when it *changes* from the previously saved
value, so its old timestamp doesn't tell us much either.

**When you re-run the suite for this fix round, please capture real
output, not just cache files**: `pytest -v --junitxml=build/tests/pytest_run_002.xml`
(or equivalent), and report the actual pass/fail/skip counts from that
run's output directly, plus the exit code. Check the XML/output in as a
durable artifact the same way `DRY_RUN_REPORT_001.md` and the extraction
JSONs are — this is exactly the kind of claim this project's discipline
requires being able to independently check, and cache-file inference
isn't good enough for it going forward.

## Fix 1 — Eli Lilly (and Deere, Merck, JPMorgan) `&`/`and` resolution gap

**Do not change `normalize_entity_name()` or `_CORPORATE_SUFFIXES`.**
ChatGPT's alternative is better and I confirmed it works by running the
actual current function: seeding an additional alias per entity, with `&`
replaced by the word `and`, resolves correctly using the *existing,
unmodified* normalizer — `normalize_entity_name("ELI LILLY and Co")` and
`normalize_entity_name("Eli Lilly and Company")` both already produce
`"eli lilly and"`. No normalizer change needed at all.

The reasoning for leaving the normalizer alone: I checked what a global
`&`→`and` canonicalization plus an `"and"`-suffix would do to real seeded
companies, and it's worse than it looks. `Johnson & Johnson` currently
normalizes to `johnson johnson`; canonicalizing `&`→`and` would silently
change that to `johnson and johnson` (since `"and"` isn't at the trailing
position there, it wouldn't even get stripped) — a change to an existing,
already-working normalized form with no benefit, requiring every existing
`entity_aliases.normalized_alias` row to be recomputed and re-checked for
new collisions across the whole 108-company set. The alias-only approach
touches nothing that already works.

**What to actually do**: in `seed_entities.py`, for every entity whose
`legal_name` contains a literal `&`, additionally insert one
`entity_aliases` row with `&` replaced by `" and "` (collapse extra
whitespace), alongside whatever aliases it already generates. Checked
against the real seed data, this currently affects at least `DEERE & CO`,
`ELI LILLY & Co`, `Merck & Co.`, and `JPMORGAN CHASE & CO` — not just
Lilly. Add a test asserting that for each of these, both the `&` form and
a spelled-out `"...and..."` form of the name resolve to the same
`entity_id`.

**Also add the deterministic issuer-identity shortcut ChatGPT
suggested**, since it's a real, separate hardening and not just a
workaround for this one normalization gap: when `process_catalyst`
already knows the catalyst's issuer entity (from the filing's own CIK,
via `watchlist_membership`), and an extracted entity in that catalyst's
events has `role="issuer"`, resolve it directly to the known issuer
`entity_id` rather than running it through name-based resolution at all.
Third-party counterparties and mentions of the issuer in *other*
companies' filings still go through normal resolution — this only
shortcuts an issuer identifying itself in its own filing, which the
pipeline already knows independently of what string the LLM happened to
extract. Add a test: an issuer entity whose extracted name would
otherwise fail resolution (e.g., a fabricated normalization mismatch)
still gets a populated `event_entities` row when it's the catalyst's own
known issuer.

## Fix 2 — `reference_source` schema + a real provenance invariant

Change `extraction_prompt_v1.md`'s output schema:
`"reference_source": {"type": ["string", "null"]}`. Bump
`extraction_prompt_version` (this fix round changes the schema in two
places — see Fix 4 below — so one version bump, e.g. `1.2.0`, covers
both; update `PROMPT_VERSION` in `llm_client.py` to match).

Additionally — this is ChatGPT's addition, and it's a real gap I hadn't
caught: add a check in `validate_extraction_output()` that if
`surprise.reference_value` (or, after Fix 3, either new range field) is
non-null, `surprise.reference_source` must be a non-empty string. Treat a
violation the same way other per-claim validation failures are already
handled (drop that claim into `validation_drop_log`, per the existing
belt-and-suspenders pattern — don't hard-fail the whole document over
one bad claim, consistent with how the rest of this function already
works). The point: a number with no stated source is an unexplained
benchmark, not a fact — this closes that off structurally rather than
relying on extraction discipline alone.

## Fix 3 — Explicit range fields for guidance (add, don't compute a midpoint)

Add `observed_value_low`/`observed_value_high` and
`reference_value_low`/`reference_value_high` (all nullable numbers) next
to the existing `observed_value`/`reference_value` in both the JSON
Schema (`extraction_prompt_v1.md`) and `extracted_events`
(new migration `004_range_valued_guidance.sql`). Update the task
instructions: use the existing point fields for a single stated figure,
the new low/high fields for a stated range, and never populate both the
point field and the range fields for the same claim. Add a CHECK
constraint (`observed_value_low IS NULL OR observed_value_high IS NULL
OR observed_value_low <= observed_value_high`, same shape for
reference_value_low/high) — reject a range where low > high rather than
silently accepting nonsense.

Deliberately do NOT compute or store a midpoint anywhere, and don't touch
`surprise_transform.py` — a range-aware transform is a real, separate
design decision for later (this matches the project's existing "raw
values now, deterministic transform later, never both from the LLM"
principle — see `extraction_prompt_v1.md`'s own "Section 3" instructions).
`surprise_transformed` simply stays NULL for a range-valued event until
that transform exists, same as it already does today when
`observed_value` is null.

## Fix 4 — Cover-page stubs should extract to an empty `events` array

A cover page that only says "we issued a press release, see Exhibit
99.1" with no substantive figure or economic proposition of its own
isn't a distinct economic event by the extraction prompt's own
definition — it's a pointer to one. Update the task instructions to say
so explicitly (something like: "A cover-page or Item 2.02 statement that
only announces or incorporates an attached exhibit by reference, without
itself stating a substantive fact, is not an extractable event on its
own — return an empty events array for it; extract the actual content
from the exhibit that carries it instead"). This removes the
contentless `earnings_surprise` stub that currently prevents a cover
page from canonicalizing with its own exhibit's real event (both events
existing separately was flagged as a real, if non-breaking, artifact in
the dry run report). Don't change canonicalization/fingerprinting itself
to try to merge these — ChatGPT's point, and I agree with it: weakening
the fingerprint match to merge a contentless and a substantive event
risks merging things that aren't actually the same claim. Fixing what
counts as an event in the first place is the more correct fix.

Add a test: a synthetic cover-page-only document (references an exhibit,
states no figures) produces zero events; a cover page that *does* state
something substantive on its own (e.g., a specific dollar figure or
named counterparty right there on the cover) still produces an event.

## How to describe this round when you're done — a framing correction, not a code fix

ChatGPT made a fair point I want carried forward, not just noted: Dry Run
001's manual extraction deliberately extracted only some of the true
events/relationships in each document (2 of Lilly's 4 acquisitions, 1
counterparty per NVIDIA sentence instead of all named ones) to keep the
round reviewable. That means it validated real-data *pipeline wiring and
precision* — do hand-verified facts survive validation and flow through
resolution/canonicalization/relationship-writing/candidate-generation
correctly — but it did NOT validate extraction *recall/completeness*,
and the unresolved-mention rate and candidate counts in that report
aren't representative of what a full-recall extraction would produce.
Please add a short, clearly-labeled addendum section to the top of
`DRY_RUN_REPORT_001.md` (don't rewrite the body) stating this distinction
plainly, plus a one-line note correcting the "84/84 passing" language per
the pytest-cache correction above — the report is a durable record and
should say accurately what it does and doesn't establish.

## Explicitly deferred — do not do this round

A per-company filing-volume cap (`--since` / `--max-new-filings-per-company`
on `edgar_ingest_worker.py`, `--max-documents` / `--max-catalysts` on
`extraction_runner.py`) and measuring real token counts on large
documents (the 1.1MB CAT exhibit) before any real paid API call — both
real, both worth doing, but neither blocks continuing free,
Claude-Code-as-extractor dry runs, and I don't want to scope-creep this
round past the four confirmed issues plus the issuer-identity hardening.
Same for the candidate-effective-N reporting distinction (5 candidate
rows from one relationship isn't 5 independent signals) — a downstream
analysis/reporting concern, not a pipeline bug. Tracked, not forgotten.

## When you're done

Re-run the full suite with real captured output (see the correction
above), report the actual command and its real pass/fail counts, and
confirm each of the four fixes plus the issuer-identity shortcut
independently — I'll verify the load-bearing ones against the actual
code again before we do another dry run.
