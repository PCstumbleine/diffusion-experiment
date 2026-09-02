-- Migration 004 -- range-valued guidance fields (extraction_prompt_v1.md
-- bumped to v1.2.0 alongside this). Found in Dry Run 001 (real EDGAR
-- data): most real guidance is stated as a RANGE ("$85.0 billion to
-- $87.0 billion"), which extracted_events' existing single-number
-- observed_value/reference_value fields cannot represent without either
-- fabricating a derived number (a midpoint) or dropping real, stated
-- figures. Adds explicit low/high fields instead -- deliberately NOT a
-- computed midpoint anywhere, and surprise_transform.py is deliberately
-- untouched (a range-aware transform is a separate design decision for
-- later; surprise_transformed simply stays NULL for a range-valued event,
-- same as it already does today for any event with a null observed_value).

ALTER TABLE extracted_events ADD COLUMN observed_value_low NUMERIC;
ALTER TABLE extracted_events ADD COLUMN observed_value_high NUMERIC;
ALTER TABLE extracted_events ADD COLUMN reference_value_low NUMERIC;
ALTER TABLE extracted_events ADD COLUMN reference_value_high NUMERIC;

-- Reject a range where low > high rather than silently accepting
-- nonsense -- NULLs on either side are fine (an open-ended or
-- single-sided range, or simply "not a range").
ALTER TABLE extracted_events
    ADD CONSTRAINT extracted_events_observed_range_order
    CHECK (observed_value_low IS NULL OR observed_value_high IS NULL
           OR observed_value_low <= observed_value_high);

ALTER TABLE extracted_events
    ADD CONSTRAINT extracted_events_reference_range_order
    CHECK (reference_value_low IS NULL OR reference_value_high IS NULL
           OR reference_value_low <= reference_value_high);
