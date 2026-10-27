# intelligence_feed/db.py
import sqlite3

from .paths import database_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, domain TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL, adapter TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, etag TEXT, last_modified TEXT, last_success_at TEXT);
CREATE TABLE IF NOT EXISTS source_overrides(source_id TEXT PRIMARY KEY REFERENCES sources(id), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)));
CREATE TABLE IF NOT EXISTS articles(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), canonical_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, translated_title TEXT, summary TEXT NOT NULL DEFAULT '', published_at TEXT, updated_at TEXT, first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL, fingerprint TEXT NOT NULL, raw_metadata TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, domain TEXT NOT NULL, headline TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', primary_article_id INTEGER, published_at TEXT, first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_articles(event_id INTEGER NOT NULL REFERENCES events(id), article_id INTEGER NOT NULL REFERENCES articles(id), is_primary INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(event_id,article_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_primary_article_per_event ON event_articles(event_id) WHERE is_primary=1;
CREATE TABLE IF NOT EXISTS user_state(profile TEXT NOT NULL, event_id INTEGER NOT NULL REFERENCES events(id), read_at TEXT, favorite_at TEXT, PRIMARY KEY(profile,event_id));
CREATE TABLE IF NOT EXISTS update_runs(id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, inserted_count INTEGER NOT NULL DEFAULT 0, merged_count INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS source_runs(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES update_runs(id), source_id TEXT NOT NULL, status TEXT NOT NULL, item_count INTEGER NOT NULL DEFAULT 0, error TEXT, elapsed_ms INTEGER NOT NULL DEFAULT 0);
CREATE VIRTUAL TABLE IF NOT EXISTS event_search USING fts5(headline, source_name);
"""


def migrate(conn: sqlite3.Connection) -> None:
    # `executescript` implicitly commits any pending transaction and runs
    # statements one-by-one in autocommit, so it cannot provide atomicity.
    # Note also that `with conn:` alone is NOT enough: in Python's legacy
    # transaction handling (<=3.11 default), DDL does not open an implicit
    # transaction, so each CREATE would autocommit individually. An explicit
    # BEGIN is required to make the whole schema transactional; on failure
    # `with conn:` rolls everything back and no partial DDL survives.
    statements = [s.strip() for s in _SCHEMA.split(";") if s.strip()]
    def apply() -> None:
        for statement in statements:
            conn.execute(statement)
        article_columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
        if "translated_title" not in article_columns:
            conn.execute("ALTER TABLE articles ADD COLUMN translated_title TEXT")

    if conn.in_transaction:
        # Caller already manages a transaction; apply statements within it
        # so the caller controls atomicity and commit/rollback.
        apply()
        return
    conn.execute("BEGIN")
    with conn:
        apply()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(database_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    with conn:
        migrate(conn)
    return conn
