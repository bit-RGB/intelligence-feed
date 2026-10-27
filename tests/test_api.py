"""Offline REST contracts; mount the router exactly as the host would."""
import asyncio
import logging
from pathlib import Path

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import plugin_api as api
from intelligence_feed import collector, db, fetch as network, repository, service, translation
from intelligence_feed.models import SourceItem


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_FEED_DATA_DIR", str(tmp_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("Real network/collector prohibited")

    # Block the product's network boundary without breaking TestClient/
    # asyncio's Windows loopback socketpair implementation.
    monkeypatch.setattr(network, "urlopen", forbidden)
    monkeypatch.setattr(collector, "run_update", forbidden)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def events():
    conn = db.connect()
    try:
        return [repository.upsert_item(conn, SourceItem(
            "test", "semiconductor", "Test", f"https://example.invalid/{i}",
            f"Chip {i}", "", None,
        ))[0] for i in range(3)]
    finally:
        conn.close()


def test_unprefixed_routes():
    assert {(next(iter(r.methods)), r.path) for r in api.router.routes} == {
        ("GET", "/events"), ("GET", "/counts"), ("GET", "/runs/latest"),
        ("GET", "/sources"), ("PATCH", "/sources/{id}"),
        ("PATCH", "/events/{id}/read"), ("PATCH", "/events/{id}/favorite"),
        ("POST", "/updates"),
    }


def test_source_settings_can_be_listed_and_toggled(client):
    listed = client.get("/sources")
    assert listed.status_code == 200
    items = listed.json()
    assert items
    assert set(items[0]) == {"id", "domain", "name", "enabled"}
    target = next(item for item in items if item["id"] == "ijiwei")
    assert target["enabled"] is True

    changed = client.patch("/sources/ijiwei", json={"value": False})
    assert changed.status_code == 200
    assert changed.json() == {**target, "enabled": False}
    assert next(item for item in client.get("/sources").json() if item["id"] == "ijiwei")["enabled"] is False

    with db.connect() as conn:
        assert conn.execute(
            "SELECT enabled FROM source_overrides WHERE source_id='ijiwei'"
        ).fetchone()["enabled"] == 0
        assert conn.execute(
            "SELECT enabled FROM sources WHERE id='ijiwei'"
        ).fetchone()["enabled"] == 0


def test_source_settings_reject_unknown_id_and_non_boolean(client):
    assert client.patch("/sources/missing", json={"value": False}).status_code == 404
    assert client.patch("/sources/ijiwei", json={"value": "false"}).status_code == 422


def test_pages_counts_and_profile_state(client, events):
    params = {"profile": "alice", "domain": "semiconductor", "limit": 2}
    first = client.get("/events", params=params).json()
    assert set(first) == {"items", "next_cursor"}
    assert [e["id"] for e in first["items"]] == list(reversed(events))[:2]
    second = client.get("/events", params={**params, "cursor": first["next_cursor"]}).json()
    assert [e["id"] for e in second["items"]] == events[:1]
    assert second["next_cursor"] is None
    for field in ("read", "favorite"):
        for value in (True, True, False, False, True):
            response = client.patch(f"/events/{events[0]}/{field}",
                                    json={"profile": "alice", "value": value})
            assert response.status_code == 200
            assert response.json() == {"event_id": events[0], field: value}
    assert client.get("/counts", params={"profile": "alice"}).json() == {
        "all": {"total": 3, "unread": 2},
        "semiconductor": {"total": 3, "unread": 2},
        "ai": {"total": 0, "unread": 0},
        "automotive": {"total": 0, "unread": 0},
        "music": {"total": 0, "unread": 0}, "favorites": 1, "favorites_unread": 0,
        "weibo": {"total": 0, "unread": 0},
    }
    bob_counts = client.get("/counts", params={"profile": "bob"}).json()
    assert bob_counts["favorites"] == 0 and bob_counts["favorites_unread"] == 0
    page = client.get("/events", params={"profile": "alice", "domain": "favorites",
                                         "read": "read", "q": "Chip"}).json()
    assert [e["id"] for e in page["items"]] == events[:1]


