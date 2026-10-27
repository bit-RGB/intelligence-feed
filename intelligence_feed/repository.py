# intelligence_feed/repository.py
import base64
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import SourceItem

_DOMAINS = ("all", "semiconductor", "ai", "automotive", "music", "weibo", "favorites")
_READ_FILTERS = ("all", "unread", "read")
_LIMIT_MIN, _LIMIT_MAX = 1, 100

# All persisted timestamps are Beijing time `YYYY-MM-DDTHH:MM:SS+08:00`.
# Window, ordering and cursors normalize them in SQL with datetime(...).
_BJ = timezone(timedelta(hours=8))
_EFFECTIVE = "datetime(COALESCE(e.published_at, e.first_seen_at))"

# FTS5 token cleanup: keep only word characters; quotes are handled before.
_FTS_TERM_BAD = re.compile(r"[^\w]+", re.UNICODE)

# CJK runs (kana + CJK unified ext + CJK + compatibility ideographs + hangul)
_CJK_RUN = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+"
)

_SEARCH_COLUMNS = """
    e.id AS id,
    e.domain AS domain,
    e.headline AS headline,
    a.translated_title AS translated_headline,
    e.summary AS summary,
    a.canonical_url AS url,
    s.name AS source_name,
    e.published_at AS published_at,
    e.first_seen_at AS first_seen_at,
    (us.read_at IS NOT NULL) AS read,
    (us.favorite_at IS NOT NULL) AS favorite,
    (SELECT COUNT(*) FROM event_articles ea WHERE ea.event_id = e.id) AS evidence_count,
    {_EFFECTIVE} AS effective
"""


def normalize_url(url: str) -> str:
    """Strip fragment, drop utm_* params, sort the remaining query params."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    query.sort()
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(query),
            "",
        )
    )


def _fingerprint(title: str, canonical_url: str) -> str:
    normalized_title = " ".join(title.split())
    return hashlib.sha256((normalized_title + canonical_url).encode("utf-8")).hexdigest()


def _now_beijing() -> str:
    return datetime.now(_BJ).replace(microsecond=0).isoformat()


def _to_beijing(value) -> str | None:
    """Normalize a parseable timestamp to `YYYY-MM-DDTHH:MM:SS+08:00`.

    Timezone-naive input is interpreted as Beijing time; anything
    unparseable becomes NULL.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_BJ)
    return dt.astimezone(_BJ).replace(microsecond=0).isoformat()


def _split_query(q: str) -> tuple[list[str], list[str]]:
    """Split free text into (fts_terms, like_substrings).

    Quotes become spaces. A chunk containing CJK is kept whole as a LIKE
    substring (so space-free mixed titles like 美光HBM4芯片 stay
    searchable) in addition to its individual CJK runs; the embedded latin
    part of such a chunk must NOT become an FTS term — FTS5 tokenizes the
    whole run as one CJK token, so the latin word would never match.
    Pure-latin chunks are cleaned into word terms (FTS prefix, with a
    parameterized LIKE fallback applied at query time) after stripping all
    FTS syntax characters.
    """
    fts_terms: list[str] = []
    like: list[str] = []
    for chunk in q.replace('"', " ").split():
        if _CJK_RUN.search(chunk):
            like.append(chunk)
            for m in _CJK_RUN.finditer(chunk):
                like.append(m.group())
        else:
            fts_terms.extend(_FTS_TERM_BAD.sub(" ", chunk).split())
    return fts_terms, like


