# intelligence_feed/sources.py
"""来源清单加载、sources 表同步、按 adapter 分派的单源采集。"""
import json
import os
import re
import urllib.error
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from .fetch import fetch_url
from .models import SourceFetchResult
from .parsers import parse_feed, parse_html_list, parse_lastfm, parse_weibo_hot
from .repository import _maybe_transaction

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "config" / "sources.json"

STATUS_SUCCESS = "success"
STATUS_NOT_MODIFIED = "not_modified"
STATUS_MISSING_CREDENTIAL = "skipped_missing_credential"

_ADAPTERS = {
    "feed": parse_feed,
    "html-list": parse_html_list,
    "lastfm": parse_lastfm,
    "weibo-hot": parse_weibo_hot,
}


def load_sources(path: str | Path | None = None) -> list[dict]:
    manifest = Path(path) if path else _MANIFEST_PATH
    return json.loads(manifest.read_text(encoding="utf-8"))


def list_source_settings(conn, manifest: list[dict]) -> list[dict]:
    overrides = {
        row["source_id"]: bool(row["enabled"])
        for row in conn.execute("SELECT source_id, enabled FROM source_overrides")
    }
    return [
        {
            "id": source["id"],
            "domain": source["domain"],
            "name": source["name"],
            "enabled": overrides.get(source["id"], bool(source.get("enabled", True))),
        }
        for source in manifest
    ]


def set_source_enabled(
    conn, manifest: list[dict], source_id: str, enabled: bool
) -> dict:
    source = next((item for item in manifest if item["id"] == source_id), None)
    if source is None:
        raise KeyError(source_id)
    with _maybe_transaction(conn):
        sync_sources(conn, manifest)
        conn.execute(
            """INSERT INTO source_overrides(source_id, enabled) VALUES(?, ?)
               ON CONFLICT(source_id) DO UPDATE SET enabled=excluded.enabled""",
            (source_id, 1 if enabled else 0),
        )
        conn.execute(
            "UPDATE sources SET enabled=? WHERE id=?",
            (1 if enabled else 0, source_id),
        )
    return {
        "id": source["id"],
        "domain": source["domain"],
        "name": source["name"],
        "enabled": enabled,
    }


def sync_sources(conn, sources: list[dict]) -> None:
    """以 sources.json 为 sources 表元数据的唯一 owner。

    UPSERT id/domain/name/url/adapter/enabled；保留 etag、last_modified、
    last_success_at（条件请求与运行统计不归本函数管）。清单外的既有
    来源置 enabled=0（不删除：articles 外键依赖）。事务中性：调用方
    已开事务时不代为 commit。
    """
    with _maybe_transaction(conn):
        overrides = {
            row["source_id"]: row["enabled"]
            for row in conn.execute("SELECT source_id, enabled FROM source_overrides")
        }
        for s in sources:
            enabled = overrides.get(s["id"], 1 if s.get("enabled", True) else 0)
            conn.execute(
                """
                INSERT INTO sources(id, domain, name, url, adapter, enabled)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    domain=excluded.domain,
                    name=excluded.name,
                    url=excluded.url,
                    adapter=excluded.adapter,
                    enabled=excluded.enabled
                """,
                (
                    s["id"],
                    s["domain"],
                    s["name"],
                    s["url"],
                    s["adapter"],
                    enabled,
                ),
            )
        if sources:
            placeholders = ",".join("?" for _ in sources)
            conn.execute(
                f"UPDATE sources SET enabled=0 WHERE id NOT IN ({placeholders})",
                [s["id"] for s in sources],
            )


def fetch_source(source: dict, state: dict | None = None) -> SourceFetchResult:
    """抓取并解析单个来源；不写数据库。

    state 可含 etag / last_modified 用于条件请求。api_key_env 存在但
    环境变量为空时返回 skipped_missing_credential，不发起请求。
    """
    state = state or {}
    # 未知 adapter 是纯配置错误，必须在凭据处理与任何网络请求之前拒绝（评审 I2）。
    adapter = source.get("adapter")
    parser = _ADAPTERS.get(adapter)
    if parser is None:
        raise ValueError(f"unknown adapter: {adapter!r}")

    url = source["url"]
    key_env = source.get("api_key_env")
    if key_env:
        key = os.environ.get(key_env, "").strip()
        if not key:
            return SourceFetchResult(
                status=STATUS_MISSING_CREDENTIAL, items=[], etag=None, last_modified=None
            )
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api_key={quote(key, safe='')}"

    try:
        resp = fetch_url(
            url,
            etag=state.get("etag"),
            last_modified=state.get("last_modified"),
            timeout=10,
            request_guard=state.get("_request_guard"),
            extra_headers=source.get("headers"),
        )
    except urllib.error.HTTPError as exc:
        _raise_sanitized_http_error(exc)
    if resp.not_modified:
        return SourceFetchResult(
            status=STATUS_NOT_MODIFIED,
            items=[],
            etag=resp.etag or state.get("etag"),
            last_modified=resp.last_modified or state.get("last_modified"),
        )

    items = parser(resp.body, source)
    include_title_regex = source.get("include_title_regex")
    exclude_title_regex = source.get("exclude_title_regex")
    if include_title_regex:
        items = [item for item in items if re.search(include_title_regex, item.title)]
    if exclude_title_regex:
        items = [item for item in items if re.search(exclude_title_regex, item.title) is None]
    max_items = source.get("max_items")
    if isinstance(max_items, int) and max_items >= 0:
        items = items[:max_items]
    return SourceFetchResult(
        status=STATUS_SUCCESS, items=items, etag=resp.etag, last_modified=resp.last_modified
    )


def _raise_sanitized_http_error(exc: urllib.error.HTTPError) -> None:
    """以去除 query 的 URL 重抛 HTTPError（评审 I3）。

    Last.fm 等来源把 API key 拼在 URL query 里；urllib 的 HTTPError 会把
    完整请求 URL 挂在 .url/.filename 上，原样上抛会让 key 随 Task 4 的
    异常日志泄露。这里重抛一个保留 code/msg、URL 已无 query 的新异常，
    并用 `from None` 切断异常链，traceback 中也不会出现原始 URL。
    """
    raw_url = getattr(exc, "url", "") or ""
    parts = urlsplit(raw_url)
    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    raise urllib.error.HTTPError(clean_url, exc.code, exc.msg, None, None) from None
