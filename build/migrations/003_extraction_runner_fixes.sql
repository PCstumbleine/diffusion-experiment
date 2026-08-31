-- Migration 003 -- fixes from an adversarial code review of the
-- extraction-runner bridge (migration 002), done before the first live
-- (real LLM, read-only) dry run. See build/README.md and
-- extraction_runner.py's module docstring for the full narrative; this
-- file carries only the schema-level changes.

-- ============================================================
-- Fix #1/#2: extraction_runs needs a claimable state machine, not a
-- check-then-insert race. A retry-after-failure must UPDATE the existing
-- row (the identity UNIQUE constraint allows exactly one row per
-- document+prompt+model), and two workers racing to claim the same
-- document must not both pay for the LLM call. 'pending' was already a
-- valid CHECK value in migration 002 (unused until now) -- this adds the
-- columns the new claim-then-update flow needs.
-- ============================================================

ALTER TABLE extraction_runs ADD COLUMN cleaned_llm_output JSONB;
ALTER TABLE extraction_runs ADD COLUMN validation_drop_log JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Fix #6b: raw_llm_output must be the TRUE raw provider response (the
-- schema's own "full structured output, for audit"), not the
-- post-validation cleaned version -- that now lives in cleaned_llm_output,
-- alongside the drop log describing what per-claim validation removed.
ALTER TABLE extraction_runs
    ADD CONSTRAINT extraction_runs_success_has_cleaned_output
    CHECK ((status = 'success') = (cleaned_llm_output IS NOT NULL));

-- ============================================================
-- Fix #6b (second half): extracted_events must link back to the
-- extraction_run that produced it, so the true raw response + cleaned
-- version + drop log are reachable from an extracted_events row -- not
-- just the single event object extracted_events.raw_llm_output stores.
-- ============================================================

ALTER TABLE extracted_events ADD COLUMN extraction_run_id UUID REFERENCES extraction_runs(extraction_run_id);
-- No pre-existing production data (see build/README.md); safe to tighten
-- directly rather than a staged backfill.
ALTER TABLE extracted_events ALTER COLUMN extraction_run_id SET NOT NULL;
CREATE INDEX idx_extracted_events_extraction_run ON extracted_events (extraction_run_id);

-- ============================================================
-- Fix #3: catalysts.canonicalization_completed_at (migration 002) was one
-- column with no awareness of WHICH prompt/model configuration produced
-- it -- reprocessing the same catalyst under a new prompt or model
-- version would see it non-NULL and silently no-op, contradicting the
-- design doc's own requirement that reprocessing under a new identity
-- produces new downstream data. Replaced with an identity-scoped
-- claim/state table, the same idea as extraction_runs one level up --
-- this also closes the race where two overlapping process_catalyst calls
-- for the same catalyst+config could both canonicalize it.
-- ============================================================

ALTER TABLE catalysts DROP COLUMN canonicalization_completed_at;

CREATE TABLE catalyst_processing_runs (
    catalyst_id                  UUID NOT NULL REFERENCES catalysts(catalyst_id),
    extraction_prompt_version      TEXT NOT NULL,
    extractor_model_id                TEXT NOT NULL,
    extractor_model_version              TEXT NOT NULL,
    status                                 TEXT NOT NULL CHECK (status IN ('pending','success','failed')),
    started_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                             TIMESTAMPTZ,
    error                                      TEXT,
    PRIMARY KEY (catalyst_id, extraction_prompt_version, extractor_model_id, extractor_model_version)
);

CREATE INDEX idx_catalyst_processing_runs_catalyst ON catalyst_processing_runs (catalyst_id);
