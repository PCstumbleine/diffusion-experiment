import uuid
import psycopg2
import pytest

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
