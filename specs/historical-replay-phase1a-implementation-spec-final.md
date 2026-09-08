# Historical Replay — Phase 1A Implementation Spec, FINAL

This document is self-contained and authoritative. It supersedes every earlier draft (v1–v2) and the round 1-6 design-discussion messages that preceded it. Where anything below conflicts with an earlier draft or discussion message, this document wins.

## 1. Scope

This phase gives `entity_relationships.evidence_public_time_precision` a frozen meaning and makes both existing relationship-write paths propagate it correctly from the source document. It does **not** touch how or when historical filings get acquired, and does not compute any new timestamp value — it only propagates a value that (for live/forward ingestion) already exists on `raw_documents` today.

## 2. Semantics (frozen)

For any timestamp/precision pair `(T_canonical, P)`:

```
T_true_public ∈ [T_canonical − P, T_canonical]
```

- `T_canonical` (`canonical_first_public_at` / `evidence_publicly_available_at`) is a **conservative upper bound** on first public availability — never a best-guess midpoint, never a lower bound.
- `P` (`first_public_timestamp_precision` / `evidence_public_time_precision`) is the **non-negative width of a backward-looking uncertainty interval**, in both `forward` and `historical_replay` environments — this is not a historical-only rule. It is explicitly NOT: a symmetric ± window, an extraction/relationship confidence score, or a timestamp-storage-resolution artifact (e.g. "we only store to the minute"). `P = 0` asserts the public time is known exactly; `P IS NULL` means the width is unknown, never that it is zero. A negative `P` is malformed data, full stop — never a legitimate value in either environment; `NULL` is the sanctioned "unknown" case, negative is not "extra unknown," it's wrong.
- A relationship inherits `(T_canonical, P)` as a single unit from the one document it was actually extracted from. Never take `T_canonical` from one source and `P` from another; never aggregate (min/max) across a multi-document filing package (a primary document and its exhibit each have their own provenance if they differ — Phase 1A does not attempt document-vs-exhibit-level granularity beyond "use the row this relationship's `document_id` actually points to").

## 3. Frozen invariants

1. `evidence_publicly_available_at` is a conservative upper bound, never a midpoint estimate.
2. `evidence_public_time_precision` is a non-negative backward-looking width — not symmetric, not confidence, not storage resolution. This holds for `forward` rows exactly as much as `historical_replay` rows: a negative precision is rejected in both environments (§5); only the *nullability* of precision differs by environment (§3.5, §3.11).
3. A relationship inherits the `(canonical, precision)` pair together, from its own source document, never mixed across documents.
4. Each relationship assertion inherits from its own actual evidence-bearing document; no artificial min/max aggregation across a filing package.
5. **Historical relationship-write invariant** (enforced today at the write boundary in `manual_resolve.py`, per §4.2/§5 — this is not yet a statement about a historical document *producer*, since none exists until Phase 1B; Phase 1B's producer must separately satisfy the "source raw_documents row" half of this same invariant when it's built). For a relationship written while operating against a `historical_replay`-purpose database:

   ```text
   source raw_documents row:
       canonical_first_public_at IS NOT NULL
       first_public_timestamp_precision IS NOT NULL
       first_public_timestamp_precision >= INTERVAL '0'

   resulting entity_relationships row:
       evidence_publicly_available_at IS NOT NULL
       evidence_public_time_precision IS NOT NULL
       evidence_public_time_precision >= INTERVAL '0'

   and, not merely non-null but exactly equal:
       evidence_publicly_available_at    == source canonical_first_public_at
       evidence_public_time_precision    == source first_public_timestamp_precision
   ```

   The equality requirement is strictly stronger than independent non-null checks on each side — it also rules out a value being present but wrong (e.g. silently substituted or copied from the wrong document).
