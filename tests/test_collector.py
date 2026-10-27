import re
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from intelligence_feed.collector import run_update
from intelligence_feed.models import SourceFetchResult, SourceItem
from intelligence_feed.repository import list_events, upsert_item, set_favorite, set_read
from intelligence_feed.sources import sync_sources


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Real network forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def source(id="a", **kwargs):
    return dict(id=id, domain="semiconductor", name=id.upper(),
                url=f"https://example.com/{id}/feed", adapter="feed", **kwargs)


def item(id="a", title="Chiplet production update", published="2026-09-11T01:00:00+00:00"):
    return SourceItem(id, "semiconductor", id.upper(), f"https://example.com/{id}", title, "", published)


def run(sources, fetch, **kwargs):
    return run_update(source_loader=lambda: sources, source_fetcher=fetch,
                      **({"clock": lambda: 0.0} | kwargs))


def test_partial_and_source_atomic_rollback(conn):
    def fetch(s, state):
        assert conn.execute("SELECT status FROM update_runs").fetchone()[0] == "running"
        if s["id"] == "bad":
            return SourceFetchResult("success", [item("bad"), replace(item("broken"), title=None)], "bad-tag")
        return SourceFetchResult("success", [item()], "good-tag")
    result = run([source(), source("bad")], fetch)
    assert result.to_dict() == dict(run_id=result.run_id, status="partial", inserted_count=1,
                                   merged_count=0, error_count=1, started_source_count=2)
    assert conn.execute("SELECT count(*) FROM articles").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert conn.execute("SELECT etag FROM sources WHERE id='bad'").fetchone()[0] is None
    assert conn.execute("SELECT etag FROM sources WHERE id='a'").fetchone()[0] == "good-tag"
    assert [r[0] for r in conn.execute("SELECT status FROM source_runs ORDER BY id")] == ["success", "failed"]
    row = conn.execute("SELECT * FROM update_runs").fetchone()
    assert (row["status"], row["inserted_count"], row["merged_count"], row["error_count"]) == ("partial", 1, 0, 1)
    for value in [row["started_at"], row["finished_at"], conn.execute("SELECT last_success_at FROM sources WHERE id='a'").fetchone()[0]]:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00", value)


@pytest.mark.parametrize("target,hard,elapsed", [(120,150,120), (200,150,150), (120,150,151)])
def test_budget_records_unstarted_and_commits(conn, target, hard, elapsed):
    now = [0.0]
    started = []
    def fetch(s, state):
        started.append(s["id"])
        now[0] = elapsed
        return SourceFetchResult("success", [item()])
    result = run([source(), source("b"), source("c")], fetch, clock=lambda: now[0],
                 target_seconds=target, hard_stop_seconds=hard)
    assert started == ["a"]
    assert result.started_source_count == result.inserted_count == 1
    assert result.status == "success"
    assert [r[0] for r in conn.execute("SELECT status FROM source_runs ORDER BY id")] == ["success", "not_started_budget", "not_started_budget"]


def test_sync_enabled_conditionals_and_skips(conn):
    sync_sources(conn, [source()])
    conn.execute("UPDATE sources SET etag='old',last_modified='modified'")
    conn.commit()
    def fetch(s, state):
        assert s["id"] != "disabled"
        if s["id"] == "a":
            assert state == {"etag": "old", "last_modified": "modified"}
            assert conn.execute("SELECT name FROM sources WHERE id='a'").fetchone()[0] == "Renamed"
            return SourceFetchResult("not_modified", [], "new", "new-modified")
        return SourceFetchResult("skipped_missing_credential", [])
    result = run([source() | {"name": "Renamed"}, source("skip"), source("disabled", enabled=False)], fetch)
    assert result.status == "success" and result.started_source_count == 2
    assert conn.execute("SELECT etag,last_modified FROM sources WHERE id='a'").fetchone()[:] == ("new", "new-modified")
    assert conn.execute("SELECT last_success_at FROM sources WHERE id='skip'").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM source_runs").fetchone()[0] == 2


@pytest.mark.parametrize("successful,status", [(False,"failed"), (True,"partial")])
def test_error_sanitization_and_status(conn, monkeypatch, capsys, successful, status):
    monkeypatch.setenv("TEST_FEED_KEY", "secret/value")
    def fetch(s, state):
        if s["id"] == "a" and successful:
            return SourceFetchResult("not_modified", [])
        raise TimeoutError("secret/value https://example.com/feed?token=hidden&x=1 secret%2Fvalue\n" + "x" * 600)
    result = run([source(), source("bad", api_key_env="TEST_FEED_KEY")], fetch)
    assert result.status == status
    for row in conn.execute("SELECT error FROM source_runs WHERE error IS NOT NULL"):
        assert len(row[0]) <= 500
        assert all(s not in row[0] for s in ["secret", "hidden", "token=", "\n"])
        assert "[REDACTED]" in row[0]
    assert capsys.readouterr() == ("", "")


