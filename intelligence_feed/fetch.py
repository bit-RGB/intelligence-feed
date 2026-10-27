# intelligence_feed/fetch.py
"""安全网络层：仅 HTTPS、条件请求、5 MiB 上限、按 brief 的重试规则。"""
import urllib.error
from contextvars import ContextVar
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import FetchResponse

USER_AGENT = "Hermes-Intelligence-Feed/0.1 (+local desktop plugin)"
MAX_BYTES = 5 * 1024 * 1024
_CHUNK = 64 * 1024
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 2  # 初始一次 + 最多重试一次
_ACTIVE_REQUEST_GUARD = ContextVar("intelligence_feed_request_guard", default=None)


class _HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    """每次 3xx 跳转前校验目标 scheme；https→非 https 一律拒绝。

    urllib 默认的 HTTPRedirectHandler 对 301/302/303/307/308 一律跟随，
    不区分目标 scheme，会让「仅 HTTPS」入口校验被静默绕过（评审 I1）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        guard = _ACTIVE_REQUEST_GUARD.get()
        if guard is not None:
            guard()
        if urlsplit(newurl).scheme != "https":
            raise ValueError("refusing non-https redirect target")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 模块级 opener 保持「urlopen」为唯一可 monkeypatch 的网络入口；
# 测试 monkeypatch 本模块的 urlopen 即接管整个网络层。
_opener = build_opener(_HTTPSOnlyRedirectHandler())
urlopen = _opener.open


class ResponseTooLargeError(Exception):
    """响应体超过 5 MiB 读取上限。"""


def _read_capped(resp) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise ResponseTooLargeError(f"response exceeds {MAX_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_url(url: str, etag: str | None = None, last_modified: str | None = None,
              timeout: int = 10, request_guard=None,
              extra_headers: dict | None = None) -> FetchResponse:
    if urlsplit(url).scheme != "https":
        raise ValueError("refusing non-https URL")

    headers = {"User-Agent": USER_AGENT}
    # 来源级请求头（sources.json 的 headers 字段）：微博等接口要求浏览器
    # UA/Referer；仅来自本地可信配置，键值统一转字符串。
    for key, value in (extra_headers or {}).items():
        headers[str(key)] = str(value)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    token = _ACTIVE_REQUEST_GUARD.set(request_guard)
    try:
        for attempt in range(_MAX_ATTEMPTS):
            if request_guard is not None:
                request_guard()
            req = Request(url, headers=headers)
            try:
                with urlopen(req, timeout=timeout) as resp:
                # 二次检查：任何路径（含多层跳转后）拿到的最终 URL 仍必须
                # 是 https，且在读取响应体之前拦截。
                    final_url = resp.geturl() if hasattr(resp, "geturl") else url
                    if urlsplit(final_url).scheme != "https":
                        raise ValueError("refusing non-https response URL")
                    status = int(getattr(resp, "status", None) or 200)
                    if status == 304:
                        return FetchResponse(status=304, body=b"", etag=etag,
                                             last_modified=last_modified, not_modified=True)
                    body = _read_capped(resp)
                    return FetchResponse(
                        status=status,
                        body=body,
                        etag=resp.headers.get("ETag"),
                        last_modified=resp.headers.get("Last-Modified"),
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return FetchResponse(status=304, body=b"", etag=etag,
                                         last_modified=last_modified, not_modified=True)
                if exc.code in RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
                    continue
                raise
        raise RuntimeError("unreachable")  # pragma: no cover
    finally:
        _ACTIVE_REQUEST_GUARD.reset(token)
