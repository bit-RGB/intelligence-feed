# tests/test_sources.py
import email.message
import urllib.error
from pathlib import Path

import pytest

from intelligence_feed import fetch as fetch_mod
from intelligence_feed.sources import (
    fetch_source,
    list_source_settings,
    load_sources,
    set_source_enabled,
    sync_sources,
)

F = Path(__file__).parent / "fixtures"

MANIFEST_IDS = {
    "ijiwei", "xcc-semiconductor", "ithome-chip", "elecfans-industry",
    "trendforce", "qbitai", "ithome-ai", "openai-news", "anthropic-news",
    "mistral-news", "nvidia-ai", "github-ai", "techcrunch-ai", "nature-ml",
    "d1ev", "ithome-car",
    "ifeng-music", "sina-music", "china-music", "weibo-hot",
}


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self, n=-1):
        if n is None or n < 0:
            out, self._body = self._body, b""
            return out
        out, self._body = self._body[:n], self._body[n:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_fake_urlopen(monkeypatch, results):
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(fetch_mod, "urlopen", fake)
    return calls


FEED_SOURCE = {"id": "semi", "domain": "semiconductor", "name": "Semi",
               "adapter": "feed", "url": "https://example.com/feed", "enabled": True}
HTML_SOURCE = {"id": "corp", "domain": "semiconductor", "name": "Corp",
               "adapter": "html-list", "url": "https://corp.example/news", "enabled": True}
LASTFM_SOURCE = {"id": "lastfm-top-tracks", "domain": "music", "name": "Last.fm",
                 "adapter": "lastfm",
                 "url": "https://ws.audioscrobbler.com/2.0/?method=chart.gettoptracks&format=json&limit=50",
                 "api_key_env": "LASTFM_API_KEY", "enabled": True}


# --- load_sources ---

def test_load_sources_returns_exact_manifest():
    sources = load_sources()
    assert {s["id"] for s in sources} == MANIFEST_IDS
    assert all(s["adapter"] in {"feed", "html-list", "weibo-hot"} for s in sources)
    assert all(s["url"].startswith("https://") for s in sources)
    assert all("api_key_env" not in s for s in sources)


# --- sync_sources ---

def _seed_source(conn, source_id="semi", adapter="unknown",
                 etag=None, last_modified=None, last_success_at=None):
    conn.execute(
        """INSERT INTO sources(id, domain, name, url, adapter, enabled, etag, last_modified, last_success_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (source_id, "semiconductor", "Placeholder", "https://example.com/articles/1",
         adapter, 1, etag, last_modified, last_success_at),
    )
    conn.commit()


def test_sync_sources_overrides_unknown_placeholder_metadata(conn):
    _seed_source(conn, "semi", adapter="unknown")
    sync_sources(conn, [FEED_SOURCE])
    row = conn.execute("SELECT * FROM sources WHERE id='semi'").fetchone()
    assert row["adapter"] == "feed"
    assert row["name"] == "Semi"
    assert row["url"] == "https://example.com/feed"
    assert row["enabled"] == 1


def test_sync_sources_preserves_etag_last_modified_last_success(conn):
    _seed_source(conn, "semi", etag='"old-etag"', last_modified="Wed, 10 Sep 2025 08:00:00 GMT",
                 last_success_at="2025-09-11T09:00:00+08:00")
    sync_sources(conn, [FEED_SOURCE])
    row = conn.execute("SELECT * FROM sources WHERE id='semi'").fetchone()
    assert row["etag"] == '"old-etag"'
    assert row["last_modified"] == "Wed, 10 Sep 2025 08:00:00 GMT"
    assert row["last_success_at"] == "2025-09-11T09:00:00+08:00"


def test_sync_sources_disables_unlisted_sources(conn):
    _seed_source(conn, "ghost-source")
    sync_sources(conn, [FEED_SOURCE])
    row = conn.execute("SELECT enabled FROM sources WHERE id='ghost-source'").fetchone()
    assert row["enabled"] == 0


def test_sync_sources_is_transaction_neutral(conn):
    _seed_source(conn, "semi")
    conn.execute("BEGIN")
    try:
        sync_sources(conn, [FEED_SOURCE])
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute("SELECT adapter FROM sources WHERE id='semi'").fetchone()
    assert row["adapter"] == "unknown"  # rollback undid the sync


def test_sync_sources_updates_existing_row_without_touching_conditionals(conn):
    _seed_source(conn, "semi", etag='"keep-me"')
    updated = dict(FEED_SOURCE, name="Semi Renamed")
    sync_sources(conn, [updated])
    row = conn.execute("SELECT * FROM sources WHERE id='semi'").fetchone()
    assert row["name"] == "Semi Renamed"
    assert row["etag"] == '"keep-me"'


def test_sync_sources_preserves_user_enabled_override(conn):
    sync_sources(conn, [FEED_SOURCE])
    conn.execute(
        "INSERT INTO source_overrides(source_id, enabled) VALUES(?, ?)",
        ("semi", 0),
    )
    conn.commit()

    sync_sources(conn, [FEED_SOURCE])

    row = conn.execute("SELECT enabled FROM sources WHERE id='semi'").fetchone()
    assert row["enabled"] == 0


def test_source_settings_follow_manifest_order_and_persist_override(conn):
    ai_source = dict(FEED_SOURCE, id="ai", domain="ai", name="AI")
    manifest = [FEED_SOURCE, ai_source]

    assert list_source_settings(conn, manifest) == [
        {"id": "semi", "domain": "semiconductor", "name": "Semi", "enabled": True},
        {"id": "ai", "domain": "ai", "name": "AI", "enabled": True},
    ]

    changed = set_source_enabled(conn, manifest, "semi", False)

    assert changed == {
        "id": "semi", "domain": "semiconductor", "name": "Semi", "enabled": False,
    }
    assert list_source_settings(conn, manifest)[0]["enabled"] is False
    assert conn.execute(
        "SELECT enabled FROM source_overrides WHERE source_id='semi'"
    ).fetchone()["enabled"] == 0
    assert conn.execute(
        "SELECT enabled FROM sources WHERE id='semi'"
    ).fetchone()["enabled"] == 0


def test_set_source_enabled_rejects_unknown_manifest_id(conn):
    with pytest.raises(KeyError):
        set_source_enabled(conn, [FEED_SOURCE], "missing", False)


# --- fetch_source ---

def test_fetch_source_feed_adapter_dispatches_and_returns_etag(monkeypatch):
    calls = make_fake_urlopen(monkeypatch, [
        FakeResponse(200, (F / "rss.xml").read_bytes(),
                     headers={"ETag": '"v2"', "Last-Modified": "Thu, 11 Sep 2025 00:00:00 GMT"}),
    ])
    result = fetch_source(FEED_SOURCE, state={"etag": '"v1"'})
    assert result.status == "success"
    assert result.items[0].url == "https://example.com/a"
    assert result.etag == '"v2"'
    assert result.last_modified == "Thu, 11 Sep 2025 00:00:00 GMT"
    headers = {k.lower(): v for k, v in calls[0].header_items()}
    assert headers["if-none-match"] == '"v1"'


def test_fetch_source_applies_title_filters_before_global_item_limit(monkeypatch):
    make_fake_urlopen(monkeypatch, [FakeResponse(200, (F / "rss.xml").read_bytes())])
    configured = dict(
        FEED_SOURCE,
        include_title_regex="Chiplet|Guid",
        exclude_title_regex="Chiplet",
        max_items=1,
    )
    result = fetch_source(configured)
    assert [item.title for item in result.items] == ["Guid only item"]


def test_fetch_source_html_list_adapter(monkeypatch):
    make_fake_urlopen(monkeypatch, [FakeResponse(200, (F / "company-news.html").read_bytes())])
    result = fetch_source(HTML_SOURCE)
    assert result.status == "success"
    assert [i.url for i in result.items][:2] == [
        "https://corp.example/news/first",
        "https://corp.example/news/second",
    ]


def test_fetch_source_304_returns_not_modified_and_keeps_state(monkeypatch):
    make_fake_urlopen(monkeypatch, [
        urllib.error.HTTPError("https://example.com/feed", 304, "not modified", None, None),
    ])
    result = fetch_source(FEED_SOURCE, state={"etag": '"cached"', "last_modified": "Wed, 10 Sep 2025 08:00:00 GMT"})
    assert result.status == "not_modified"
    assert result.items == []
    assert result.etag == '"cached"'
    assert result.last_modified == "Wed, 10 Sep 2025 08:00:00 GMT"


def test_fetch_source_skips_lastfm_without_key(monkeypatch):
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("network called without credential")

    monkeypatch.setattr(fetch_mod, "urlopen", boom)
    result = fetch_source(LASTFM_SOURCE, state={"etag": '"e"'})
    assert result.status == "skipped_missing_credential"
    assert result.items == []
    assert result.etag is None and result.last_modified is None


def test_fetch_source_appends_api_key_and_parses(monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "secret-key")
    calls = make_fake_urlopen(monkeypatch, [FakeResponse(200, (F / "lastfm.json").read_bytes())])
    result = fetch_source(LASTFM_SOURCE)
    assert result.status == "success"
    assert "api_key=secret-key" in calls[0].full_url
    assert result.items[0].title == "Artist A — Song A"


def test_fetch_source_unknown_adapter_raises_without_network(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("network called for unknown adapter")

    monkeypatch.setattr(fetch_mod, "urlopen", boom)
    with pytest.raises(ValueError):
        fetch_source({"id": "x", "domain": "music", "name": "X",
                      "adapter": "teapot", "url": "https://example.com/x"})


def test_fetch_source_sanitizes_http_error_leaking_api_key(monkeypatch):
    import traceback

    monkeypatch.setenv("LASTFM_API_KEY", "SECRET")

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", None, None)

    monkeypatch.setattr(fetch_mod, "urlopen", fake)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch_source(LASTFM_SOURCE)
    exc = excinfo.value
    assert exc.code == 500
    assert "SECRET" not in str(exc)
    assert "SECRET" not in repr(exc)
    assert "SECRET" not in (getattr(exc, "url", "") or "")
    assert "SECRET" not in (getattr(exc, "filename", "") or "")
    assert "SECRET" not in traceback.format_exc()