def test_sanitizer_strips_queries_before_credential_replacement():
    from intelligence_feed.collector import _sanitize

    assert "PRIVATE" not in _sanitize(
        ValueError("https://example.com/feed?token=PRIVATE"), {"https"}
    )
    assert "PRIVATE" not in _sanitize(ValueError("/feed?token=PRIVATE"), set())


@pytest.mark.parametrize("title,domain,date,merged", [
    ("ＣＨＩＰＬＥＴ production, UPDATE!", "semiconductor", "2026-09-14T01:00:00Z", 1),
    ("Chiplet production update", "music", "2026-09-11T01:00:00Z", 0),
    ("Chiplet production update", "semiconductor", "2026-09-14T01:00:01Z", 0),
    ("Unrelated release", "semiconductor", "2026-09-11T01:00:00Z", 0),
    ("Chiplet production update", "semiconductor", None, 0),
])
def test_merge_candidates(conn, title, domain, date, merged):
    run([source()], lambda s, state: SourceFetchResult("success", [item()]))
    result = run([source("b")], lambda s, state: SourceFetchResult("success", [replace(item("b", title, date), domain=domain)]))
    assert result.inserted_count == 1 and result.merged_count == merged
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2 - merged
    assert conn.execute("SELECT count(*) FROM event_search").fetchone()[0] == 2 - merged
    assert not conn.execute("SELECT event_id FROM event_articles GROUP BY event_id HAVING sum(is_primary) != 1").fetchall()


@pytest.mark.parametrize("first,second,merged", [
    ("美光HBM4芯片量产启动", "美光ＨＢＭ４芯片量产启动！", 1),
    ("美光芯片量产启动", "三星芯片停止生产", 0),
    ("a b c d e f g h i", "a b c d e f g h", 1),
    ("a b c d e f g h i", "a b c d e f g", 0),
    ("!!!", "???", 0),
])
def test_title_similarity(conn, first, second, merged):
    result = run([source(), source("b")], lambda s, state: SourceFetchResult("success", [item(s["id"], first if s["id"] == "a" else second)]))
    assert result.merged_count == merged


def test_duplicate_support_and_primary_keep_evidence_fts_and_user_state(conn):
    sources = [source(), source("b")]
    fetch = lambda s, state: SourceFetchResult("success", [item(s["id"])])
    result = run(sources, fetch)
    assert result.merged_count == 1
    eid = conn.execute("SELECT id FROM events").fetchone()[0]
    set_read(conn, "me", eid, True)
    set_favorite(conn, "me", eid, True)
    result = run(sources, fetch)
    assert (result.inserted_count, result.merged_count) == (0, 0)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM event_articles").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM event_search").fetchone()[0] == 1
    assert set(conn.execute("SELECT source_name FROM event_search").fetchone()[0].split()) == {"A", "B"}
    assert conn.execute("SELECT count(*) FROM event_search WHERE event_search MATCH 'B'").fetchone()[0] == 1
    assert all(conn.execute("SELECT read_at,favorite_at FROM user_state").fetchone())
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_merged_cjk_support_source_name_is_searchable(conn):
    sources = [source(), source("b") | {"name": "中文来源"}]
    current = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def fetch(s, state):
        return SourceFetchResult("success", [replace(item(s["id"]), source_name=s["name"], published_at=current)])

    run(sources, fetch)
    rows = list_events(conn, "me", "semiconductor", "all", "中文来源", 50, None)["items"]
    assert len(rows) == 1 and rows[0]["evidence_count"] == 2
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cleanup_uses_instant_and_preserves_old_data_on_failure(conn):
    eid, _ = upsert_item(conn, item())
    set_favorite(conn, "me", eid, True)
    # Earlier UTC instant, but same Beijing calendar date as the boundary.
    cutoff = datetime.now(timezone.utc) - timedelta(days=90, seconds=10)
    conn.execute("UPDATE articles SET fetched_at=?,raw_metadata='old'", (cutoff.astimezone(timezone(timedelta(hours=8))).isoformat(),))
    upsert_item(conn, item("recent", "Unrelated"))
    conn.execute("UPDATE articles SET raw_metadata='recent' WHERE source_id='recent'")
    conn.commit()
    def fail(s, state):
        raise ValueError("parse failed")
    assert run([source()], fail).status == "failed"
    assert [r[0] for r in conn.execute("SELECT raw_metadata FROM articles ORDER BY id")] == [None, "recent"]
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    assert conn.execute("SELECT favorite_at FROM user_state").fetchone()[0]


def test_empty_batch_closes_successfully(conn):
    result = run([], lambda *_: pytest.fail("unexpected fetch"))
    assert result.status == "success" and result.started_source_count == 0
    assert conn.execute("SELECT finished_at FROM update_runs").fetchone()[0]