def _encode_cursor(effective: str, event_id: int) -> str:
    payload = json.dumps([effective, event_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        effective, event_id = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        return str(effective), int(event_id)
    except Exception as exc:  # noqa: BLE001 - any malformed cursor is a ValueError
        raise ValueError(f"invalid cursor: {cursor!r}") from exc


@contextmanager
def _maybe_transaction(conn: sqlite3.Connection):
    """Commit only when we opened the transaction ourselves.

    Nested `with conn:` would commit an outer caller's transaction, so
    when the connection is already inside a transaction we execute
    without committing and leave commit/rollback to the caller.
    """
    if conn.in_transaction:
        yield
    else:
        with conn:
            yield


def upsert_item(conn: sqlite3.Connection, item: SourceItem) -> tuple[int, bool]:
    """Insert or update an article; create one event per new URL.

    Returns (event_id, created): created is True when a new article+event
    pair was inserted, False when an existing article/event was updated.
    """
    canonical_url = normalize_url(item.url)
    fingerprint = _fingerprint(item.title, canonical_url)
    published_at = _to_beijing(item.published_at)
    now = _now_beijing()
    with _maybe_transaction(conn):
        # Sources must exist first: articles.source_id references it.
        # Feed metadata (url/name/adapter) is owned by source registration
        # (Task 3); the article path never overwrites an existing source.
        conn.execute(
            """
            INSERT INTO sources(id, domain, name, url, adapter)
            VALUES(?,?,?,?,'unknown')
            ON CONFLICT(id) DO NOTHING
            """,
            (item.source_id, item.domain, item.source_name, canonical_url),
        )
        row = conn.execute(
            "SELECT id FROM articles WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()

        if row is None:
            cur = conn.execute(
                """
                INSERT INTO articles(
                    source_id, canonical_url, title, summary, published_at,
                    updated_at, first_seen_at, fetched_at, fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    item.source_id,
                    canonical_url,
                    item.title,
                    item.summary,
                    published_at,
                    now,
                    now,
                    now,
                    fingerprint,
                ),
            )
            article_id = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO events(domain, headline, summary, primary_article_id, published_at, first_seen_at, updated_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (item.domain, item.title, item.summary, article_id, published_at, now, now),
            )
            event_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO event_articles(event_id, article_id, is_primary) VALUES(?,?,1)",
                (event_id, article_id),
            )
            conn.execute(
                "INSERT INTO event_search(rowid, headline, source_name) VALUES(?,?,?)",
                (event_id, item.title, item.source_name),
            )
            return event_id, True

        article_id = int(row["id"])
        # 微博来源曾由 weibo-hot-search 更名为 weibo-hot；canonical URL 相同会命中
        # 旧 article。把当前热搜迁移到正在抓取的来源，否则旧来源停用后记录会被列表过滤。
        if item.domain == "weibo":
            conn.execute("UPDATE articles SET source_id=? WHERE id=?", (item.source_id, article_id))
        # A new NULL published_at must not erase a known one (M-1).
        conn.execute(
            """
            UPDATE articles
            SET translated_title=CASE WHEN title<>? THEN NULL ELSE translated_title END,
                title=?, summary=?, published_at=COALESCE(?, published_at), updated_at=?, fingerprint=?
            WHERE id=?
            """,
            (item.title, item.title, item.summary, published_at, now, fingerprint, article_id),
        )
        link = conn.execute(
            "SELECT event_id, is_primary FROM event_articles WHERE article_id=? ORDER BY is_primary DESC LIMIT 1",
            (article_id,),
        ).fetchone()
        if link is None:
            cur = conn.execute(
                """
                INSERT INTO events(domain, headline, summary, primary_article_id, published_at, first_seen_at, updated_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (item.domain, item.title, item.summary, article_id, published_at, now, now),
            )
            event_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO event_articles(event_id, article_id, is_primary) VALUES(?,?,1)",
                (event_id, article_id),
            )
        else:
            event_id = int(link["event_id"])
            if not link["is_primary"]:
                rebuild_event_search(conn, event_id)
                return event_id, False
            conn.execute(
                """
                UPDATE events
                SET headline=?, summary=?, published_at=COALESCE(?, published_at), updated_at=?
                WHERE id=?
                """,
                (item.title, item.summary, published_at, now, event_id),
            )
        # Keep the FTS row in sync with the event.
        rebuild_event_search(conn, event_id)
        return event_id, False


def rebuild_event_search(conn: sqlite3.Connection, event_id: int) -> None:
    """Refresh search text using every evidence source, within the caller's transaction."""
    headline = conn.execute("SELECT headline FROM events WHERE id=?", (event_id,)).fetchone()[0]
    names = conn.execute(
        """SELECT DISTINCT s.name FROM event_articles ea
        JOIN articles a ON a.id=ea.article_id JOIN sources s ON s.id=a.source_id
        WHERE ea.event_id=? ORDER BY s.name""", (event_id,)
    ).fetchall()
    conn.execute("DELETE FROM event_search WHERE rowid=?", (event_id,))
    conn.execute("INSERT INTO event_search(rowid,headline,source_name) VALUES(?,?,?)",
                 (event_id, headline, " ".join(row[0] for row in names)))


def list_events(
    conn: sqlite3.Connection,
    profile: str,
    domain: str,
    read: str,
    q: str,
    limit: int,
    cursor: str | None,
) -> dict:
    if domain not in _DOMAINS:
        raise ValueError(f"invalid domain: {domain!r}")
    if read not in _READ_FILTERS:
        raise ValueError(f"invalid read filter: {read!r}")
    if not _LIMIT_MIN <= limit <= _LIMIT_MAX:
        raise ValueError(f"limit must be between {_LIMIT_MIN} and {_LIMIT_MAX}")

    where: list[str] = []
    params: list = []

    if domain == "favorites":
        where.append("us.favorite_at IS NOT NULL")
    elif domain == "all":
        where.append("s.enabled = 1")
        where.append(f"{_EFFECTIVE} >= datetime('now', '-30 days')")
    else:
        where.append("e.domain = ?")
        params.append(domain)
        where.append("s.enabled = 1")
        where.append(f"{_EFFECTIVE} >= datetime('now', '-30 days')")

    if read == "unread":
        where.append("us.read_at IS NULL")
    elif read == "read":
        where.append("us.read_at IS NOT NULL")

    if q.strip():
        fts_terms, like_substrings = _split_query(q)
        conds: list[str] = []
        for term in fts_terms:
            # Terms are \w-only (all FTS syntax stripped during cleaning)
            # and quoted here, so AND/OR/NOT/NEAR can never act as
            # operators; each term also gets a parameterized LIKE fallback
            # so latin model numbers inside space-free CJK titles still
            # hit. AND across multiple query words is preserved.
            like = f"%{term}%"
            conds.append(
                "(e.headline LIKE ? OR a.translated_title LIKE ? OR e.summary LIKE ? OR s.name LIKE ? "
                "OR e.id IN (SELECT rowid FROM event_search WHERE event_search MATCH ?))"
            )
            params.extend([like, like, like, like, f'"{term}"*'])
        for sub in like_substrings:
            conds.append(
                "(e.headline LIKE ? OR a.translated_title LIKE ? OR e.summary LIKE ? OR s.name LIKE ? OR EXISTS ("
                "SELECT 1 FROM event_articles sea "
                "JOIN articles saa ON saa.id=sea.article_id "
                "JOIN sources sas ON sas.id=saa.source_id "
                "WHERE sea.event_id=e.id AND sas.name LIKE ?))"
            )
            like = f"%{sub}%"
            params.extend([like, like, like, like, like])
        if conds:
            where.append("(" + " AND ".join(conds) + ")")

    if cursor is not None and domain != "weibo":
        effective, event_id = _decode_cursor(cursor)
        where.append(f"({_EFFECTIVE} < ? OR ({_EFFECTIVE} = ? AND e.id < ?))")
        params.extend([effective, effective, event_id])

    # 微博每次抓取会用同一个 published_at 刷新当前约 50 条榜单；先按该批次时间，
    # 再把“123.4万”还原为数值按热度降序。无热度条目 CAST 后为 0，自然排在末尾。
    weibo_heat = "(CASE WHEN e.summary LIKE '%万' THEN CAST(REPLACE(e.summary, '万', '') AS REAL) * 10000 ELSE CAST(e.summary AS REAL) END)"
    order = f"{_EFFECTIVE} DESC, {weibo_heat} DESC, e.id ASC" if domain == "weibo" else f"{_EFFECTIVE} DESC, e.id DESC"
    sql = f"""
        SELECT {_SEARCH_COLUMNS.format(_EFFECTIVE=_EFFECTIVE)}
        FROM events e
        JOIN event_articles ea ON ea.event_id = e.id AND ea.is_primary = 1
        JOIN articles a ON a.id = ea.article_id
        JOIN sources s ON s.id = a.source_id
        LEFT JOIN user_state us ON us.event_id = e.id AND us.profile = ?
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT ?
    """
    params.insert(0, profile)
    params.append(limit + 1)
    # No broad OperationalError swallow: input cleaning + quoted FTS terms
    # make FTS syntax errors impossible, so an OperationalError here means
    # a real schema/connection failure and must propagate (review I-2).
    rows = conn.execute(sql, params).fetchall()

    next_cursor = None
    if len(rows) > limit and domain != "weibo":
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode_cursor(last["effective"], last["id"])
    elif len(rows) > limit:
        rows = rows[:limit]

    items = []
    for r in rows:
        d = dict(r)
        d.pop("effective", None)
        d["read"] = bool(d["read"])
        d["favorite"] = bool(d["favorite"])
        items.append(d)
    return {"items": items, "next_cursor": next_cursor}


def _event_exists(conn: sqlite3.Connection, event_id: int) -> None:
    if conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is None:
        raise KeyError(f"event {event_id} does not exist")


def set_read(conn: sqlite3.Connection, profile: str, event_id: int, value: bool) -> dict:
    _event_exists(conn, event_id)
    read_at = _now_beijing() if value else None
    with _maybe_transaction(conn):
        conn.execute(
            """
            INSERT INTO user_state(profile, event_id, read_at)
            VALUES(?,?,?)
            ON CONFLICT(profile, event_id) DO UPDATE SET read_at=excluded.read_at
            """,
            (profile, event_id, read_at),
        )
    return {"event_id": event_id, "read": bool(value)}


def set_favorite(conn: sqlite3.Connection, profile: str, event_id: int, value: bool) -> dict:
    _event_exists(conn, event_id)
    favorite_at = _now_beijing() if value else None
    with _maybe_transaction(conn):
        conn.execute(
            """
            INSERT INTO user_state(profile, event_id, favorite_at)
            VALUES(?,?,?)
            ON CONFLICT(profile, event_id) DO UPDATE SET favorite_at=excluded.favorite_at
            """,
            (profile, event_id, favorite_at),
        )
    return {"event_id": event_id, "favorite": bool(value)}


def counts(conn: sqlite3.Connection, profile: str) -> dict:
    def _domain_counts(domain: str) -> dict:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN us.read_at IS NULL THEN 1 ELSE 0 END) AS unread
            FROM events e
            JOIN event_articles ea ON ea.event_id = e.id AND ea.is_primary = 1
            JOIN articles a ON a.id = ea.article_id
            JOIN sources s ON s.id = a.source_id AND s.enabled = 1
            LEFT JOIN user_state us ON us.event_id = e.id AND us.profile = ?
            WHERE e.domain = ? AND {_EFFECTIVE} >= datetime('now', '-30 days')
            """,
            (profile, domain),
        ).fetchone()
        return {"total": int(row["total"]), "unread": int(row["unread"] or 0)}

    def _domain_counts_all() -> dict:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN us.read_at IS NULL THEN 1 ELSE 0 END) AS unread
            FROM events e
            JOIN event_articles ea ON ea.event_id = e.id AND ea.is_primary = 1
            JOIN articles a ON a.id = ea.article_id
            JOIN sources s ON s.id = a.source_id AND s.enabled = 1
            LEFT JOIN user_state us ON us.event_id = e.id AND us.profile = ?
            WHERE {_EFFECTIVE} >= datetime('now', '-30 days')
            """,
            (profile,),
        ).fetchone()
        return {"total": int(row["total"]), "unread": int(row["unread"] or 0)}

    favorites = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread
        FROM user_state
        WHERE profile = ? AND favorite_at IS NOT NULL
        """,
        (profile,),
    ).fetchone()

    return {
        "all": _domain_counts_all(),
        "semiconductor": _domain_counts("semiconductor"),
        "ai": _domain_counts("ai"),
        "automotive": _domain_counts("automotive"),
        "music": _domain_counts("music"),
        "weibo": _domain_counts("weibo"),
        "favorites": int(favorites["total"]),
        "favorites_unread": int(favorites["unread"] or 0),
    }
