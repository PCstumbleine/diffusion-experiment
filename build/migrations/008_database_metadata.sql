-- Migration 008 -- Historical Replay Phase 0
-- (specs/historical-replay-phase0-implementation-spec-final.md), Section 2:
-- database_metadata, a shared, migration-created singleton table
-- identifying which purpose (forward vs. historical_replay) this specific
-- database is allowed to be used for. Applied identically to every
-- database; only the one row's value (inserted separately, in the same
-- transaction as this migration -- see bootstrap_database.py's atomic
-- "008" sequence) differs.

CREATE TABLE database_metadata (
    singleton_key     TEXT PRIMARY KEY DEFAULT 'singleton',
    database_purpose  TEXT NOT NULL CHECK (database_purpose IN ('forward', 'historical_replay')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT database_metadata_is_singleton CHECK (singleton_key = 'singleton')
);
