import os
import sys
import uuid
import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DB_DSN = "dbname=diffusion_experiment user=postgres"


@pytest.fixture(autouse=True)
def clean_db():
    """Truncate every application table before each test. Rollback-on-
    teardown alone isn't enough isolation here: the code under test
    (ingest_filing) calls conn.commit() itself, same as it will in
    production, so a prior test's data is durably there unless something
    clears it. Runs before the `conn` fixture below (declared as a
    dependency), so every test starts from a genuinely empty database
    regardless of run order or what a previous run left behind."""
    c = psycopg2.connect(DB_DSN)
    with c.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = [row[0] for row in cur.fetchall()]
        if tables:
            cur.execute("TRUNCATE TABLE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
    c.commit()
    c.close()
    yield


@pytest.fixture()
def conn(clean_db):
    """One connection per test. The database itself is guaranteed clean by
    clean_db above; this connection's own transaction is rolled back too,
    for any test that doesn't call commit() itself."""
    c = psycopg2.connect(DB_DSN)
    yield c
    c.rollback()
    c.close()


def make_entity(conn, name="Test Co"):
    eid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entities (entity_id, legal_name) VALUES (%s, %s)",
            (eid, name),
        )
    return eid


def make_raw_document(conn, raw_content="test document body", document_type="8-K"):
    """Minimal raw_documents row for tests that need something to hang an
    extraction_runs/entity_relationships provenance chain off of, without
    caring about the SEC-specific columns."""
    doc_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_documents (document_id, source_name, document_type, raw_content, content_hash) "
            "VALUES (%s, 'test', %s, %s, 'test-hash')",
            (doc_id, document_type, raw_content),
        )
    return doc_id


def make_extraction_run(conn, document_id=None, status="success", raw_llm_output=None,
                         extraction_prompt_version="1.1.0", extractor_model_id="test-model",
                         extractor_model_version="test-version", error=None):
    """A minimal extraction_runs row -- used both by extraction-runner tests
    and by older fixture-style tests (e.g. test_bitemporal.py) that insert
    directly into entity_relationships and need a provenance FK to satisfy
    its NOT NULL constraint (migration 002)."""
    if document_id is None:
        document_id = make_raw_document(conn)
    if status == "success" and raw_llm_output is None:
        raw_llm_output = {}
    if status == "failed" and error is None:
        error = "test failure"
    run_id = str(uuid.uuid4())
    cleaned_llm_output = raw_llm_output if status == "success" else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extraction_runs (
                extraction_run_id, document_id, extraction_prompt_version,
                extractor_model_id, extractor_model_version, status,
                raw_llm_output, cleaned_llm_output, error, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (run_id, document_id, extraction_prompt_version, extractor_model_id,
             extractor_model_version, status,
             psycopg2.extras.Json(raw_llm_output) if raw_llm_output is not None else None,
             psycopg2.extras.Json(cleaned_llm_output) if cleaned_llm_output is not None else None,
             error),
        )
    return run_id


def make_catalyst_with_documents(conn, documents, issuer_entity_id=None, issuer_cik=None):
    """documents: list of (label, role, raw_content) tuples, role in
    ('primary','exhibit'), exactly one 'primary'. Returns
    (catalyst_id, {label: document_id})."""
    doc_ids = {}
    primary_label, _role, primary_content = next(d for d in documents if d[1] == "primary")
    primary_id = make_raw_document(conn, raw_content=primary_content)
    doc_ids[primary_label] = primary_id
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalysts (originating_document_id, issuer_entity_id, issuer_cik) "
            "VALUES (%s, %s, %s) RETURNING catalyst_id",
            (primary_id, issuer_entity_id, issuer_cik),
        )
        catalyst_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO catalyst_documents (catalyst_id, document_id, document_role) VALUES (%s, %s, 'primary')",
            (catalyst_id, primary_id),
        )
    for label, role, raw_content in documents:
        if role == "primary":
            continue
        doc_id = make_raw_document(conn, raw_content=raw_content)
        doc_ids[label] = doc_id
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO catalyst_documents (catalyst_id, document_id, document_role) VALUES (%s, %s, %s)",
                (catalyst_id, doc_id, role),
            )
    return str(catalyst_id), doc_ids
