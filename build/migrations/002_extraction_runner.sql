-- Migration 002 — Extraction-runner bridge
-- Implements docs/EXTRACTION_RUNNER_DESIGN_V2.md.
--
-- schema.sql's own content is treated as migration "001" retroactively (it
-- was never versioned before this). Apply schema.sql first, then this file,
-- in order -- see build/README.md.
--
-- Sections below are numbered to match the design doc's own section
-- numbers (§1, §2b, §2c, §3, §4, §4a) so a reviewer can cross-reference
-- directly, not because they reflect dependency order within this file
-- (they do also happen to, since each section only references tables
-- defined earlier in the file or in schema.sql).

-- ============================================================
-- §1. Entity, alias, and instrument seeding support
-- ============================================================

-- The current schema only INDEXES cik (idx_entities_cik, non-unique) -- it
-- does not stop two entity rows from claiming the same issuer. Replaced
-- with a partial unique index (Postgres has no UNIQUE ... WHERE clause on
-- ADD CONSTRAINT, so this is done as an index, same technique already used
-- by idx_raw_documents_accession_sequence in schema.sql).
DROP INDEX IF EXISTS idx_entities_cik;
CREATE UNIQUE INDEX idx_entities_cik_unique ON entities (cik) WHERE cik IS NOT NULL;

CREATE TABLE entity_aliases (
    alias_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id                UUID NOT NULL REFERENCES entities(entity_id),
    alias_text                TEXT NOT NULL,           -- as written (e.g. "NVIDIA Corporation")
    normalized_alias           TEXT NOT NULL,           -- lowercased, punctuation-stripped, corporate-suffix-stripped
    alias_source                 TEXT NOT NULL,         -- 'seed_legal_name' | 'seed_bare_name' | 'manual_resolution' | ...
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_id, normalized_alias)
);

-- Deliberately NOT unique on normalized_alias alone: two different entities
-- CAN legitimately collide on a normalized alias (rare, but the resolver
-- must detect this as ambiguous rather than the schema silently forbidding
-- it or silently picking one). See entity_resolution.py.
CREATE INDEX idx_entity_aliases_normalized ON entity_aliases (normalized_alias);
CREATE INDEX idx_entity_aliases_entity ON entity_aliases (entity_id);

-- The design doc's own suggested alternative to hand-copying entity_id
-- UUIDs into edgar_ingest_worker.py's WATCHLIST: a tiny membership table
-- the worker resolves at startup. Deliberately separate from `entities`
-- itself (not a boolean column there) so the polling-watchlist vs.
-- candidate-entity-universe split (§3a) is structural, not just documented
-- -- an entity manually resolved as a counterparty (§3a) never appears
-- here unless someone deliberately decides to also poll it as a filer.
CREATE TABLE watchlist_membership (
    entity_id                UUID PRIMARY KEY REFERENCES entities(entity_id),
    cik                        TEXT NOT NULL,
    added_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cik)
);

-- ============================================================
-- §2b. extracted_events — nullable surprise block (real bug fix)
-- ============================================================

-- extraction_prompt_v1.md's own JSON schema allows "surprise": null (a
-- capacity_change/acquisition_or_divestiture event legitimately has no
-- numeric surprise). Every other column in the surprise block
-- (observed_value, reference_value, reference_source, reference_timestamp,
-- unit, period) was already nullable in schema.sql -- only surprise_type
-- itself was NOT NULL, making that valid extraction un-insertable.
ALTER TABLE extracted_events ALTER COLUMN surprise_type DROP NOT NULL;

-- ============================================================
-- §2c. extraction_runs — idempotency + provenance
-- ============================================================

