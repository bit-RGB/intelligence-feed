# tests/test_fetch.py
import email.message
import traceback
import urllib.error
from pathlib import Path

import pytest

from intelligence_feed import fetch as fetch_mod
from intelligence_feed.fetch import (
    MAX_BYTES,
    USER_AGENT,
    ResponseTooLargeError,
    fetch_url,
)

F = Path(__file__).parent / "fixtures"


class FakeResponse:
    """Minimal stand-in for http.client.HTTPResponse."""

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


def http_error(code):
    return urllib.error.HTTPError("https://example.com/x", code, "err", None, None)


def make_fake_urlopen(monkeypatch, results):
    """Queue of FakeResponse/Exception; records (request, timeout) calls."""
    calls = []

    def fake(req, timeout=None):
        calls.append((req, timeout))
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(fetch_mod, "urlopen", fake)
    return calls


def last_headers(calls):
    req = calls[-1][0]
    return {k.lower(): v for k, v in req.header_items()}


# --- 安全 URL ---

def test_rejects_non_https_without_touching_network(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("network called")
    monkeypatch.setattr(fetch_mod, "urlopen", boom)
    with pytest.raises(ValueError):
        fetch_url("http://example.com/feed")
    with pytest.raises(ValueError):
        fetch_url("ftp://example.com/feed")


def test_sends_fixed_user_agent(monkeypatch):
    calls = make_fake_urlopen(monkeypatch, [FakeResponse(200, b"ok")])
    fetch_url("https://example.com/feed")
    assert last_headers(calls)["user-agent"] == USER_AGENT


def test_sends_conditional_headers_and_passes_timeout(monkeypatch):
    calls = make_fake_urlopen(monkeypatch, [FakeResponse(200, b"ok")])
    fetch_url("https://example.com/feed", etag='"abc"', last_modified="Wed, 10 Sep 2025 08:00:00 GMT")
    h = last_headers(calls)
    assert h["if-none-match"] == '"abc"'
    assert h["if-modified-since"] == "Wed, 10 Sep 2025 08:00:00 GMT"
    assert calls[0][1] == 10  # default timeout


def test_returns_etag_and_last_modified_from_response(monkeypatch):
    make_fake_urlopen(monkeypatch, [
        FakeResponse(200, b"body", headers={"ETag": '"v1"', "Last-Modified": "Thu, 11 Sep 2025 00:00:00 GMT"}),
    ])
    resp = fetch_url("https://example.com/feed")
    assert resp.status == 200
    assert resp.body == b"body"
    assert resp.etag == '"v1"'
    assert resp.last_modified == "Thu, 11 Sep 2025 00:00:00 GMT"
    assert resp.not_modified is False


# --- 304 ---

def test_304_via_http_error_returns_not_modified(monkeypatch):
    make_fake_urlopen(monkeypatch, [http_error(304)])
    resp = fetch_url("https://example.com/feed", etag='"abc"')
    assert resp.not_modified is True
    assert resp.status == 304
    assert resp.body == b""


def test_304_via_status_attribute_returns_not_modified(monkeypatch):
    make_fake_urlopen(monkeypatch, [FakeResponse(status=304)])
    resp = fetch_url("https://example.com/feed")
    assert resp.not_modified is True


# --- 重试 ---

@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_retryable_status_retried_once_then_success(monkeypatch, code):
    calls = make_fake_urlopen(monkeypatch, [http_error(code), FakeResponse(200, b"ok")])
    resp = fetch_url("https://example.com/feed")
    assert resp.status == 200
    assert len(calls) == 2


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_retryable_status_raises_after_second_failure(monkeypatch, code):
    calls = make_fake_urlopen(monkeypatch, [http_error(code), http_error(code)])
    with pytest.raises(urllib.error.HTTPError):
        fetch_url("https://example.com/feed")
    assert len(calls) == 2  # exactly one retry, no more


def test_request_guard_blocks_retry_after_deadline(monkeypatch):
    calls = make_fake_urlopen(monkeypatch, [http_error(503), FakeResponse(200, b"late")])
    checks = iter([None, TimeoutError("collection request deadline reached")])

    def guard():
        outcome = next(checks)
        if outcome:
            raise outcome

    with pytest.raises(TimeoutError, match="deadline"):
        fetch_url("https://example.com/feed", request_guard=guard)
    assert len(calls) == 1


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_other_4xx_not_retried(monkeypatch, code):
    calls = make_fake_urlopen(monkeypatch, [http_error(code)])
    with pytest.raises(urllib.error.HTTPError):
        fetch_url("https://example.com/feed")
    assert len(calls) == 1


# --- 读取上限 ---

def test_rejects_response_over_5mib(monkeypatch):
    make_fake_urlopen(monkeypatch, [FakeResponse(200, b"x" * (MAX_BYTES + 1))])
    with pytest.raises(ResponseTooLargeError):
        fetch_url("https://example.com/feed")


def test_accepts_response_at_5mib_boundary(monkeypatch):
    make_fake_urlopen(monkeypatch, [FakeResponse(200, b"x" * MAX_BYTES)])
    resp = fetch_url("https://example.com/feed")
    assert len(resp.body) == MAX_BYTES


def test_reads_body_in_chunks(monkeypatch):
    # 5 MiB split across many small reads must still hit the cap exactly once
    body = b"x" * (MAX_BYTES + 1)
    calls = []

    class SlowResponse(FakeResponse):
        def read(self, n=-1):
            return super().read(64 * 1024 if n and n > 0 else n)

    def fake(req, timeout=None):
        calls.append(req)
        return SlowResponse(200, body)

    monkeypatch.setattr(fetch_mod, "urlopen", fake)
    with pytest.raises(ResponseTooLargeError):
        fetch_url("https://example.com/feed")


# --- redirect 安全（评审 I1） ---

def test_redirect_handler_rejects_http_target():
    from urllib.request import Request

    from intelligence_feed.fetch import _HTTPSOnlyRedirectHandler
    handler = _HTTPSOnlyRedirectHandler()
    req = Request("https://example.com/feed")
    with pytest.raises(ValueError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/x")


def test_redirect_handler_rejects_relative_target():
    from urllib.request import Request

    from intelligence_feed.fetch import _HTTPSOnlyRedirectHandler
    handler = _HTTPSOnlyRedirectHandler()
    req = Request("https://example.com/feed")
    with pytest.raises(ValueError):
        handler.redirect_request(req, None, 302, "Found", {}, "/rel/path")


def test_redirect_handler_allows_https_target():
    from urllib.request import Request

    from intelligence_feed.fetch import _HTTPSOnlyRedirectHandler
    handler = _HTTPSOnlyRedirectHandler()
    req = Request("https://example.com/feed")
    new = handler.redirect_request(req, None, 302, "Found", {},
                                   "https://example.com/new")
    assert new.full_url == "https://example.com/new"


def test_fetch_url_rejects_non_https_final_url(monkeypatch):
    class RedirectedResponse(FakeResponse):
        def geturl(self):
            return "http://evil.example/x"

    make_fake_urlopen(monkeypatch, [RedirectedResponse(200, b"ok")])
    with pytest.raises(ValueError):
        fetch_url("https://example.com/feed")


def test_redirect_rejection_does_not_leak_query_secret():
    from urllib.request import Request

    from intelligence_feed.fetch import _HTTPSOnlyRedirectHandler

    handler = _HTTPSOnlyRedirectHandler()
    req = Request("https://example.com/feed")
    try:
        handler.redirect_request(
            req, None, 302, "Found", {},
            "http://evil.example/x?api_key=SECRET#fragment",
        )
    except ValueError as exc:
        rendered = "\n".join((str(exc), repr(exc), traceback.format_exc()))
        assert "SECRET" not in rendered
        assert "api_key" not in rendered
    else:  # pragma: no cover
        raise AssertionError("non-https redirect was not rejected")


def test_final_url_rejection_does_not_leak_query_secret(monkeypatch):
    class RedirectedResponse(FakeResponse):
        def geturl(self):
            return "http://evil.example/x?api_key=SECRET#fragment"

    make_fake_urlopen(monkeypatch, [RedirectedResponse(200, b"ok")])
    try:
        fetch_url("https://example.com/feed")
    except ValueError as exc:
        rendered = "\n".join((str(exc), repr(exc), traceback.format_exc()))
        assert "SECRET" not in rendered
        assert "api_key" not in rendered
    else:  # pragma: no cover
        raise AssertionError("non-https final URL was not rejected")
