"""
Entity resolution -- Extraction-Runner Design v2, Section 3.

Normalizes a raw entity name as extracted from a document (e.g. "NVIDIA
Corporation") and matches it against entities.legal_name / entity_aliases
.normalized_alias. A match returns that entity's entity_id. No match logs
an unresolved_entity_mentions row and returns None -- this module never
creates an entity or writes a relationship on a failed match (see
resolve_entity_name's docstring and the tests in
tests/test_entity_resolution.py for the explicit "no-match never creates
an entity" assertion the design doc's Section 6 calls for).

Ambiguous matches (a normalized name found under more than one entity_id --
possible because entity_aliases.normalized_alias is deliberately NOT
globally unique, only unique per-entity, so the resolver can detect this
case rather than the schema silently forbidding or silently resolving it)
are treated the same as no-match: logged, not guessed at. Silently picking
one of several candidates would be a wrong-entity assignment risk with no
way for a human to know it happened.
"""

from __future__ import annotations

import re
import uuid

import psycopg2.extras

# Ordered longest-first so a multi-word suffix ("l l c") is tried before its
# component tokens could be individually mistaken for something else.
_CORPORATE_SUFFIXES = sorted(
    [
        "incorporated", "corporation", "company", "limited",
        "holdings", "holding", "group", "l l c", "l p",
        "inc", "corp", "co", "ltd", "llc", "plc", "lp",
        "n v", "s a", "ag", "se",
    ],
    key=lambda s: -len(s.split(" ")),
)

_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# SEC company_tickers.json / submissions titles routinely carry a trailing
# state/country-of-incorporation tag in slashes (e.g. "MASCO CORP /DE/",
# "PULTEGROUP INC/MI/") that is SEC filing-index formatting, not part of
# the company's actual legal name -- confirmed mechanically (it's always a
# short all-caps code in slashes at the very end), not inferred from memory.
_SEC_STATE_SUFFIX_RE = re.compile(r"\s*/\s*[A-Z]{2,3}\s*/?\s*$")


def strip_sec_filing_index_noise(raw_title: str) -> str:
    """Removes a trailing SEC filing-index artifact like ' /DE/' or '/MI/'
    from a company_tickers.json / submissions API title. Deliberately does
    NOT attempt to fix name-order oddities some SEC titles also have (e.g.
    "HORTON D R INC" for D.R. Horton) -- that would require judgment this
    function can't verify mechanically; see build/seed_data/watchlist_ciks.csv
    and the seeding script's docstring for that known limitation."""
    return _SEC_STATE_SUFFIX_RE.sub("", raw_title).strip()


def normalize_entity_name(name: str) -> str:
    """lowercase, strip punctuation, strip a trailing corporate suffix
    (possibly more than one, e.g. "Eaton Corp plc" -> "eaton") -- per
    design doc Section 3. Suffixes are only stripped from the END of the
    name, never from the middle, so a company whose actual name happens to
    contain a suffix-like word elsewhere is not mangled."""
    s = name.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    tokens = s.split(" ") if s else []

    changed = True
    while changed and tokens:
        changed = False
        for suffix in _CORPORATE_SUFFIXES:
            suffix_tokens = suffix.split(" ")
            n = len(suffix_tokens)
            if n <= len(tokens) and tokens[-n:] == suffix_tokens:
                tokens = tokens[:-n]
                changed = True
                break
    return " ".join(tokens).strip()


def build_resolution_index(conn) -> dict[str, list[str]]:
    """One dict, built fresh per run: normalized_name -> [entity_id, ...].
    A list of length > 1 means an ambiguous normalized name (see module
    docstring). Rebuilt every call rather than cached -- resolution runs
    are infrequent (per extraction batch, not per document) and this
    project's entity count is in the low hundreds at most, so a fresh
    SELECT is cheap and never risks a stale in-memory index after a manual
    resolution adds a new alias."""
    index: dict[str, list[str]] = {}

    with conn.cursor() as cur:
        cur.execute("SELECT entity_id, legal_name FROM entities")
        for entity_id, legal_name in cur.fetchall():
            key = normalize_entity_name(legal_name)
            if key:
                index.setdefault(key, [])
                if entity_id not in index[key]:
                    index[key].append(entity_id)

    with conn.cursor() as cur:
        cur.execute("SELECT entity_id, normalized_alias FROM entity_aliases")
        for entity_id, normalized_alias in cur.fetchall():
            index.setdefault(normalized_alias, [])
            if entity_id not in index[normalized_alias]:
                index[normalized_alias].append(entity_id)

    return index


def log_unresolved_mention(conn, raw_name: str, normalized_name: str, document_id: str,
                            extraction_run_id: str) -> str:
    mention_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO unresolved_entity_mentions
                (mention_id, raw_name, normalized_name, document_id, extraction_run_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (mention_id, raw_name, normalized_name, document_id, extraction_run_id),
        )
    return mention_id


def resolve_entity_name(conn, raw_name: str, document_id: str, extraction_run_id: str,
                         index: dict[str, list[str]] | None = None) -> str | None:
    """Returns an entity_id on an unambiguous match. On no match OR an
    ambiguous match (>1 entity sharing the normalized name), logs to
    unresolved_entity_mentions and returns None -- this function never
    creates a row in `entities` and never writes to `entity_relationships`
    itself; callers must treat None as "cannot write a relationship
    involving this entity right now"."""
    if index is None:
        index = build_resolution_index(conn)

    normalized = normalize_entity_name(raw_name)
    candidates = index.get(normalized, [])

    if len(candidates) == 1:
        return candidates[0]

    log_unresolved_mention(conn, raw_name, normalized, document_id, extraction_run_id)
    return None


def list_unresolved_mentions(conn, limit: int = 100):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT mention_id, raw_name, normalized_name, document_id,
                   extraction_run_id, first_seen_at
            FROM unresolved_entity_mentions
            WHERE status = 'unresolved'
            ORDER BY first_seen_at
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def count_pending_unresolved_mentions(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM unresolved_entity_mentions WHERE status = 'unresolved'")
        return cur.fetchone()[0]
