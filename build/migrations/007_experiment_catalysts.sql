-- Migration 007 -- Section 7f Implementation Spec, FINAL
-- (specs/section7f-implementation-spec-final.md), Section 1: explicit
-- catalyst/experiment-epoch membership.
--
-- Nothing below experiments.scoring_epoch currently says which catalysts
-- belong to a given experiment/epoch. candidate_signals and
-- model_candidate_decisions carry no experiment_id at all, and inferring
-- membership from timestamps is fragile (backfilled filings, late
-- ingestion, corrected timestamps can all change which catalysts appear to
-- fall inside a date range). Membership must be its own explicit,
-- auditable fact, decided once, before either arm's decisions -- never
-- duplicated per model.

CREATE TABLE experiment_catalysts (
    experiment_id  UUID NOT NULL REFERENCES experiments(experiment_id),
    scoring_epoch  TEXT NOT NULL,
    catalyst_id    UUID NOT NULL REFERENCES catalysts(catalyst_id),
    admitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id, catalyst_id)
);

CREATE INDEX idx_experiment_catalysts_epoch ON experiment_catalysts (experiment_id, scoring_epoch);

-- scoring_epoch is redundant with experiments.scoring_epoch by design (for a
-- directly-queryable, self-contained audit row) -- enforce it can never
-- silently drift from the experiment's own value.
CREATE OR REPLACE FUNCTION check_experiment_catalyst_epoch_consistency() RETURNS TRIGGER AS $$
DECLARE
    v_experiment_epoch TEXT;
BEGIN
    SELECT scoring_epoch INTO v_experiment_epoch FROM experiments WHERE experiment_id = NEW.experiment_id;
    IF v_experiment_epoch IS DISTINCT FROM NEW.scoring_epoch THEN
        RAISE EXCEPTION 'experiment_catalysts.scoring_epoch % does not match experiments.scoring_epoch % for experiment_id %',
            NEW.scoring_epoch, v_experiment_epoch, NEW.experiment_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_experiment_catalyst_epoch_consistency
    BEFORE INSERT OR UPDATE ON experiment_catalysts
    FOR EACH ROW EXECUTE FUNCTION check_experiment_catalyst_epoch_consistency();

-- Append-only: admission is a one-time, auditable fact. If a catalyst was
-- wrongly admitted, that is itself data (a later row/process records it),
-- never a reason to edit or remove the original admission.
CREATE OR REPLACE FUNCTION forbid_experiment_catalyst_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'experiment_catalysts is append-only -- UPDATE/DELETE are never permitted (attempted on experiment_id=%, catalyst_id=%)',
        COALESCE(OLD.experiment_id, NEW.experiment_id), COALESCE(OLD.catalyst_id, NEW.catalyst_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_forbid_experiment_catalyst_mutation
    BEFORE UPDATE OR DELETE ON experiment_catalysts
    FOR EACH ROW EXECUTE FUNCTION forbid_experiment_catalyst_mutation();
