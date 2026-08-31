"""
Before constructing Arm A vs. Arm G portfolios for an event, confirm every
eligible candidate actually has a decision from BOTH models at the frozen
model_version being used for that comparison. The schema (shared
candidate_signals pool, per-model rows in model_candidate_decisions) makes
it possible for both arms to see the same candidates -- it does NOT by
itself guarantee both arms actually decided on all of them. A code review
round correctly caught that an earlier version of this project's tests
claimed the schema made incomplete coverage "structurally impossible,"
which was false; this function is the actual completeness check that
claim was missing.
"""

from __future__ import annotations


def find_incomplete_coverage(conn, event_version_id: str, model_ids: list[str], model_version: str) -> list[str]:
    """Returns the candidate_ids (among ELIGIBLE candidates for this event)
    that are missing a decision from at least one of model_ids at
    model_version. An empty list means coverage is complete -- safe to
    build A/G portfolios from this event's candidates."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT candidate_id FROM candidate_signals "
            "WHERE event_version_id = %s AND eligibility_status = 'eligible'",
            (event_version_id,),
        )
        candidate_ids = [row[0] for row in cur.fetchall()]

    incomplete = []
    for candidate_id in candidate_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT model_id FROM model_candidate_decisions "
                "WHERE candidate_id = %s AND model_version = %s",
                (candidate_id, model_version),
            )
            decided_models = {row[0] for row in cur.fetchall()}
        if not set(model_ids).issubset(decided_models):
            incomplete.append(candidate_id)
    return incomplete


def assert_full_coverage(conn, event_version_id: str, model_ids: list[str], model_version: str) -> None:
    incomplete = find_incomplete_coverage(conn, event_version_id, model_ids, model_version)
    if incomplete:
        raise ValueError(
            f"{len(incomplete)} eligible candidate(s) for event {event_version_id} are missing a "
            f"decision from one or more of {model_ids} at model_version={model_version!r}: {incomplete}. "
            "Do not construct Arm A/G portfolios from this event until every eligible candidate has "
            "been decided on by both models -- an incomplete decision set breaks the shared-candidate-"
            "universe comparison this whole design depends on."
        )
