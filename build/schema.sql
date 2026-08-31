-- The Diffusion Experiment — database schema
-- Implements v2.2.1, Section 7, using the corrected field names from Section 3
-- (evidence_publicly_available_at / system_observed_at) and the X_i definition
-- from Section 4 (expected_transmission_effect excluded from primary features).
--
-- Design notes:
--   * UUID primary keys throughout (gen_random_uuid() is built into Postgres
--     core since v13 — no extension required).
--   * pgvector is used for embedding-based novelty dedup (Section 10). It is
--     NOT installed in this sandbox, so the embedding column below is a
--     placeholder (bytea) with a commented-out real definition. In the real
--     deployment: `CREATE EXTENSION vector;` then swap the commented column in.
--   * Nothing here grants broad privileges. Section 10/11's role separation
--     (insert-only pipeline role, separate admin role) is set up at the end,
--     in 03_roles.sql, deliberately kept apart from the table definitions so
--     the two concerns don't get tangled.

-- ============================================================
-- 1. Documents & events  (Sections 1, 7)
-- ============================================================

CREATE TABLE raw_documents (
    document_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_name             TEXT NOT NULL,                 -- 'sec_edgar', 'company_ir', ...
    source_url              TEXT,
    document_type           TEXT NOT NULL,                 -- '8-K', 'press_release', 'earnings_transcript', ...
    raw_content             TEXT NOT NULL,                 -- the document as fetched, unmodified
    content_hash            TEXT NOT NULL,                 -- sha256 of raw_content — kept for flagging, not for identity (see below)

    -- Filing-package identity (added after a review round caught that
    -- content_hash UNIQUE could silently collapse two DISTINCT disclosures
    -- that happen to share identical boilerplate text into one row). NULL
    -- for non-SEC sources (company-IR pages, etc.), which have no accession.
    sec_accession_number     TEXT,
    document_component        TEXT,   -- 'primary' | 'EX-99.1' | 'EX-99.2' | ... ; a DESCRIPTIVE label only, NOT identity (see sec_document_sequence) -- NULL when sec_accession_number is NULL
    -- A second review round found a REAL, live 8-K (UDR Inc., filed
    -- 2026-04-29) containing two EX-99.1 documents -- an .htm and a .pdf
    -- version of the same exhibit -- and the same for EX-99.2, confirmed by
    -- fetching that filing's own "-index-headers.html" directly. document_
    -- component alone ("EX-99.1") collides on exactly that real filing, not
    -- just a hypothetical one, which the original README flagged this
    -- gap as "low real-world likelihood, safe to defer" -- it isn't.
    -- sec_document_sequence is SEC's own per-document sequence number
    -- (required in every document tag nest per SEC's Public Dissemination
    -- Technical Specification), guaranteed unique within one filing package,
    -- and is the real identity now; document_component stays purely
    -- descriptive.
    sec_document_sequence    INT,    -- SEC's own per-document SEQUENCE; NULL when sec_accession_number is NULL
    duplicate_content_of_document_id  UUID REFERENCES raw_documents(document_id),  -- set when content_hash matches an earlier row, WITHOUT dropping this occurrence

    -- Full timestamp taxonomy (Section 1) — never collapsed into one field.
    source_published_at        TIMESTAMPTZ,                -- what the source claims
    sec_acceptance_at            TIMESTAMPTZ,              -- SEC's own acceptanceDateTime, recorded as a raw fact — NOT treated as the public-availability time (see canonical_first_public_at)
    source_observed_at         TIMESTAMPTZ,                -- when our ingestion saw it published
    ingested_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    canonical_first_public_at  TIMESTAMPTZ,                -- best estimate of true first disclosure
    first_public_timestamp_source     TEXT,                -- which document/feed set it
    first_public_timestamp_precision  INTERVAL,            -- explicit uncertainty window, e.g. '3 minutes'

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Identity is by filing-package component when we have one (SEC's own
-- accession number + which document within it), never by content bytes —
-- two distinct disclosures can legitimately share identical text, and for
-- information-diffusion research the OCCURRENCE of a disclosure is itself
-- data, not something to silently discard. content_hash stays indexed
-- (non-unique) purely to flag likely duplicates for review.
--
-- Uses sec_document_sequence, NOT document_component -- a review round
-- found a real filing where document_component ("EX-99.1") is not unique
-- within one accession (see the column comment above). SEQUENCE is.
CREATE UNIQUE INDEX idx_raw_documents_accession_sequence
    ON raw_documents (sec_accession_number, sec_document_sequence)
    WHERE sec_accession_number IS NOT NULL;
CREATE INDEX idx_raw_documents_content_hash ON raw_documents (content_hash);
CREATE INDEX idx_raw_documents_source_published_at ON raw_documents (source_published_at);

-- catalyst / event / version identifiers (Section 1): a catalyst is the
-- originating disclosure; it can produce several canonical_events (one
-- earnings release -> guidance event + buyback event + capacity event);
-- each canonical_event can have several event_versions (a same-day
-- correction of the same proposition).

CREATE TABLE catalysts (
    catalyst_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    originating_document_id UUID NOT NULL REFERENCES raw_documents(document_id),  -- the primary/cover document specifically
    -- The ingestion worker already knows which watchlist entity/CIK a
    -- filing came from -- recorded here directly rather than making
    -- extraction rediscover the issuer from document text later. NULL for
    -- sources that don't carry this at ingestion time (e.g. some IR feeds).
    -- FK to entities added below in Section 2, once that table exists --
    -- entities isn't defined yet at this point in the file.
    issuer_entity_id          UUID,
    issuer_cik                 TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_catalysts_issuer_entity ON catalysts (issuer_entity_id);

-- A catalyst is usually more than one document: the 8-K cover page plus its
-- exhibits (EX-99.1 routinely holds the actual earnings release/guidance
-- numbers the cover page just references). This links ALL of them to the
-- catalyst at ingestion time, before any event has been extracted from any
-- of them — event_document_links (below) is a separate, later link made
-- once specific evidence spans within a specific document support a
-- specific event.
CREATE TABLE catalyst_documents (
    catalyst_document_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalyst_id                UUID NOT NULL REFERENCES catalysts(catalyst_id),
    document_id                 UUID NOT NULL REFERENCES raw_documents(document_id),
    document_role                 TEXT NOT NULL CHECK (document_role IN ('primary','exhibit')),
    UNIQUE (catalyst_id, document_id)
);

CREATE INDEX idx_catalyst_documents_catalyst ON catalyst_documents (catalyst_id);

CREATE TABLE canonical_events (
    canonical_event_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    catalyst_id             UUID NOT NULL REFERENCES catalysts(catalyst_id),
    event_category          TEXT NOT NULL,   -- guidance_revision, capacity_change, order_win, ...
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_canonical_events_catalyst ON canonical_events (catalyst_id);

CREATE TABLE event_versions (
    event_version_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_event_id      UUID NOT NULL REFERENCES canonical_events(canonical_event_id),
    version_number          INT NOT NULL,
    superseded_by           UUID REFERENCES event_versions(event_version_id),
    event_effective_at      TIMESTAMPTZ,
    decision_at             TIMESTAMPTZ,     -- timestamp actually used for downstream decisions
    first_executable_at     TIMESTAMPTZ,     -- first executable timestamp per Section 4's trading-session policy
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (canonical_event_id, version_number)
);

-- Documents <-> events is many-to-many (Section 1): a single earnings
-- release can support several distinct events at once.
CREATE TABLE event_document_links (
    event_document_link_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_event_id      UUID NOT NULL REFERENCES canonical_events(canonical_event_id),
    document_id             UUID NOT NULL REFERENCES raw_documents(document_id),
    relationship_type       TEXT NOT NULL,   -- 'primary_source', 'corroborating', 'correction', ...
    evidence_span_start     INT,             -- character offset into raw_documents.raw_content
    evidence_span_end       INT,
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULLS NOT DISTINCT (PG15+): ordinary UNIQUE treats every NULL as
    -- distinct from every other NULL, so without this, two identical
    -- document/event links with unset spans could be inserted repeatedly.
    UNIQUE NULLS NOT DISTINCT (canonical_event_id, document_id, evidence_span_start, evidence_span_end)
);

CREATE INDEX idx_event_document_links_event ON event_document_links (canonical_event_id);
CREATE INDEX idx_event_document_links_document ON event_document_links (document_id);

-- Surprise transform registry (Sections 1, 8): versioned config, never
-- computed by the LLM.
CREATE TABLE surprise_transform_registry (
    registry_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type              TEXT NOT NULL,
    transform_type          TEXT NOT NULL,   -- 'log_ratio', 'pct_point_change', 'robust_scale_change', 'floored_pct_change'
    scale_method            TEXT,
    denominator_floor       NUMERIC,
    parameters              JSONB NOT NULL DEFAULT '{}'::jsonb,
    version                 INT NOT NULL,
    effective_from          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_type, version)
);

CREATE TABLE extracted_events (
    extracted_event_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_version_id        UUID NOT NULL REFERENCES event_versions(event_version_id),

    -- Surprise block (Section 1) — raw values always retained regardless
    -- of which transform later applies.
    surprise_type            TEXT NOT NULL,
    observed_value           NUMERIC,
    reference_value          NUMERIC,
    reference_source         TEXT,           -- company's own prior guidance/disclosure (Phase I-A)
    reference_timestamp      TIMESTAMPTZ,
    unit                     TEXT,
    period                   TEXT,
    surprise_raw             NUMERIC,        -- observed_value - reference_value
    surprise_transformed     NUMERIC,        -- computed deterministically from surprise_transform_registry, not by the LLM
    surprise_transform_registry_id UUID REFERENCES surprise_transform_registry(registry_id),

    extraction_prompt_version TEXT NOT NULL,
    raw_llm_output            JSONB NOT NULL, -- full structured output, for audit

    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_extracted_events_event_version ON extracted_events (event_version_id);

-- ============================================================
-- 2. Entity & instrument master  (Section 2)
-- ============================================================

CREATE TABLE entities (
    entity_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_name              TEXT NOT NULL,
    cik                      TEXT,           -- SEC Central Index Key, where applicable
    entity_status            TEXT NOT NULL DEFAULT 'active',  -- active, delisted, acquired, bankrupt, renamed
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_entities_cik ON entities (cik);

ALTER TABLE catalysts
    ADD CONSTRAINT fk_catalysts_issuer_entity
    FOREIGN KEY (issuer_entity_id) REFERENCES entities(entity_id);

CREATE TABLE instruments (
    instrument_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id                UUID NOT NULL REFERENCES entities(entity_id),
    exchange                 TEXT,
    asset_type                TEXT NOT NULL DEFAULT 'equity',
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_instruments_entity ON instruments (entity_id);

-- Ticker validity windows — the core fix from Section 2: ticker is display
-- metadata with a validity period, never a stable key.
CREATE TABLE instrument_identifiers (
    instrument_identifier_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id             UUID NOT NULL REFERENCES instruments(instrument_id),
    identifier_type            TEXT NOT NULL,   -- 'ticker', 'cusip', 'isin', ...
    identifier_value            TEXT NOT NULL,
    valid_from                  TIMESTAMPTZ NOT NULL,
    valid_to                    TIMESTAMPTZ                 -- NULL = still valid
);

CREATE INDEX idx_instrument_identifiers_lookup
    ON instrument_identifiers (identifier_type, identifier_value, valid_from, valid_to);

CREATE TABLE corporate_actions (
    corporate_action_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id             UUID NOT NULL REFERENCES instruments(instrument_id),
    action_type                TEXT NOT NULL,  -- split, merger, spinoff, delisting, ...
    effective_at                TIMESTAMPTZ NOT NULL,
    adjustment_factor            NUMERIC
);

CREATE INDEX idx_corporate_actions_instrument ON corporate_actions (instrument_id);

-- Multi-party event roles (Section 5): who is what, per event.
CREATE TABLE event_entities (
    event_entity_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_version_id           UUID NOT NULL REFERENCES event_versions(event_version_id),
    entity_id                   UUID NOT NULL REFERENCES entities(entity_id),
    role                         TEXT NOT NULL CHECK (role IN
        ('issuer','subject','supplier','customer','buyer','target','partner','counterparty')),
    UNIQUE (event_version_id, entity_id, role)
);

CREATE INDEX idx_event_entities_event ON event_entities (event_version_id);
CREATE INDEX idx_event_entities_entity ON event_entities (entity_id);

-- ============================================================
-- 3. Causal-link evidence  (Section 3 — three axes + bitemporal history)
-- ============================================================

CREATE TABLE entity_relationships (
    relationship_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id_a                 UUID NOT NULL REFERENCES entities(entity_id),
    entity_id_b                 UUID NOT NULL REFERENCES entities(entity_id),
    relationship_type            TEXT NOT NULL,  -- 'supplier', 'customer', 'competitor', ...

    -- Three independent evidence axes (Section 3) — never conflated.
    source_authority              TEXT NOT NULL CHECK (source_authority IN
        ('government','regulatory_filing','company','licensed_commercial','secondary_inference')),
    -- 'model_inferred' was removed from the allowed values (extraction prompt
    -- v1.1.0): it meant "the LLM is inferring this itself, not reading it
    -- from the text", which directly contradicted the extraction prompt's
    -- own "must point to a supporting span" rule. No real extraction has
    -- run yet (see README), so there is no legacy data using it.
    relationship_evidence          TEXT NOT NULL CHECK (relationship_evidence IN
        ('explicit_named','quantified_named','inferred_structured')),
    shock_transmission_evidence     TEXT NOT NULL CHECK (shock_transmission_evidence IN
        ('historical','economic_model','new_or_unobserved')),

    -- Raw vs. calibrated (Section 3) — never the same field.
    -- NOTE: calibrated_p_transmission does NOT exist here on purpose. v2.2.1
    -- retired the idea of transmission as a calibratable probability in
    -- favor of expected_transmission_effect below (a magnitude, not a
    -- probability) — a calibrated_p_* column for it would be dead weight
    -- nothing could ever correctly populate. (An earlier draft of this
    -- schema left a stale calibrated_p_transmission column in from before
    -- that fix; removed.)
    raw_llm_relationship_score       NUMERIC,
    raw_llm_transmission_score        NUMERIC,
    calibrated_p_relationship          NUMERIC,
    calibration_model_version            TEXT,

    -- Transmission as an effect distribution, not a fake binary probability
    -- (Section 3/4 correction) — logged always, excluded from primary X_i
    -- until produced by a separately frozen, prior-only model (Section 4).
    expected_transmission_effect          NUMERIC,
    transmission_effect_interval_low       NUMERIC,
    transmission_effect_interval_high      NUMERIC,
    transmission_model_version              TEXT,
    transmission_estimated_as_of            TIMESTAMPTZ,
    transmission_training_cutoff            TIMESTAMPTZ,  -- must predate the event being scored

    -- Bitemporal history (Section 3, v2.2.1 fix) — three clocks, not two.
    relationship_valid_from                  TIMESTAMPTZ,   -- when the relationship economically began
    relationship_valid_to                    TIMESTAMPTZ,   -- when it ended, if it has
    evidence_publicly_available_at           TIMESTAMPTZ NOT NULL,  -- when the market could have known
    evidence_public_time_precision            INTERVAL,
    system_observed_at                        TIMESTAMPTZ NOT NULL,  -- when THIS pipeline ingested it
    record_superseded_at                       TIMESTAMPTZ,          -- correction/retraction

    source_document_id                          UUID REFERENCES raw_documents(document_id),
    created_at                                   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (entity_id_a <> entity_id_b)
);

CREATE INDEX idx_entity_relationships_a ON entity_relationships (entity_id_a);
CREATE INDEX idx_entity_relationships_b ON entity_relationships (entity_id_b);
-- The index that makes the two bitemporal query patterns from Section 3 fast:
CREATE INDEX idx_entity_relationships_public_time ON entity_relationships (evidence_publicly_available_at);
CREATE INDEX idx_entity_relationships_observed_time ON entity_relationships (system_observed_at);

-- ============================================================
-- 4. Underreaction estimates  (Section 4)
-- ============================================================

CREATE TABLE underreaction_estimates (
    estimate_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_version_id             UUID NOT NULL REFERENCES event_versions(event_version_id),
    entity_id                     UUID NOT NULL REFERENCES entities(entity_id),
    relationship_id                UUID REFERENCES entity_relationships(relationship_id),  -- NULL for the direct entity itself

    decision_at                     TIMESTAMPTZ NOT NULL,
    horizon_days                     INT NOT NULL,

    -- X_i (Section 4, v2.2.1 — expected_transmission_effect intentionally excluded)
    x_i                               JSONB NOT NULL,
    -- I_t: everything observable at decision time (Section 4)
    i_t                                JSONB NOT NULL,

    expected_car                        NUMERIC NOT NULL,   -- E[CAR(0,H) | X_i, I_t]
    expected_car_interval_low            NUMERIC,
    expected_car_interval_high            NUMERIC,
    observed_car_so_far                   NUMERIC NOT NULL,  -- CAR(0, t)
    underreaction_estimate                 NUMERIC NOT NULL,  -- UR_i(t,H) = expected_car - observed_car_so_far

    scoring_model_version                   TEXT NOT NULL,
    scoring_epoch                            TEXT NOT NULL,   -- frozen evaluation epoch (Section 8)

    created_at                                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_underreaction_estimates_event ON underreaction_estimates (event_version_id);
CREATE INDEX idx_underreaction_estimates_epoch ON underreaction_estimates (scoring_epoch);

-- ============================================================
-- 5. Candidate signals & per-model decisions  (Section 5)
-- ============================================================

-- The pool itself: every candidate considered, model-independent. A
-- candidate is "this entity, as a possibly-tradable security, for this
-- event" — UNIQUE below prevents the same entity from being registered
-- twice for one event (a review round caught this: nothing previously
-- stopped it, and a test fixture had accidentally done exactly that).
CREATE TABLE candidate_signals (
    candidate_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_version_id             UUID NOT NULL REFERENCES event_versions(event_version_id),
    entity_id                     UUID NOT NULL REFERENCES entities(entity_id),

    eligibility_status              TEXT NOT NULL,          -- 'eligible' | 'ineligible'
    eligibility_reason               TEXT,                  -- populated when ineligible
    policy_version                    TEXT NOT NULL,         -- which eligibility rule version applied (Section 3)
    decision_timestamp                 TIMESTAMPTZ NOT NULL,

    created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_version_id, entity_id)
);

CREATE INDEX idx_candidate_signals_event ON candidate_signals (event_version_id);
CREATE INDEX idx_candidate_signals_eligibility ON candidate_signals (eligibility_status);

-- A candidate can be supported by more than one relationship (e.g. an
-- entity that's both a named supplier AND a named competitor) — moved out
-- of candidate_signals into its own join table so that's representable,
-- instead of a single nullable relationship_id forcing a choice of one.
CREATE TABLE candidate_supporting_relationships (
    candidate_id                UUID NOT NULL REFERENCES candidate_signals(candidate_id),
    relationship_id               UUID NOT NULL REFERENCES entity_relationships(relationship_id),
    PRIMARY KEY (candidate_id, relationship_id)
);

-- Per-model decisions on the SAME candidate pool (Section 5, v2.2.1 fix):
-- Arm A and Arm G can rank/select differently from an identical starting set.
CREATE TABLE model_candidate_decisions (
    decision_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id                  UUID NOT NULL REFERENCES candidate_signals(candidate_id),
    model_id                       TEXT NOT NULL,      -- 'arm_a_llm' | 'arm_g_mechanical'
    model_version                   TEXT NOT NULL,
    score                             NUMERIC,
    rank                               INT,
    selected                            BOOLEAN NOT NULL,
    abstained                            BOOLEAN NOT NULL DEFAULT false,
    decision_reason                       TEXT,
    decision_at                            TIMESTAMPTZ NOT NULL,
    UNIQUE (candidate_id, model_id, model_version),
    CHECK (NOT (selected AND abstained))  -- a model can't simultaneously pick and abstain on the same candidate
);

CREATE INDEX idx_model_candidate_decisions_candidate ON model_candidate_decisions (candidate_id);
CREATE INDEX idx_model_candidate_decisions_model ON model_candidate_decisions (model_id);

-- ============================================================
-- 6. Experiments, arms, entries, outcomes  (Sections 5, 7, 8)
-- ============================================================

CREATE TABLE experiments (
    experiment_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                              TEXT NOT NULL,
    scoring_epoch                      TEXT NOT NULL,        -- frozen evaluation epoch this experiment belongs to
    cohort_type                         TEXT NOT NULL CHECK (cohort_type IN ('pilot','confirmatory')),
    -- Pre-registration contract (Section 8) — frozen before the confirmatory
    -- cohort starts; may be null/placeholder for pilot-cohort experiments.
    primary_horizon_days                 INT,
    confidence_level                      NUMERIC,
    alternative                            TEXT,              -- 'one_sided' | 'two_sided'
    inference_method                        TEXT,             -- exactly one, chosen before data collection
    delta                                    NUMERIC,          -- minimum economically meaningful A-G advantage, net of costs
    a_position_rule                           JSONB,
    g_position_rule                            JSONB,
    notional_per_event                          NUMERIC,
    max_positions_per_catalyst                   INT,
    abstention_rule                               TEXT,
    direction_rule                                 TEXT,
    frozen_at                                       TIMESTAMPTZ,  -- when the above was locked; NULL = still pilot/mutable
    created_at                                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE experiment_arms (
    arm_id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id               UUID NOT NULL REFERENCES experiments(experiment_id),
    arm_code                     TEXT NOT NULL CHECK (arm_code IN ('A','B','C','D','E','F','G','H')),
    arm_label                     TEXT NOT NULL,   -- e.g. 'Mechanical ranker on shared candidate graph' for G
    UNIQUE (experiment_id, arm_code)
);

-- A review round caught that this table couldn't actually represent five
-- of the seven arms: candidate_id only exists for Arm A/G (the arms that go
-- through candidate selection at all). Arm B (headline company), C (sector
-- ETF), D (matched placebo), and H (same-security random-time placebo)
-- pick a security by a fixed rule tied to the EVENT, not by candidate
-- selection — with no event_version_id or instrument_id, there was no
-- field anywhere saying which security a Arm-C entry actually bought.
CREATE TABLE arm_entries (
    entry_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    arm_id                        UUID NOT NULL REFERENCES experiment_arms(arm_id),
    event_version_id               UUID NOT NULL REFERENCES event_versions(event_version_id),
    instrument_id                    UUID REFERENCES instruments(instrument_id),  -- NULL only for Arm E (cash-equivalent); enforced below
    candidate_id                       UUID REFERENCES candidate_signals(candidate_id),      -- set for Arm A/G only
    model_decision_id                    UUID REFERENCES model_candidate_decisions(decision_id), -- the specific decision that produced this entry, for A/G
    entry_delay_label              TEXT,            -- 'immediate' | '+30min' | '+2h' | 'next_open' (Arm F)
    entry_timestamp                 TIMESTAMPTZ NOT NULL,
    entry_quote_snapshot_id          UUID,          -- FK added after quote_snapshots exists (below)
    notional                           NUMERIC,
    direction                           TEXT CHECK (direction IN ('long','short')),
    created_at                           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_arm_entries_event ON arm_entries (event_version_id);
CREATE INDEX idx_arm_entries_instrument ON arm_entries (instrument_id);

-- Enforce "securities arms need an instrument, cash arm doesn't" using the
-- arm's code rather than hand-checking every insert -- looked up once here.
CREATE OR REPLACE FUNCTION check_arm_entry_instrument() RETURNS TRIGGER AS $$
DECLARE
    v_arm_code TEXT;
BEGIN
    SELECT arm_code INTO v_arm_code FROM experiment_arms WHERE arm_id = NEW.arm_id;
    IF v_arm_code <> 'E' AND NEW.instrument_id IS NULL THEN
        RAISE EXCEPTION 'arm_entries.instrument_id is required for every arm except E (cash-equivalent); arm_id=% (code=%)', NEW.arm_id, v_arm_code;
    END IF;
    -- A review round's second pass noted the comment above this trigger
    -- ("instrument NULL only for Arm E") was only half-enforced: nothing
    -- stopped the cash arm from ALSO carrying a security. Enforce the
    -- stated invariant exactly, not just its non-E half.
    IF v_arm_code = 'E' AND NEW.instrument_id IS NOT NULL THEN
        RAISE EXCEPTION 'arm_entries.instrument_id must be NULL for arm E (cash-equivalent); arm_id=%', NEW.arm_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_arm_entry_instrument
    BEFORE INSERT OR UPDATE ON arm_entries
    FOR EACH ROW EXECUTE FUNCTION check_arm_entry_instrument();

-- A review round flagged that although candidate_id, model_decision_id,
-- event_version_id, and instrument_id are each individually valid foreign
-- keys, nothing enforced that they're mutually CONSISTENT -- e.g. an
-- arm_entries row could reference a candidate_id belonging to a different
-- event_version_id than the entry's own, or a model_decision_id belonging
-- to a different candidate than the entry's own candidate_id. A second
-- pass of that same review round then found a third case of the same bug:
-- a candidate for one entity paired with a traded instrument_id for a
-- DIFFERENT entity entirely. All three are individually-valid-but-
-- contradictory references that would silently produce plausible-looking,
-- wrong research data rather than an obvious error. The quote/instrument
-- consistency check (entry_quote_snapshot_id and
-- arm_outcomes.exit_quote_snapshot_id) is added further below, once
-- quote_snapshots exists.
CREATE OR REPLACE FUNCTION check_arm_entry_candidate_consistency() RETURNS TRIGGER AS $$
DECLARE
    v_candidate_event_version_id UUID;
    v_candidate_entity_id UUID;
    v_instrument_entity_id UUID;
    v_decision_candidate_id UUID;
BEGIN
    IF NEW.candidate_id IS NOT NULL THEN
        SELECT event_version_id, entity_id INTO v_candidate_event_version_id, v_candidate_entity_id
        FROM candidate_signals WHERE candidate_id = NEW.candidate_id;
        IF v_candidate_event_version_id IS DISTINCT FROM NEW.event_version_id THEN
            RAISE EXCEPTION 'arm_entries.candidate_id % belongs to event_version_id %, not this entry''s event_version_id %',
                NEW.candidate_id, v_candidate_event_version_id, NEW.event_version_id;
        END IF;

        -- A review round's second pass flagged that nothing stopped a
        -- candidate for one company (e.g. NVIDIA) from being paired with a
        -- traded instrument_id for a completely different company (e.g.
        -- Apple) -- both individually-valid FKs, but the entry would then
        -- be "about" two different companies at once. Deliberately compares
        -- entity_id, not instrument_id, so a company with more than one
        -- tradable share class/instrument is still allowed as long as it's
        -- the SAME company the candidate is about.
        IF NEW.instrument_id IS NOT NULL THEN
            SELECT entity_id INTO v_instrument_entity_id FROM instruments WHERE instrument_id = NEW.instrument_id;
            IF v_candidate_entity_id IS DISTINCT FROM v_instrument_entity_id THEN
                RAISE EXCEPTION 'arm_entries.candidate_id % is about entity %, but instrument_id % belongs to a different entity %',
                    NEW.candidate_id, v_candidate_entity_id, NEW.instrument_id, v_instrument_entity_id;
            END IF;
        END IF;
    END IF;

    IF NEW.model_decision_id IS NOT NULL THEN
        SELECT candidate_id INTO v_decision_candidate_id
        FROM model_candidate_decisions WHERE decision_id = NEW.model_decision_id;
        IF NEW.candidate_id IS NULL OR v_decision_candidate_id IS DISTINCT FROM NEW.candidate_id THEN
            RAISE EXCEPTION 'arm_entries.model_decision_id % belongs to candidate_id % (or this entry has no candidate_id at all), not this entry''s candidate_id %',
                NEW.model_decision_id, v_decision_candidate_id, NEW.candidate_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_arm_entry_candidate_consistency
    BEFORE INSERT OR UPDATE ON arm_entries
    FOR EACH ROW EXECUTE FUNCTION check_arm_entry_candidate_consistency();

CREATE TABLE arm_outcomes (
    outcome_id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_id                       UUID NOT NULL REFERENCES arm_entries(entry_id),
    horizon_label                   TEXT NOT NULL,   -- '30min' | '1day' | '5day' | '20day', ... -- one entry can have several outcomes, one per horizon
    exit_timestamp                  TIMESTAMPTZ NOT NULL,
    exit_quote_snapshot_id           UUID,          -- FK added after quote_snapshots exists (below)
    return_gross                      NUMERIC,
    return_net_of_costs                NUMERIC,
    competing_event_flag                BOOLEAN NOT NULL DEFAULT false,   -- Section 8: recorded, never used to drop the row
    competing_event_at                   TIMESTAMPTZ,
    competing_event_type                  TEXT,
    censored_in_sensitivity_analysis       BOOLEAN NOT NULL DEFAULT false,
    created_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entry_id, horizon_label)
);

CREATE INDEX idx_arm_entries_arm ON arm_entries (arm_id);
CREATE INDEX idx_arm_outcomes_entry ON arm_outcomes (entry_id);

-- ============================================================
-- 7. Quotes & market data  (Sections 4, 5, 7)
-- ============================================================

CREATE TABLE quote_snapshots (
    quote_snapshot_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id                   UUID NOT NULL REFERENCES instruments(instrument_id),
    bid                               NUMERIC,
    ask                               NUMERIC,
    bid_size                          NUMERIC,
    ask_size                          NUMERIC,
    mid                               NUMERIC,
    last                              NUMERIC,
    quote_timestamp                   TIMESTAMPTZ NOT NULL,
    data_provider                      TEXT NOT NULL,
    trading_session                    TEXT NOT NULL CHECK (trading_session IN ('regular','pre_market','after_hours')),
    staleness_seconds                   NUMERIC,
    created_at                           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_quote_snapshots_instrument_time ON quote_snapshots (instrument_id, quote_timestamp);

ALTER TABLE arm_entries
    ADD CONSTRAINT fk_arm_entries_entry_quote
    FOREIGN KEY (entry_quote_snapshot_id) REFERENCES quote_snapshots(quote_snapshot_id);

ALTER TABLE arm_outcomes
    ADD CONSTRAINT fk_arm_outcomes_exit_quote
    FOREIGN KEY (exit_quote_snapshot_id) REFERENCES quote_snapshots(quote_snapshot_id);

-- Second half of the arm_entries/arm_outcomes consistency check flagged by
-- a review round (see check_arm_entry_candidate_consistency above): a
-- quote snapshot is itself for one specific instrument, so an entry or
-- exit quote for a DIFFERENT instrument than the one the entry/outcome is
-- actually about would be individually-valid-FK but nonsensical data.
-- Placed here, not next to the other arm_entries trigger, because it needs
-- quote_snapshots to exist first.
CREATE OR REPLACE FUNCTION check_entry_quote_matches_instrument() RETURNS TRIGGER AS $$
DECLARE
    v_quote_instrument_id UUID;
BEGIN
    IF NEW.entry_quote_snapshot_id IS NOT NULL THEN
        SELECT instrument_id INTO v_quote_instrument_id
        FROM quote_snapshots WHERE quote_snapshot_id = NEW.entry_quote_snapshot_id;
        IF v_quote_instrument_id IS DISTINCT FROM NEW.instrument_id THEN
            RAISE EXCEPTION 'arm_entries.entry_quote_snapshot_id % is a quote for instrument %, not this entry''s instrument %',
                NEW.entry_quote_snapshot_id, v_quote_instrument_id, NEW.instrument_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_entry_quote_matches_instrument
    BEFORE INSERT OR UPDATE ON arm_entries
    FOR EACH ROW EXECUTE FUNCTION check_entry_quote_matches_instrument();

CREATE OR REPLACE FUNCTION check_exit_quote_matches_entry_instrument() RETURNS TRIGGER AS $$
DECLARE
    v_entry_instrument_id UUID;
    v_quote_instrument_id UUID;
BEGIN
    IF NEW.exit_quote_snapshot_id IS NOT NULL THEN
        SELECT instrument_id INTO v_entry_instrument_id FROM arm_entries WHERE entry_id = NEW.entry_id;
        SELECT instrument_id INTO v_quote_instrument_id
        FROM quote_snapshots WHERE quote_snapshot_id = NEW.exit_quote_snapshot_id;
        IF v_quote_instrument_id IS DISTINCT FROM v_entry_instrument_id THEN
            RAISE EXCEPTION 'arm_outcomes.exit_quote_snapshot_id % is a quote for instrument %, not entry %''s instrument %',
                NEW.exit_quote_snapshot_id, v_quote_instrument_id, NEW.entry_id, v_entry_instrument_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_exit_quote_matches_entry_instrument
    BEFORE INSERT OR UPDATE ON arm_outcomes
    FOR EACH ROW EXECUTE FUNCTION check_exit_quote_matches_entry_instrument();

-- Point-in-time OHLCV/factor exposures for the specific windows needed —
-- NOT a general tick archive (Section 7: bulk history belongs in
-- Parquet/object storage, not here).
CREATE TABLE market_data (
    market_data_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id                    UUID NOT NULL REFERENCES instruments(instrument_id),
    bar_timestamp                     TIMESTAMPTZ NOT NULL,
    bar_interval                       TEXT NOT NULL,       -- '1min','1day', ...
    open                                NUMERIC,
    high                                NUMERIC,
    low                                 NUMERIC,
    close                               NUMERIC,
    volume                              NUMERIC,
    beta_market                          NUMERIC,           -- factor-model betas used for AR (Section 4)
    beta_sector                          NUMERIC,
    data_provider                        TEXT NOT NULL,
    UNIQUE (instrument_id, bar_timestamp, bar_interval, data_provider)
);

CREATE INDEX idx_market_data_instrument_time ON market_data (instrument_id, bar_timestamp);

-- ============================================================
-- 8. Novelty dedup embeddings  (Section 10)
-- ============================================================
-- pgvector is not installed in this sandbox. In the real deployment:
--   CREATE EXTENSION vector;
--   ALTER TABLE document_embeddings ALTER COLUMN embedding TYPE vector(1536);
-- Placeholder keeps the table structure buildable everywhere.

CREATE TABLE document_embeddings (
    embedding_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id                       UUID NOT NULL REFERENCES raw_documents(document_id),
    embedding                          BYTEA,                -- placeholder; vector(1536) once pgvector is installed
    embedding_model_version              TEXT NOT NULL,
    created_at                            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, embedding_model_version)
);

-- ============================================================
-- 9. Audit log  (Section 11 — makes "append-only" a checkable fact)
-- ============================================================

CREATE TABLE audit_log (
    audit_id                          BIGSERIAL PRIMARY KEY,
    table_name                          TEXT NOT NULL,
    row_pk                                TEXT NOT NULL,
    operation                             TEXT NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    db_role                               TEXT NOT NULL DEFAULT current_user,
    changed_at                             TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_data                               JSONB
);