@pytest.mark.parametrize("profile", ["", "a" * 65, "../x", "a b", "中文", "a\n"])
def test_invalid_profiles(client, profile):
    for route in ("/events", "/counts"):
        assert client.get(route, params={"profile": profile}).status_code == 422
    for field in ("read", "favorite"):
        assert client.patch(f"/events/1/{field}",
                            json={"profile": profile, "value": True}).status_code == 422


@pytest.mark.parametrize("params", [{"domain": "bad"}, {"read": "bad"},
    {"q": "a" * 101}, {"limit": 0}, {"limit": 101}, {"limit": "abc"}])
def test_query_validation(client, params):
    assert client.get("/events", params={"profile": "a", **params}).status_code == 422


@pytest.mark.parametrize("body", [{}, {"profile": "a"}, {"value": True},
    {"profile": "a", "value": "false"}, {"profile": "a", "value": 1},
    {"profile": "a", "value": None}])
def test_explicit_boolean_required(client, body):
    assert client.patch("/events/1/read", json=body).status_code == 422


def test_boundaries_defaults_and_forwarding(client, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "list_events", lambda **kw: seen.append(kw) or {
        "items": [], "next_cursor": None})
    assert client.get("/events", params={"profile": "a"}).status_code == 200
    assert seen[-1] == dict(profile="a", domain="all", read="all",
                           q="", limit=30, cursor=None)
    assert client.get("/events", params=dict(profile="A_0-" * 16, domain="music",
        read="unread", q="x" * 100, limit=100, cursor="opaque")).status_code == 200
    assert seen[-1]["cursor"] == "opaque"
    assert client.get("/events", params={"profile": "a", "limit": 1}).status_code == 200


def test_missing_event_and_bad_cursor(client):
    assert client.patch("/events/999/read", json={"profile": "a", "value": True}).status_code == 404
    assert client.get("/events", params={"profile": "a", "cursor": "!"}).status_code == 400


@pytest.mark.parametrize("error,status", [(ValueError, 400), (KeyError, 404), (RuntimeError, 500)])
@pytest.mark.parametrize("method,path,name,kwargs", [
    ("get", "/events", "list_events", {"params": {"profile": "a"}}),
    ("get", "/counts", "counts", {"params": {"profile": "a"}}),
    ("get", "/runs/latest", "latest_run", {}),
    ("patch", "/events/1/read", "set_read", {"json": {"profile": "a", "value": True}}),
    ("patch", "/events/1/favorite", "set_favorite", {"json": {"profile": "a", "value": True}}),
    ("post", "/updates", "run_update", {}),
])
def test_safe_error_mapping(client, monkeypatch, caplog, error, status, method, path, name, kwargs):
    secret = "APIKEY-secret SELECT * FROM private C:/private/file ?token=secret"
    def fail(*args, **kw):
        raise error(secret)
    monkeypatch.setattr(service, name, fail)
    with caplog.at_level(logging.ERROR, logger=api.__name__):
        response = getattr(client, method)(path, **kwargs)
    assert response.status_code == status
    assert response.json() == {"detail": {400: "Invalid request", 404: "Not found",
                                         500: "Internal server error"}[status]}
    records = [r for r in caplog.records if r.name == api.__name__]
    if status == 500:
        assert records
    assert all(r.exc_info is None and secret not in r.getMessage() for r in records)


def test_validation_does_not_echo_input(client):
    secret = "C:/private/secret?api_key=hidden SELECT"
    responses = [client.get("/counts", params={"profile": secret}),
                 client.patch("/events/1/read", json={"profile": secret, "value": secret}),
                 client.patch("/events/1/read", content='{"secret":',
                              headers={"Content-Type": "application/json"}),
                 client.patch(f"/events/{secret.split('/')[0]}/read",
                              json={"profile": "a", "value": True})]
    for response in responses:
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid request"}


