A code review (adversarial, not another design round — the design itself
was already locked in and verified) found real bugs in the extraction-
runner bridge you built. I independently traced every one of these against
the actual code before sending this — they're confirmed, not just claims.
Fix these before we do the planned live (real LLM, read-only against
EDGAR) dry run.

**One correction on my end first:** the review bundle I generated mislabeled
`extraction_prompt_v1.md` and `schema.sql` without their `build/` prefix,
which produced a false "wrong prompt path" finding. Ignore that one —
`llm_client.py`'s `PROMPT_PATH` is correct as written. Don't change it.

## Fix these six

**1. `extraction_runs` retry is structurally broken.** The migration's own
`UNIQUE (document_id, extraction_prompt_version, extractor_model_id,
extractor_model_version)` constraint allows exactly one row total per that
identity — but `select_unprocessed_documents()` re-selects a document whose
only existing row has `status='failed'` (it only excludes `'success'`), and
`extract_document()` then tries to `INSERT` a second row under the same
identity. That's a guaranteed uniqueness violation on any retry, success or
failure. Make one row the state machine: claim/create it as `pending`
first (`INSERT ... ON CONFLICT (document_id, extraction_prompt_version,
extractor_model_id, extractor_model_version) DO NOTHING`, then check who
actually holds the row), and have a retry `UPDATE` that same row —
incrementing `attempt_count`, clearing `error`, setting the new
`raw_llm_output`/`status` — rather than inserting a new one. If a `pending`
row already exists and looks stale (define what "stale" means — e.g. older
than some timeout), that's a recovery policy to add explicitly, not to
leave undefined.

**2. Claim the extraction before calling the LLM, not after.** Right now
the "does a successful row already exist" check and the eventual `INSERT`
are two separate statements with the (potentially expensive, real-money)
LLM call in between — two runners can both pass the check and both pay for
the call. Use the same atomic claim from fix #1 to close this gap. Add a
test with two connections/two simulated workers claiming the same document
concurrently — this is exactly the coverage the existing idempotency test
doesn't provide (it only tests a successful rerun of the same config, never
a genuine race).

**3. `process_catalyst`'s "already done" check needs to be config-specific
and locked.** Right now `canonicalization_completed_at` is one column with
no awareness of which prompt/model produced it — reprocessing the same
catalyst under a new prompt or model version will see it non-NULL and
silently no-op, directly contradicting the design's requirement that
reprocessing "creates new extraction runs and new downstream event data."
Replace the single timestamp with an identity-scoped record (a
`catalyst_processing_runs`-style table keyed by catalyst + prompt version +
model configuration, same idea as `extraction_runs` one level up). While
you're in there, close the same race as #2: two overlapping calls for the
same catalyst+config must not both canonicalize it — use `SELECT ... FOR
UPDATE` (or the same claim-row pattern) rather than a bare read-then-write.

**4. Freeze `decision_at` after the whole catalyst is resolved, not once at
the top.** `process_catalyst` currently captures one `processed_at` before
any resolution or relationship-writing happens, and reuses it as both
`system_observed_at` for every relationship AND `decision_at` for every
event version — and generates each event's candidates inside the same loop
that's still writing later events' relationships. Restructure to match
§5a exactly: resolve entities and write every relationship for every event
in the catalyst first; only then capture `decision_at`; then generate
candidates for every event from the now-complete relationship graph. This
matters because right now an earlier event in a multi-event catalyst
literally cannot see a relationship a later event in the same catalyst
discovers — that's a real completeness gap in candidate generation, not
just a timestamp cosmetics issue.

**5. Enforce the JSON schema on the real LLM call, not just in prose.**
`llm_client.py` loads `self.schema` from `extraction_prompt_v1.md` but
never gives it to the Anthropic call — the only enforcement is the text
instruction "Return ONLY valid JSON matching the schema." Use Claude's
structured-output/tool-use mechanism to actually constrain the response,
and additionally run a real JSON Schema validator (not just the current
hand-rolled field checks in `validate_extraction_output`) before treating
anything as parseable. Keep the existing per-claim span/vocabulary cleanup
as a second pass after that.

**6. Two small, cheap correctness fixes while you're in this code:**
   - `validate_extraction_output` checks that `document_id` is present but
     never that it equals the document actually sent. Pass the real
     document_id in and require exact equality; fix the test helper (which
     currently returns the literal string `"irrelevant"` for every call,
     which is why this gap wasn't caught) and add a negative test for a
     mismatch.
   - `extraction_runs.raw_llm_output` currently stores the *cleaned* output
     (post-validation), not the actual raw provider response, and
     `extracted_events.raw_llm_output` stores a single event object rather
     than the full document-level output — neither matches "full structured
     output, for audit" from the schema/design doc. Store the true raw
     provider response separately from the cleaned/validated version and
     the drop log, and link `extracted_events` to its `extraction_run_id`
     so the audit trail is actually reconstructable.

## Explicitly deferred, do not fix this round

Relationship directionality (entity_a/entity_b order taken as-is from the
LLM with no ordering contract) — real, but needs a prompt change and
careful thought, not a quick patch, and won't affect a first mechanics-only
dry run. The §3a U.S.-tradable admission rule in `manual_resolve.py`,
alias-learning on manual resolution, and `normalize_entity_name`'s
legal-suffix edge cases — none of these get exercised until manual
resolution actually happens, which it won't in this dry run.
`first_executable_at` staying NULL — already tracked in the README, only
matters at the trade-execution stage. Leave all of these as they are; don't
scope-creep this fix round.

## When you're done

Re-run the full test suite plus whatever new tests fix #1/#2/#3 need
(concurrent-claim tests especially — the existing idempotency test doesn't
cover retry-after-failure or genuine concurrency at all, which is exactly
how bugs #1-#4 slipped through). Report back what changed, what the new
tests actually exercise, and confirm the six items above are each
independently addressed — I'll verify the load-bearing ones against the
actual code again before we move to the live dry run.