CREATE TABLE extraction_runs (
    extraction_run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id                  UUID NOT NULL REFERENCES raw_documents(document_id),
    extraction_prompt_version      TEXT NOT NULL,
    extractor_model_id               TEXT NOT NULL,   -- e.g. 'anthropic:claude-...'
    extractor_model_version            TEXT NOT NULL, -- swapping the model under an unchanged prompt
                                                        -- version is a different extractor -- tracked
                                                        -- as one (design doc §2c).
    status                               TEXT NOT NULL CHECK (status IN ('pending','success','failed')),
    attempt_count                        INT NOT NULL DEFAULT 1,
    raw_llm_output                       JSONB,         -- full structured output; set on success
    error                                TEXT,          -- set on failure (validation or LLM-call error)
    started_at                           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                          TIMESTAMPTZ,

    -- Idempotency key: document + prompt version + model configuration,
    -- NOT prompt version alone (design doc §2c) -- reprocessing under a
    -- bumped prompt version or a different model creates a NEW row here,
    -- never overwrites an old one.
    UNIQUE (document_id, extraction_prompt_version, extractor_model_id, extractor_model_version),
    CHECK ((status = 'success') = (raw_llm_output IS NOT NULL)),
    CHECK ((status = 'failed') = (error IS NOT NULL))
);

CREATE INDEX idx_extraction_runs_document ON extraction_runs (document_id);
CREATE INDEX idx_extraction_runs_status ON extraction_runs (status);

-- Catalyst-level canonicalization idempotency (added beyond the design
-- doc's explicit list): canonical_events has no natural business key that
-- could be UNIQUE-constrained (a catalyst can legitimately produce several
-- events of the same event_category), so re-running the catalyst-level
-- merge step (§2a) needs an explicit "already merged" marker to stay a
-- true no-op on replay, the same way extraction_runs already makes
-- per-document extraction idempotent one level down. Flagged here because
-- it's a mechanical necessity the design doc didn't spell out, not a
-- reinterpretation of anything it decided.
ALTER TABLE catalysts ADD COLUMN canonicalization_completed_at TIMESTAMPTZ;

-- ============================================================
-- §3. Entity resolution — unresolved mentions
-- ============================================================

CREATE TABLE unresolved_entity_mentions (
    mention_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_name                    TEXT NOT NULL,          -- exactly as extracted from the document
    normalized_name               TEXT NOT NULL,
    document_id                     UUID NOT NULL REFERENCES raw_documents(document_id),
    extraction_run_id                 UUID NOT NULL REFERENCES extraction_runs(extraction_run_id),
    first_seen_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status                                TEXT NOT NULL DEFAULT 'unresolved' CHECK (status IN ('unresolved','resolved')),
    resolved_entity_id                     UUID REFERENCES entities(entity_id),
    resolved_at                              TIMESTAMPTZ,
    CHECK ((status = 'resolved') = (resolved_entity_id IS NOT NULL AND resolved_at IS NOT NULL))
);

CREATE INDEX idx_unresolved_entity_mentions_status ON unresolved_entity_mentions (status);
CREATE INDEX idx_unresolved_entity_mentions_document ON unresolved_entity_mentions (document_id);

-- ============================================================
-- §4 / §4a. entity_relationships — provenance + closed vocabulary
-- ============================================================

-- Closed vocabulary + directionality convention (§4a), exactly the design
-- doc's suggested set -- see extraction_runner.py's module docstring for
-- the directionality convention this fixes ("a supplies b", etc.) and how
-- the LLM's own free-text relationship_type is mapped into it.
ALTER TABLE entity_relationships
    ADD CONSTRAINT entity_relationships_type_vocabulary
    CHECK (relationship_type IN ('supplier','customer','competitor','partner','acquirer_target'));

-- Provenance FK (§4): makes idempotency enforceable at the relationship
-- level too, not just at the document level -- re-running the SAME
-- extraction run's catalyst-merge must not insert a second, duplicate
-- assertion of the same relationship. A LATER, independent disclosure
-- (a new document, a new extraction_run_id) legitimately DOES get its own
-- new row -- this does not turn the table into an upsert-on-relationship
-- target, only prevents literal replay of one run's own output.
ALTER TABLE entity_relationships
    ADD COLUMN extraction_run_id UUID REFERENCES extraction_runs(extraction_run_id);

-- No pre-existing production data (README: "no real extraction has run
-- yet"), so this can be tightened to NOT NULL directly rather than a
-- staged backfill.
ALTER TABLE entity_relationships ALTER COLUMN extraction_run_id SET NOT NULL;

ALTER TABLE entity_relationships
    ADD CONSTRAINT entity_relationships_run_pair_type_unique
    UNIQUE (extraction_run_id, entity_id_a, entity_id_b, relationship_type);