def test_latest_run_order_and_safe_error(client):
    assert client.get("/runs/latest").json() == {"run": None, "source_runs": []}
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT INTO update_runs(id,started_at,status) VALUES(1,'2026-09-10','success')")
            conn.execute("INSERT INTO update_runs(id,started_at,status) VALUES(2,'2026-09-11','failed')")
            conn.execute("INSERT INTO source_runs(id,run_id,source_id,status,error) VALUES(9,2,'b','failed',?)",
                         ("SELECT C:/secret ?key=secret",))
            conn.execute("INSERT INTO source_runs(id,run_id,source_id,status) VALUES(8,2,'a','success')")
            conn.execute("INSERT INTO source_runs(id,run_id,source_id,status) VALUES(7,1,'old','success')")
    finally:
        conn.close()
    result = client.get("/runs/latest").json()
    assert result["run"]["id"] == 2
    assert [s["id"] for s in result["source_runs"]] == [8, 9]
    assert result["source_runs"][1]["error"] == "Source collection failed"


def test_public_article_url_rejects_userinfo(client):
    conn = db.connect()
    try:
        repository.upsert_item(conn, SourceItem("test", "music", "Test",
            "https://user:secret@example.invalid/news?api_key=secret#secret", "News", "", None))
    finally:
        conn.close()
    page = client.get("/events", params={"profile": "a", "domain": "music"}).json()
    assert page["items"][0]["url"] == ""


def test_public_article_url_keeps_safe_query(client):
    conn = db.connect()
    try:
        repository.upsert_item(conn, SourceItem("test", "music", "Test",
            "https://s.weibo.com/weibo?q=%23topic%23", "Topic", "", None))
    finally:
        conn.close()
    page = client.get("/events", params={"profile": "a", "domain": "music"}).json()
    assert page["items"][0]["url"] == "https://s.weibo.com/weibo?q=%23topic%23"


@pytest.mark.parametrize("url", [
    "https:///local/path",
    "https://example.invalid/api/APIKEY-secret",
    "http://example.invalid/news",
])
def test_public_article_url_rejects_invalid_or_path_credentials(client, url):
    conn = db.connect()
    try:
        repository.upsert_item(conn, SourceItem(
            "test", "music", "Test", url, "Unsafe", "", None
        ))
    finally:
        conn.close()
    page = client.get("/events", params={"profile": "a", "domain": "music"}).json()
    assert page["items"][0]["url"] == ""


def test_public_article_url_rejects_configured_opaque_credential(client, monkeypatch):
    monkeypatch.setenv("TEST_SOURCE_API_KEY", "opaqueValue987")
    monkeypatch.setattr(
        service.sources,
        "load_sources",
        lambda: [{"api_key_env": "TEST_SOURCE_API_KEY"}],
    )
    conn = db.connect()
    try:
        repository.upsert_item(conn, SourceItem(
            "test", "music", "Test",
            "https://example.invalid/releases/opaqueValue987", "Unsafe", "", None,
        ))
    finally:
        conn.close()
    page = client.get("/events", params={"profile": "a", "domain": "music"}).json()
    assert page["items"][0]["url"] == ""


def test_service_closes_connections(monkeypatch):
    opened = []
    original = db.connect
    def tracked():
        conn = original()
        opened.append(conn)
        return conn
    monkeypatch.setattr(db, "connect", tracked)
    service.counts("a")
    service.latest_run()
    service.list_events(profile="a", domain="music", read="all", q="", limit=30, cursor=None)
    for setter in (service.set_read, service.set_favorite):
        with pytest.raises(KeyError):
            setter("a", 999, True)
    assert len(opened) == 5
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_service_delegates_collector(monkeypatch):
    expected = collector.CollectResult(1, "success", 2, 3, 0, 4)
    translated = []
    monkeypatch.setattr(collector, "run_update", lambda: expected)
    monkeypatch.setattr(translation, "translate_pending", lambda conn, limit: translated.append(limit) or 2)
    assert service.run_update() is expected
    assert translated == [100]