6. `NULL` never means zero uncertainty. A NULL precision must never be treated as, defaulted to, or displayed as `INTERVAL '0'`.
7. Manual resolution changes entity identity only, never evidence timing. `evidence_publicly_available_at` / `evidence_public_time_precision` are preserved through resolution exactly as read from the source document; `system_observed_at` reflects the real (present-day) resolution time, and these two clocks are intentionally allowed to diverge — this is not a bug to reconcile.
8. No historical-purpose code path may substitute `system_observed_at` (or any "now") for missing historical public-time provenance. The `canonical_public_at or system_observed_at` / `canonical_first_public_at or resolution_time` fallback pattern remains legitimate for `forward`-purpose rows only, and only for a NULL canonical value — never for a negative precision, which is rejected outright in both environments (§2, §5).
9. Both production relationship-write paths identified in this repo today — `extraction_runner.py` and `manual_resolve.py::_write_backfilled_relationships` — must read `first_public_timestamp_precision` alongside `canonical_first_public_at` from `raw_documents`, and write `evidence_public_time_precision` alongside `evidence_publicly_available_at` into `entity_relationships`, as a single atomic pair. Silently propagating one field without the other is exactly the bug being fixed.
10. A `historical_replay`-purpose relationship write must independently fail closed — refuse to insert the row, raising a distinct exception rather than writing incomplete or invalid provenance — if `canonical_first_public_at IS NULL`, or `first_public_timestamp_precision IS NULL`, or `first_public_timestamp_precision < INTERVAL '0'`. **This is a required, tested behavior of `manual_resolve.py` in Phase 1A**, since it is the only production writer reachable against a `historical_replay`-purpose database today (see §4.2 — `extraction_runner.py` is hardcoded to `assert_database_purpose(conn, "forward")` and cannot reach `historical_replay` at all under the current CLI). Whatever Phase 1B adds or changes for historical extraction must implement this same fail-closed contract at that time; Phase 1A does not add a speculative, currently-unreachable branch to `extraction_runner.py` for it, consistent with not claiming untested behavior as a requirement.
11. `forward`-purpose rows keep their existing observation-time fallback for a NULL canonical value (invariant 8's exception) and are not required to be backfilled or migrated to satisfy invariant 5 — invariant 5 is a `historical_replay`-run invariant only, not a retroactive cleanup of existing `forward` data. A `forward`-purpose row with a negative precision is NOT covered by this fallback exception — negative precision is rejected in `forward` mode too (§2, §5), since NULL (sanctioned unknown) and negative (malformed) are different things.
12. Manual resolution may alter entity identity and set `system_observed_at` to the real resolution time; it may never alter, regenerate, or override the source document's `(canonical_first_public_at, first_public_timestamp_precision)` pair.
13. The shared helper (§5) accepts exactly two `purpose` values, `"forward"` and `"historical_replay"`. Any other value — an unrecognized string, a typo, `None` — is a hard error, never a silent fall-through to the permissive `forward` branch. A future caller passing a malformed purpose must fail loudly, not accidentally inherit the historical-replay-prohibited fallback.

## 4. Call sites and required changes

Repo-wide inventory (grepped, not assumed): exactly two production call sites insert into `entity_relationships`. No third writer exists as of this spec.

### 4.1 `extraction_runner.py`

Current read (around line 1037-1042):

```python
cur.execute(
    "SELECT document_id, raw_content, canonical_first_public_at FROM raw_documents "
    "WHERE document_id::text = ANY(%s)",
    (list(doc_run_ids.keys()),),
)
doc_meta = {str(doc_id): (content, public_at) for doc_id, content, public_at in cur.fetchall()}
```

Current write (around line 1206-1222):

```python
_content, canonical_public_at = doc_meta.get(document_id, (None, None))
public_at = canonical_public_at or system_observed_at
with conn.cursor() as cur:
    cur.execute(
        """
        INSERT INTO entity_relationships
            (entity_id_a, entity_id_b, relationship_type, source_authority,
             relationship_evidence, shock_transmission_evidence,
             raw_llm_relationship_score, evidence_publicly_available_at,
             system_observed_at, source_document_id, extraction_run_id)
        VALUES (%s, %s, %s, %s, %s, 'new_or_unobserved', %s, %s, %s, %s, %s)
        ON CONFLICT (extraction_run_id, entity_id_a, entity_id_b, relationship_type)
        DO NOTHING
        """,
        (entity_id_a, entity_id_b, rel["relationship_type"], rel["source_authority"],
         rel["relationship_evidence"], rel.get("raw_llm_relationship_score"),
         public_at, system_observed_at, document_id, extraction_run_id),
    )
```

Required change: the `SELECT` must also fetch `first_public_timestamp_precision`; `doc_meta` becomes a 3-tuple; the `INSERT` column list gains `evidence_public_time_precision` and the values tuple gains the resolved precision, obtained by calling `public_time_provenance.resolve_relationship_public_time(purpose="forward", ...)` (§5) for each relationship — this call site always passes the literal string `"forward"`, matching its own hardcoded guard; it is not given a way to pass `"historical_replay"` in Phase 1A (§3, invariant 10).

### 4.2 `manual_resolve.py`

Current read (`_write_backfilled_relationships`, around line 70-73):

```python
cur.execute("SELECT raw_content, canonical_first_public_at FROM raw_documents WHERE document_id = %s",
            (mention["document_id"],))
raw_content, canonical_first_public_at = cur.fetchone()
```

Current write (around line 99-117), values tuple line 112:

```python
canonical_first_public_at or resolution_time,
```

Required change: the `SELECT` must also fetch `first_public_timestamp_precision`. Immediately after this read — before entering the `for event in raw_output.get("events", [])` / `for rel in event.get("relationships", [])` loop, not inside it — call `public_time_provenance.resolve_relationship_public_time(...)` exactly once with the `purpose` this invocation was run under, and reuse the returned `(evidence_publicly_available_at, evidence_public_time_precision)` pair for every relationship written from this mention. The source pair is document-level provenance; there is no reason to recompute or re-validate it per relationship, and resolving it once, up front, guarantees malformed provenance (historical OR a negative-precision forward row) is caught before any relationship from this mention is written — not partially written, then failing partway through the loop. The `INSERT` column list gains `evidence_public_time_precision`; the hardcoded `canonical_first_public_at or resolution_time` expression at line 112 is deleted entirely, replaced by the pre-resolved values from the single upfront call.

`purpose` must be threaded down the call stack to reach this point — none of `main()` → `resolve_mention()` / `create_entity_and_resolve()` → `_write_backfilled_relationships()` currently pass it. `main()` already validates `args.purpose` via `db_config.assert_database_purpose(conn, args.purpose)` (line 214) before any of this runs; Phase 1A only needs to carry that already-validated value down, not re-derive or re-check it.

## 5. Shared helper module: `public_time_provenance.py`

A new, neutral module — not placed inside `extraction_runner.py` (which is forward-only today and should not become the place historical semantics are imported from) or `manual_resolve.py` (which needs to import it, not own it):

```text
extraction_runner.py ─┐
                       ├──> public_time_provenance.py
manual_resolve.py ─────┘
```

Contents:

```python
"""Historical Replay Phase 1A: shared, purpose-aware resolution of a
relationship's (evidence_publicly_available_at, evidence_public_time_precision)
pair from its source document's (canonical_first_public_at,
first_public_timestamp_precision). Deliberately its own module, not part of
extraction_runner.py (forward-only orchestrator) or manual_resolve.py (the
dual-purpose caller) -- both import this; neither owns it.

Negative precision is rejected in BOTH purposes -- it is malformed data, not
a legitimate "extra unknown" beyond NULL. Only NULL canonical_first_public_at
(forward) and NULL precision (historical_replay) are purpose-conditional;
negative precision is an unconditional rejection."""

from __future__ import annotations

from datetime import datetime, timedelta


class HistoricalPublicTimeError(ValueError):
    """Raised when a historical_replay-purpose relationship write would
    require incomplete or invalid public-time provenance. Never caught and
    silently worked around -- the caller must fix the source data, or the
    write must not happen."""


def resolve_relationship_public_time(
    *,
    canonical_first_public_at: datetime | None,
    first_public_timestamp_precision: timedelta | None,
    purpose: str,
    fallback_observed_at: datetime,
) -> tuple[datetime, timedelta | None]:
    """Returns (evidence_publicly_available_at, evidence_public_time_precision)
    for a relationship write, given the (canonical, precision) pair read from
    its source raw_documents row.

    purpose="forward": retains the existing observation-time fallback
    (invariant 8's documented exception) -- a NULL canonical_first_public_at
    becomes fallback_observed_at; a NULL precision passes through unchanged.
    A NEGATIVE precision is rejected (raises ValueError) -- NULL is the
    sanctioned "unknown" case, negative is malformed data, and that
    distinction holds in forward mode too.

    purpose="historical_replay": fails closed. Never substitutes
    fallback_observed_at. Raises HistoricalPublicTimeError if
    canonical_first_public_at is NULL, or first_public_timestamp_precision is
    NULL, or precision is negative.

    Any other purpose value raises ValueError -- never silently treated as
    "forward". A malformed or unrecognized purpose must fail loudly, not
    accidentally inherit the historical-replay-prohibited fallback.
    """
    if purpose not in ("forward", "historical_replay"):
        raise ValueError(f"resolve_relationship_public_time: unknown purpose={purpose!r}")

    if first_public_timestamp_precision is not None and first_public_timestamp_precision < timedelta(0):
        if purpose == "historical_replay":
            raise HistoricalPublicTimeError(
                f"historical_replay relationship write with negative precision: "
                f"{first_public_timestamp_precision!r}"
            )
        raise ValueError(
            f"forward relationship write with negative precision: "
            f"{first_public_timestamp_precision!r}"
        )

    if purpose == "historical_replay":
        if canonical_first_public_at is None:
            raise HistoricalPublicTimeError(
                "historical_replay relationship write with NULL canonical_first_public_at"
            )
        if first_public_timestamp_precision is None:
            raise HistoricalPublicTimeError(
                "historical_replay relationship write with NULL first_public_timestamp_precision"
            )
        return canonical_first_public_at, first_public_timestamp_precision

    # purpose == "forward"
    return (
        canonical_first_public_at or fallback_observed_at,
        first_public_timestamp_precision,
    )
```

## 6. Required tests

- `resolve_relationship_public_time` unit tests: forward with non-NULL canonical (passthrough); forward with NULL canonical (fallback fires); forward with NULL precision (passes through as NULL, no error); forward with NEGATIVE precision (raises plain `ValueError`, never propagates a negative value — new); historical_replay with all fields present (passthrough, no error); historical_replay with NULL canonical (raises `HistoricalPublicTimeError`); historical_replay with NULL precision (raises); historical_replay with negative precision (raises `HistoricalPublicTimeError`); unknown purpose value — `"Forward"`, `"historical-replay"`, `""`, `None` — raises plain `ValueError`, never falls through to the forward branch.
- `manual_resolve.py` integration test: resolving a mention whose source document has a non-NULL `(canonical_first_public_at, first_public_timestamp_precision)` produces an `entity_relationships` row with both fields correctly propagated (not just the timestamp), and equal to the source values (invariant 5's equality, not just non-nullness).
- `manual_resolve.py` integration test, `--purpose historical_replay`, precision missing: resolving a mention whose source document has `first_public_timestamp_precision IS NULL` (non-NULL `canonical_first_public_at`) raises `HistoricalPublicTimeError` and writes no row.
- `manual_resolve.py` integration test, `--purpose historical_replay`, canonical missing (the regression test for the fallback-reintroduction hazard): resolving a mention whose source document has `canonical_first_public_at IS NULL` and a valid, non-NULL, non-negative `first_public_timestamp_precision` raises `HistoricalPublicTimeError` and writes no row. This is the test that specifically fails if an implementation reintroduces `canonical_first_public_at or resolution_time` ahead of the helper call — a buggy implementation would silently pass a non-NULL, resolution-time-derived value into the helper, which would then see a non-NULL canonical and a valid precision and pass it through with no error, causing this test (which asserts the error IS raised) to fail. This directly protects invariant 8 at the actual call site, not just inside the helper's own unit tests.
- `extraction_runner.py` integration test: unchanged forward-path behavior — a relationship written today with a non-NULL `raw_documents.first_public_timestamp_precision` now also carries that value into `entity_relationships.evidence_public_time_precision` (regression test that the propagation fix didn't break the already-working forward timestamp path).
- A repo-wide grep-based test (e.g. asserting the count of `INSERT INTO entity_relationships` occurrences in `build/*.py` outside `tests*/` equals exactly 2) is deliberately NOT proposed here — that's a brittle test that breaks on any refactor unrelated to this spec, not a real behavioral guarantee. The inventory in §4 is a one-time verification for this spec's acceptance, not an ongoing invariant to enforce mechanically.

## 7. Non-goals (explicitly deferred to Phase 1B)

- Any SEC timestamp-parsing change (timezone handling of `acceptanceDateTime`, DST behavior, EST-vs-Eastern-local question).
- Any new historical EDGAR acquisition code (the older-filings paginated index, a new orchestrator module).
- The exact historical `canonical_first_public_at` / precision formula for backfilled documents, or any specific numeric precision value.
- Any business-day/trading-calendar decision (NYSE calendar reuse or a separate SEC-operational calendar).
- Extending `extraction_runner.py` (or building whatever replaces/supplements it) to actually run against `historical_replay` databases.
- Building an actual historical document producer that satisfies §3.5's "source raw_documents row" half — Phase 1A only enforces that half at the `manual_resolve.py` write boundary; no code in this phase populates a historical `raw_documents` row in the first place.

None of the above is touched by this spec. Phase 1A only makes the already-empty `evidence_public_time_precision` column get populated correctly wherever a document's own precision value already exists, and makes the one reachable historical write path fail closed instead of silently accepting incomplete or invalid provenance.
