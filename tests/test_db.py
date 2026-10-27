# tests/test_db.py
import sqlite3

import pytest

from intelligence_feed.db import connect, migrate

SCHEMA_OBJECTS = {
    "sources",
    "source_overrides",
    "articles",
    "events",
    "event_articles",
    "user_state",
    "update_runs",
    "source_runs",
    "event_search",
}


def test_connect_creates_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_FEED_DATA_DIR", str(tmp_path))
    conn = connect()
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert SCHEMA_OBJECTS <= names
        article_columns = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
        assert "translated_title" in article_columns
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_connect_pragmas_and_row_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_FEED_DATA_DIR", str(tmp_path))
    conn = connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_FEED_DATA_DIR", str(tmp_path))
    conn = connect()
    try:
        migrate(conn)
        migrate(conn)
        names_once = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    conn = connect()
    try:
        migrate(conn)
        names_twice = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert SCHEMA_OBJECTS <= names_once
    assert names_once == names_twice


def test_migrate_rolls_back_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_FEED_DATA_DIR", str(tmp_path))
    conn = connect()
    try:
        # A script whose first statement is valid and second is broken.
        # An atomic migration must leave no partial schema behind.
        monkeypatch.setattr(
            "intelligence_feed.db._SCHEMA",
            "CREATE TABLE partial_table(id INTEGER);\nCREATE TABLE broken_syntax(\n",
        )
        with pytest.raises(sqlite3.Error):
            migrate(conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "partial_table" not in names
        assert not conn.in_transaction
    finally:
        conn.close()


def test_connect_adds_translation_column_to_existing_database(tmp_path, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_FEED_DATA_DIR", str(tmp_path))
    legacy = sqlite3.connect(tmp_path / "intelligence.db")
    legacy.execute(
        """CREATE TABLE articles(
            id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            updated_at TEXT,
            first_seen_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            raw_metadata TEXT
        )"""
    )
    legacy.commit()
    legacy.close()

    conn = connect()
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
        assert "translated_title" in columns
    finally:
        conn.close()
