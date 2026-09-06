-- Migration 006 -- Section 8 Implementation Spec, FINAL
-- (specs/section8-implementation-spec-final.md), Section 7c: the shadow
-- outcome/execution-pricing contract columns on arm_outcomes. Every rule
-- here is frozen after eight rounds of review -- see the spec file.
--
-- Precondition: this migration must fail loudly if arm_outcomes already has
-- any rows (confirmed today: zero execution/outcome-computation code exists
-- anywhere in the repo, so this table has no rows yet in practice -- but the
-- migration itself must assert this, not assume it).
DO $$
DECLARE
    v_count BIGINT;
BEGIN
    SELECT count(*) INTO v_count FROM arm_outcomes;
    IF v_count > 0 THEN
        RAISE EXCEPTION 'migration 006 precondition failed: arm_outcomes has % existing row(s) -- '
            'this migration adds NOT NULL columns with no DEFAULT, so applying it to a non-empty '
            'table would either fail outright or force a backfilled value nobody chose. Resolve '
            'those rows explicitly (or drop/rebuild the table) before applying this migration.',
            v_count;
    END IF;
END $$;

-- No DEFAULT on any of these -- every writer must supply them explicitly, so
-- a forgotten fee or an unproven methodology surfaces as a write-time error,
-- not a silently-wrong 0 or 'nbbo_side_proxy'. entry_fee/exit_fee are
-- non-negative USD amounts. 'actual_fill' is intentionally excluded from
-- both price-source CHECKs -- adding it requires durable fill-record storage
-- (e.g. entry_fill_id/exit_fill_id) that doesn't exist and is out of scope
-- here. No confirmatory_execution_valid column -- validity is derived
-- (assert_outcome_ready_for_confirmation), matching the existing
-- assert_full_coverage pattern of a function, not a cached flag.
ALTER TABLE arm_outcomes
    ADD COLUMN entry_price_source    TEXT NOT NULL CHECK (entry_price_source IN ('nbbo_side_proxy')),
    ADD COLUMN exit_price_source     TEXT NOT NULL CHECK (exit_price_source IN ('nbbo_side_proxy')),
    ADD COLUMN entry_fee             NUMERIC NOT NULL CHECK (entry_fee >= 0),
    ADD COLUMN exit_fee              NUMERIC NOT NULL CHECK (exit_fee >= 0),
    ADD COLUMN return_method_version TEXT NOT NULL,
    ADD COLUMN fee_method_version    TEXT NOT NULL,
    ADD COLUMN exit_reason           TEXT NOT NULL CHECK (exit_reason IN ('horizon', 'protective_stop'));