@pytest.mark.parametrize("target,hard,end", [(120,150,120), (200,150,150), (120,150,151)])
@pytest.mark.parametrize("phase", ["initial", "retry", "redirect"])
def test_production_request_deadlines(conn, monkeypatch, target, hard, end, phase):
    import urllib.error
    from intelligence_feed import fetch as network
    now = [0.0]
    calls = []
    manifest = [source(), source("b")]
    original = [dict(s) for s in manifest]
    ticks = [0]
    def clock():
        ticks[0] += 1
        if phase == "initial" and ticks[0] >= 3:
            now[0] = end
        return now[0]
    def opening(req, timeout=None):
        calls.append(now[0])
        if phase == "redirect":
            handler = network._HTTPSOnlyRedirectHandler()
            req = handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/one")
            calls.append(now[0])
            now[0] = end
            handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/two?secret=PRIVATE")
            calls.append(now[0])
        now[0] = end
        raise urllib.error.HTTPError(req.full_url, 503, "unavailable", None, None)
    monkeypatch.setattr(network, "urlopen", opening)
    result = run_update(source_loader=lambda: manifest, clock=clock,
                        target_seconds=target, hard_stop_seconds=hard)
    assert calls == ([] if phase == "initial" else [0.0, 0.0] if phase == "redirect" else [0.0])
    assert manifest == original
    assert result.error_count == 1
    assert conn.execute("SELECT error FROM source_runs WHERE status='failed'").fetchone()[0] == "Collection request deadline reached"
    assert conn.execute("SELECT status FROM source_runs ORDER BY id DESC").fetchone()[0] == "not_started_budget"


def test_injected_fetcher_receives_original_source_and_state(conn):
    s = source()
    def fetch(received, state):
        assert received is s
        assert received == source()
        assert state == {"etag": None, "last_modified": None}
        return SourceFetchResult("not_modified", [])
    assert run([s], fetch).status == "success"


@pytest.mark.parametrize("url", ["https://example.com/feed", "//example.com/feed", "/feed", "../feed", "feed"])
@pytest.mark.parametrize("credential", ["https", "example", "feed"])
def test_sanitize_strips_url_before_credentials(conn, monkeypatch, url, credential):
    monkeypatch.setenv("REGRESSION_KEY", credential)
    def fetch(s, state):
        raise ValueError(f"failed {url}?token=PRIVATE#FRAGMENT {url}#PRIVATE {credential}")
    run([source(api_key_env="REGRESSION_KEY")], fetch)
    error = conn.execute("SELECT error FROM source_runs").fetchone()[0]
    assert all(secret not in error for secret in ("PRIVATE", "FRAGMENT", "token=", "?", "#"))
    assert "[REDACTED]" in error


def test_all_merged_cjk_sources_searchable(conn):
    import sqlite3
    from intelligence_feed.repository import list_events
    sources = [source() | {"name": "Primary"}, source("b") | {"name": "中文来源"}, source("c") | {"name": "日本情報"}]
    result = run(sources, lambda s, state: SourceFetchResult("success", [replace(item(s["id"]), published_at=datetime.now(timezone.utc).isoformat())]))
    assert result.merged_count == 2
    def search(q):
        return list_events(conn, "default", "semiconductor", "all", q, 50, None)["items"]
    for q in ["中文", "日本", "中文 日本", "Chiplet 中文"]:
        rows = search(q)
        assert len(rows) == 1
        assert rows[0]["evidence_count"] == 3
    for q in ["中文 不存在", "Missing 中文", "中文' OR 1=1 --"]:
        assert search(q) == []
    conn.execute("ALTER TABLE sources RENAME COLUMN name TO broken_name")
    with pytest.raises(sqlite3.OperationalError):
        search("中文")


def test_failure_after_merge_restores_survivor(conn, monkeypatch):
    from intelligence_feed import collector
    run([source()], lambda s, state: SourceFetchResult("success", [item()]))
    eid = conn.execute("SELECT id FROM events").fetchone()[0]
    set_read(conn, "me", eid, True)
    set_favorite(conn, "me", eid, True)
    tables = ["articles", "events", "event_articles", "event_search", "user_state"]
    before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}
    merge = collector._merge_event
    def fail_after_merge(c, event_id):
        assert merge(c, event_id)
        raise ValueError("failure after merge")
    monkeypatch.setattr(collector, "_merge_event", fail_after_merge)
    result = run([source("b")], lambda s, state: SourceFetchResult("success", [item("b")], "bad-tag"))
    assert (result.status, result.inserted_count, result.merged_count, result.error_count) == ("failed", 0, 0, 1)
    assert {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables} == before
    assert conn.execute("SELECT etag,last_success_at FROM sources WHERE id='b'").fetchone()[:] == (None, None)


def test_invalid_fetch_status_is_failed_without_writes(conn):
    result = run([source()], lambda s, state: SourceFetchResult("invalid", [item()], "bad"))
    assert (result.status, result.error_count, result.inserted_count) == ("failed", 1, 0)
    assert conn.execute("SELECT count(*) FROM articles").fetchone()[0] == 0
    assert conn.execute("SELECT error FROM source_runs").fetchone()[0] == "Unexpected source fetch status"
    assert conn.execute("SELECT etag,last_success_at FROM sources").fetchone()[:] == (None, None)