def test_service_translation_failure_does_not_fail_collection(monkeypatch, caplog):
    expected = collector.CollectResult(1, "success", 2, 3, 0, 4)
    monkeypatch.setattr(collector, "run_update", lambda: expected)

    def fail(_conn, limit):
        raise RuntimeError("private model path C:/secret")

    monkeypatch.setattr(translation, "translate_pending", fail)
    with caplog.at_level(logging.WARNING, logger=service.__name__):
        assert service.run_update() is expected
    messages = [record.getMessage() for record in caplog.records if record.name == service.__name__]
    assert messages == ["Intelligence feed title translation failed"]
    assert "C:/secret" not in messages[0]


@pytest.mark.parametrize("fails", [False, True])
def test_update_nonblocking_lock_and_release(client, monkeypatch, fails):
    entered, release = threading.Event(), threading.Event()
    calls = []
    expected = collector.CollectResult(1, "success", 2, 3, 0, 4)
    def collect():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        if fails:
            raise RuntimeError("private")
        return expected
    monkeypatch.setattr(service, "run_update", collect)
    original = asyncio.to_thread
    threaded = []
    async def tracked(func, *args, **kwargs):
        threaded.append(threading.get_ident())
        return await original(func, *args, **kwargs)
    monkeypatch.setattr(api.asyncio, "to_thread", tracked)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(client.post, "/updates")
        try:
            assert entered.wait(5)
            assert client.post("/updates").status_code == 409
            assert len(calls) == 1
            assert client.get("/counts", params={"profile": "a"}).status_code == 200
        finally:
            release.set()
        response = first.result(timeout=5)
    assert response.status_code == (500 if fails else 200)
    if not fails:
        assert response.json() == expected.to_dict()
    assert threaded and calls[0] != threaded[0]
    monkeypatch.setattr(service, "run_update", lambda: expected)
    assert client.post("/updates").json() == expected.to_dict()


def test_cancelled_request_keeps_lock_until_worker_finishes(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    expected = collector.CollectResult(1, "success", 0, 0, 0, 0)
    def collect():
        entered.set()
        assert release.wait(5)
        return expected
    monkeypatch.setattr(service, "run_update", collect)

    async def scenario():
        request = asyncio.create_task(api.updates())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            with pytest.raises(api.HTTPException) as error:
                await api.updates()
            assert error.value.status_code == 409
        finally:
            release.set()
            await asyncio.gather(*list(api._updates))
        assert not api._update_lock.locked()
    asyncio.run(scenario())


def test_executor_failure_releases_lock(client, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("Executor unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(api.asyncio, "to_thread", unavailable)
        assert client.post("/updates").status_code == 500
    assert not api._update_lock.locked()


def test_finished_worker_failure_logs_fixed_message(caplog):
    async def scenario():
        async def fail():
            raise RuntimeError("APIKEY-secret C:/private ?token=hidden")
        task = asyncio.create_task(fail())
        api._updates.add(task)
        task.add_done_callback(api._finished)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger=api.__name__):
        asyncio.run(scenario())
    messages = [r.getMessage() for r in caplog.records if r.name == api.__name__]
    assert "Detached intelligence feed update failed" in messages
    assert all("APIKEY-secret" not in message and "C:/private" not in message for message in messages)


def test_dispatch_cancellation_before_worker_start_releases_lock(monkeypatch):
    async def scenario():
        gate = asyncio.Event()

        async def queued(*args, **kwargs):
            await gate.wait()

        monkeypatch.setattr(api.asyncio, "to_thread", queued)
        assert api._update_lock.acquire(blocking=False)
        task = asyncio.create_task(api._dispatch())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not api._update_lock.locked()

    asyncio.run(scenario())


def test_dashboard_api_bootstraps_private_package_path():
    source = (Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    bootstrap = source.index("_PLUGIN_ROOT = Path(__file__).resolve().parents[1]")
    package_import = source.index("from intelligence_feed import service")
    assert bootstrap < package_import
    assert "sys.path.insert(0, str(_PLUGIN_ROOT))" in source
