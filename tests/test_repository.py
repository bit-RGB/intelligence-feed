# tests/test_repository.py
import base64
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from intelligence_feed.db import connect
from intelligence_feed.models import SourceItem
from intelligence_feed.repository import (
    counts,
    list_events,
    set_favorite,
    set_read,
    upsert_item,
)

_BJ = timezone(timedelta(hours=8))
_BEIJING_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_FEED_DATA_DIR", str(tmp_path))
    c = connect()
    yield c
    c.close()


def item(url, age_days=0, title="HBM capacity expands", domain="semiconductor",
         source_name="Semi", published_at=None):
    if published_at is None:
        published_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return SourceItem("semi", domain, source_name, url, title, "summary", published_at)


# --- brief 自带测试 ---

def test_recent_hidden_but_favorite_history_visible(conn):
    event_id, _ = upsert_item(conn, item("https://example.com/old", 31))
    assert list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"] == []
    set_favorite(conn, "default", event_id, True)
    rows = list_events(conn, "default", "favorites", "all", "", 50, None)["items"]
    assert [r["id"] for r in rows] == [event_id]


def test_disabled_source_is_hidden_from_domain_but_retained_in_favorites(conn):
    event_id, _ = upsert_item(conn, item("https://example.com/disabled"))
    set_favorite(conn, "default", event_id, True)
    conn.execute("UPDATE sources SET enabled=0 WHERE id='semi'")
    conn.commit()

    assert list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"] == []
    assert counts(conn, "default")["semiconductor"] == {"total": 0, "unread": 0}
    rows = list_events(conn, "default", "favorites", "all", "", 50, None)["items"]
    assert [row["id"] for row in rows] == [event_id]


def test_read_and_search_compose(conn):
    event_id, _ = upsert_item(conn, item("https://example.com/hbm"))
    assert len(list_events(conn, "default", "semiconductor", "unread", "HBM", 50, None)["items"]) == 1
    set_read(conn, "default", event_id, True)
    assert list_events(conn, "default", "semiconductor", "unread", "HBM", 50, None)["items"] == []


def test_all_domain_lists_every_enabled_domain(conn):
    upsert_item(conn, item("https://example.com/semi"))
    upsert_item(conn, item("https://example.com/song", domain="music"))
    rows = list_events(conn, "default", "all", "all", "", 50, None)["items"]
    assert len(rows) == 2
    c = counts(conn, "default")
    assert c["all"]["total"] == 2
    assert c["semiconductor"]["total"] == 1
    assert c["music"]["total"] == 1


def test_weibo_domain_shows_current_snapshot_sorted_by_heat(conn):
    now = datetime.now(_BJ).replace(microsecond=0)
    old = (now - timedelta(days=1)).isoformat()
    current = now.isoformat()
    entries = [
        SourceItem("weibo", "weibo", "微博热搜", "https://s.weibo.com/weibo?q=a", "词条A", "10.0万", old),
        SourceItem("weibo", "weibo", "微博热搜", "https://s.weibo.com/weibo?q=b", "词条B", "20.0万", old),
        SourceItem("weibo", "weibo", "微博热搜", "https://s.weibo.com/weibo?q=stale", "已下榜", "99.0万", old),
        SourceItem("weibo", "weibo", "微博热搜", "https://s.weibo.com/weibo?q=a", "词条A", "40.0万", current),
        SourceItem("weibo", "weibo", "微博热搜", "https://s.weibo.com/weibo?q=b", "词条B", "50.0万", current),
    ]
    for entry in entries:
        upsert_item(conn, entry)

    rows = list_events(conn, "default", "weibo", "all", "", 2, None)["items"]
    assert [(r["headline"], r["summary"]) for r in rows] == [
        ("词条B", "50.0万"),
        ("词条A", "40.0万"),
    ]


def test_weibo_item_moves_from_disabled_legacy_source_to_current_source(conn):
    url = "https://s.weibo.com/weibo?q=legacy"
    upsert_item(conn, SourceItem("weibo-hot-search", "weibo", "微博热搜", url, "旧词条", "10.0万", None))
    conn.execute("UPDATE sources SET enabled=0 WHERE id='weibo-hot-search'")
    upsert_item(conn, SourceItem("weibo-hot", "weibo", "微博热搜", url, "当前词条", "20.0万", datetime.now(_BJ).isoformat()))

    rows = list_events(conn, "default", "weibo", "all", "", 50, None)["items"]
    assert [(r["headline"], r["summary"]) for r in rows] == [("当前词条", "20.0万")]
    source_id = conn.execute("SELECT source_id FROM articles WHERE canonical_url=?", (url,)).fetchone()[0]
    assert source_id == "weibo-hot"


# --- URL 规范化与去重 ---

def test_url_normalization_strips_fragment_utm_and_sorts_query(conn):
    e1, created1 = upsert_item(conn, item("https://Example.com/a?b=2&utm_source=x&a=1#frag"))
    e2, created2 = upsert_item(conn, item("https://example.com/a?a=1&b=2"))
    assert created1 is True
    assert created2 is False
    assert e1 == e2
    assert counts(conn, "default")["semiconductor"]["total"] == 1


def test_duplicate_url_updates_article_without_new_event(conn):
    e1, _ = upsert_item(conn, item("https://example.com/x", title="Old headline"))
    e2, created = upsert_item(conn, item("https://example.com/x", title="New headline"))
    assert created is False
    assert e1 == e2
    rows = list_events(conn, "default", "semiconductor", "all", "New", 50, None)["items"]
    assert [r["headline"] for r in rows] == ["New headline"]
    rows = list_events(conn, "default", "semiconductor", "all", "Old", 50, None)["items"]
    assert rows == []


def test_list_events_returns_and_searches_translated_headline(conn):
    event_id, _ = upsert_item(
        conn,
        item("https://example.com/translated", title="AI agents improve coding"),
    )
    conn.execute(
        "UPDATE articles SET translated_title=? WHERE canonical_url=?",
        ("AI 智能体提升编程效率", "https://example.com/translated"),
    )
    conn.commit()

    rows = list_events(conn, "default", "semiconductor", "all", "智能体", 50, None)["items"]
    assert [(row["id"], row["headline"], row["translated_headline"]) for row in rows] == [
        (event_id, "AI agents improve coding", "AI 智能体提升编程效率"),
    ]


def test_upsert_clears_translation_only_when_original_title_changes(conn):
    url = "https://example.com/title-refresh"
    upsert_item(conn, item(url, title="Original title"))
    conn.execute(
        "UPDATE articles SET translated_title='原标题译文' WHERE canonical_url=?",
        (url,),
    )
    conn.commit()

    upsert_item(conn, item(url, title="Original title"))
    assert conn.execute(
        "SELECT translated_title FROM articles WHERE canonical_url=?", (url,)
    ).fetchone()[0] == "原标题译文"

    upsert_item(conn, item(url, title="Changed title"))
    assert conn.execute(
        "SELECT translated_title FROM articles WHERE canonical_url=?", (url,)
    ).fetchone()[0] is None


# --- 无 published_at 回退到 first_seen_at ---

def test_missing_published_at_falls_back_to_first_seen(conn):
    it = SourceItem("semi", "semiconductor", "Semi", "https://example.com/nopub", "No pub", "s", None)
    event_id, _ = upsert_item(conn, it)
    rows = list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"]
    assert [r["id"] for r in rows] == [event_id]
    assert rows[0]["published_at"] is None
    assert rows[0]["first_seen_at"] is not None


# --- 已读/收藏组合 ---

def test_read_and_favorite_flags_compose(conn):
    e1, _ = upsert_item(conn, item("https://example.com/1"))
    e2, _ = upsert_item(conn, item("https://example.com/2"))
    set_read(conn, "default", e1, True)
    set_favorite(conn, "default", e1, True)
    set_favorite(conn, "default", e2, True)

    read_items = list_events(conn, "default", "semiconductor", "read", "", 50, None)["items"]
    assert [r["id"] for r in read_items] == [e1]
    unread = list_events(conn, "default", "semiconductor", "unread", "", 50, None)["items"]
    assert [r["id"] for r in unread] == [e2]

    favs = list_events(conn, "default", "favorites", "read", "", 50, None)["items"]
    assert [r["id"] for r in favs] == [e1]
    assert favs[0]["read"] is True and favs[0]["favorite"] is True

    # 取消已读/收藏
    set_read(conn, "default", e1, False)
    set_favorite(conn, "default", e2, False)
    assert list_events(conn, "default", "semiconductor", "read", "", 50, None)["items"] == []
    assert [r["id"] for r in list_events(conn, "default", "favorites", "all", "", 50, None)["items"]] == [e1]


def test_set_read_missing_event_raises_keyerror(conn):
    with pytest.raises(KeyError):
        set_read(conn, "default", 9999, True)
    with pytest.raises(KeyError):
        set_favorite(conn, "default", 9999, True)


# --- 非法参数 ---

def test_invalid_domain_read_limit_raise_valueerror(conn):
    upsert_item(conn, item("https://example.com/a"))
    with pytest.raises(ValueError):
        list_events(conn, "default", "sports", "all", "", 50, None)
    with pytest.raises(ValueError):
        list_events(conn, "default", "semiconductor", "maybe", "", 50, None)
    with pytest.raises(ValueError):
        list_events(conn, "default", "semiconductor", "all", "", 0, None)
    with pytest.raises(ValueError):
        list_events(conn, "default", "semiconductor", "all", "", 101, None)


# --- 游标分页 ---

def _seed(conn, n):
    ids = []
    for i in range(n):
        eid, _ = upsert_item(conn, item(f"https://example.com/p{i}", age_days=i, title=f"Ev {i}"))
        ids.append(eid)
    return ids


def test_cursor_pagination_walks_all_items_in_order(conn):
    _seed(conn, 5)
    seen = []
    cursor = None
    while True:
        page = list_events(conn, "default", "semiconductor", "all", "", 2, cursor)
        seen.extend(r["id"] for r in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_cursor_pagination_stability(conn):
    _seed(conn, 5)
    page1 = list_events(conn, "default", "semiconductor", "all", "", 2, None)
    page2 = list_events(conn, "default", "semiconductor", "all", "", 2, page1["next_cursor"])
    page3 = list_events(conn, "default", "semiconductor", "all", "", 2, page2["next_cursor"])
    ids1 = [r["id"] for r in page1["items"]]
    ids2 = [r["id"] for r in page2["items"]]
    ids3 = [r["id"] for r in page3["items"]]
    assert set(ids1).isdisjoint(ids2)
    assert set(ids1 + ids2).isdisjoint(ids3)
    assert len(ids1 + ids2 + ids3) == 5
    assert page2["next_cursor"] is not None
    assert page3["next_cursor"] is None
    assert page3["items"] and len(ids3) == 1


# --- counts ---

def test_counts_split_by_domain_and_unread(conn):
    e1, _ = upsert_item(conn, item("https://example.com/s1"))
    e2, _ = upsert_item(conn, item("https://example.com/s2"))
    upsert_item(conn, item("https://example.com/m1", domain="music", source_name="Mu"))
    set_read(conn, "default", e1, True)
    set_favorite(conn, "default", e1, True)
    set_favorite(conn, "default", e2, True)
    c = counts(conn, "default")
    assert c["semiconductor"] == {"total": 2, "unread": 1}
    assert c["music"] == {"total": 1, "unread": 1}
    assert c["favorites"] == 2
    assert c["favorites_unread"] == 1


def test_counts_exclude_older_than_30_days_but_favorites_include(conn):
    upsert_item(conn, item("https://example.com/recent"))
    old_id, _ = upsert_item(conn, item("https://example.com/ancient", 31))
    set_favorite(conn, "default", old_id, True)
    c = counts(conn, "default")
    assert c["semiconductor"]["total"] == 1
    assert c["favorites"] == 1
    assert c["favorites_unread"] == 1


# --- 搜索结果字段完整性 ---

def test_list_events_item_shape(conn):
    eid, _ = upsert_item(conn, item("https://example.com/shape"))
    row = list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"][0]
    assert set(row.keys()) == {
        "id", "domain", "headline", "summary", "url",
        "translated_headline", "source_name", "published_at", "first_seen_at",
        "read", "favorite", "evidence_count",
    }
    assert row["id"] == eid
    assert row["url"] == "https://example.com/shape"
    assert row["read"] is False and row["favorite"] is False
    assert row["evidence_count"] == 1


# --- C-1: FTS 对抗输入不得抛 OperationalError ---

@pytest.mark.parametrize("q", ["-foo", "--", "HBM -TSMC", "-boom", "-", "...", "((", "5nm", "x86-64"])
def test_search_arbitrary_input_never_raises(conn, q):
    upsert_item(conn, item("https://example.com/adv1", title="HBM capacity expands"))
    upsert_item(conn, item("https://example.com/adv2", title="TSMC 5nm roadmap"))
    page = list_events(conn, "default", "semiconductor", "all", q, 50, None)
    assert isinstance(page["items"], list)


def test_search_pure_punctuation_degrades_to_unfiltered(conn):
    eid, _ = upsert_item(conn, item("https://example.com/punct"))
    page = list_events(conn, "default", "semiconductor", "all", "--", 50, None)
    assert [r["id"] for r in page["items"]] == [eid]


# --- I-4: 中文子串搜索 ---

def test_chinese_substring_search(conn):
    eid, _ = upsert_item(conn, item("https://example.com/cn", title="美光宣布 HBM4 存储芯片量产"))
    assert [r["id"] for r in list_events(conn, "default", "semiconductor", "all", "芯片", 50, None)["items"]] == [eid]
    assert [r["id"] for r in list_events(conn, "default", "semiconductor", "all", "存储", 50, None)["items"]] == [eid]
    # 多词 AND：两个中文子串 + 中英混合（CJK 走 LIKE，英文词走 FTS）
    assert [r["id"] for r in list_events(conn, "default", "semiconductor", "all", "存储 芯片", 50, None)["items"]] == [eid]
    assert [r["id"] for r in list_events(conn, "default", "semiconductor", "all", "HBM4 芯片", 50, None)["items"]] == [eid]
    assert list_events(conn, "default", "semiconductor", "all", "英特尔", 50, None)["items"] == []


# --- I-2: sources 元数据不被文章 URL 腐蚀 ---

def test_second_article_does_not_overwrite_source_metadata(conn):
    upsert_item(conn, item("https://feed.example/article-1", title="Article one"))
    conn.execute(
        "UPDATE sources SET url='https://feed.example/rss', name='Semi Daily' WHERE id='semi'"
    )
    conn.commit()
    upsert_item(conn, item("https://feed.example/article-2", title="Article two"))
    row = conn.execute("SELECT url, name, adapter FROM sources WHERE id='semi'").fetchone()
    assert row["url"] == "https://feed.example/rss"
    assert row["name"] == "Semi Daily"


# --- I-3: 事务中性 ---

def test_upsert_inside_outer_transaction_rolls_back(conn):
    conn.execute("BEGIN")
    upsert_item(conn, item("https://example.com/tx"))
    assert conn.in_transaction
    conn.rollback()
    assert counts(conn, "default")["semiconductor"]["total"] == 0


def test_set_read_favorite_transaction_neutral(conn):
    eid, _ = upsert_item(conn, item("https://example.com/tx2"))
    conn.execute("BEGIN")
    set_read(conn, "default", eid, True)
    set_favorite(conn, "default", eid, True)
    conn.rollback()
    row = list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"][0]
    assert row["read"] is False and row["favorite"] is False


# --- 北京时间规范化 ---

def test_utc_published_at_converted_to_beijing(conn):
    upsert_item(conn, item("https://example.com/tz1", published_at="2026-09-10T00:00:00+00:00"))
    row = conn.execute("SELECT published_at FROM articles WHERE canonical_url LIKE '%/tz1'").fetchone()
    assert row["published_at"] == "2026-09-10T08:00:00+08:00"


def test_naive_published_at_interpreted_as_beijing(conn):
    upsert_item(conn, item("https://example.com/tz2", published_at="2026-09-10 16:00:00"))
    row = conn.execute("SELECT published_at FROM articles WHERE canonical_url LIKE '%/tz2'").fetchone()
    assert row["published_at"] == "2026-09-10T16:00:00+08:00"


def test_unparseable_published_at_stored_as_null(conn):
    it = SourceItem("semi", "semiconductor", "Semi", "https://example.com/bad", "Bad date", "s", "not a date")
    event_id, _ = upsert_item(conn, it)
    row = list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"][0]
    assert row["id"] == event_id
    assert row["published_at"] is None


def test_generated_timestamps_are_beijing_iso(conn):
    eid, _ = upsert_item(conn, item("https://example.com/fmt"))
    row = list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"][0]
    assert _BEIJING_RE.fullmatch(row["first_seen_at"])
    set_read(conn, "default", eid, True)
    set_favorite(conn, "default", eid, True)
    us = conn.execute("SELECT read_at, favorite_at FROM user_state WHERE profile='default'").fetchone()
    assert _BEIJING_RE.fullmatch(us["read_at"])
    assert _BEIJING_RE.fullmatch(us["favorite_at"])


# --- 30 天边界与混合格式排序 ---

def test_thirty_day_window_boundary(conn):
    now = datetime.now(timezone.utc)
    inside = (now - timedelta(days=30) + timedelta(seconds=10)).isoformat()
    outside = (now - timedelta(days=30) - timedelta(seconds=10)).isoformat()
    e_in, _ = upsert_item(conn, item("https://example.com/boundary-in", published_at=inside))
    e_out, _ = upsert_item(conn, item("https://example.com/boundary-out", published_at=outside))
    rows = list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"]
    assert [r["id"] for r in rows] == [e_in]
    assert rows[0]["id"] != e_out


def test_sorting_normalizes_mixed_time_formats(conn):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    early_aware = (now - timedelta(hours=5)).isoformat()  # aware, earlier instant
    late_naive_bj = (now - timedelta(hours=1)).astimezone(_BJ).replace(tzinfo=None).isoformat(sep=" ")
    e_early, _ = upsert_item(conn, item("https://example.com/mix-early", title="Early", published_at=early_aware))
    e_late, _ = upsert_item(conn, item("https://example.com/mix-late", title="Late", published_at=late_naive_bj))
    # late_naive_bj 按北京时间解释，实际比 early_aware 晚 4 小时：必须排在前面
    rows = list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"]
    assert [r["id"] for r in rows] == [e_late, e_early]


def test_cursor_pagination_with_mixed_time_formats(conn):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    times = [
        (now - timedelta(hours=h)).astimezone(_BJ).replace(tzinfo=None).isoformat(sep=" ")
        if i % 2 else (now - timedelta(hours=h)).isoformat()
        for i, h in enumerate([1, 2, 3, 4])
    ]
    ids = []
    for i, t in enumerate(times):
        eid, _ = upsert_item(conn, item(f"https://example.com/fmt{i}", title=f"Fmt {i}", published_at=t))
        ids.append(eid)
    expected = [ids[0], ids[1], ids[2], ids[3]]  # 1h,2h,3h,4h ago → newest first
    seen = []
    cursor = None
    while True:
        page = list_events(conn, "default", "semiconductor", "all", "", 3, cursor)
        seen.extend(r["id"] for r in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == expected


def test_cursor_encodes_sql_normalized_time(conn):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    published = (now - timedelta(hours=2)).isoformat()
    upsert_item(conn, item("https://example.com/cursor-norm", title="Cursor norm", published_at=published))
    upsert_item(conn, item("https://example.com/cursor-norm-2", title="Cursor norm two",
                           published_at=(now - timedelta(hours=3)).isoformat()))
    page = list_events(conn, "default", "semiconductor", "all", "", 1, None)
    payload = json.loads(base64.urlsafe_b64decode(page["next_cursor"].encode("ascii")))
    # cursor 必须是 SQL datetime() 归一后的 UTC 'YYYY-MM-DD HH:MM:SS'，不是原始 +08:00 字符串
    expected = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    assert payload[0] == expected


# --- M-1: 重复 URL 且新 published_at 为 NULL 时保留已有时间 ---

def test_duplicate_url_with_null_published_keeps_existing(conn):
    published = datetime.now(_BJ).replace(microsecond=0).isoformat()
    e1, _ = upsert_item(conn, item("https://example.com/keep", published_at=published))
    it = SourceItem("semi", "semiconductor", "Semi", "https://example.com/keep", "Keep time", "s", None)
    e2, created = upsert_item(conn, it)
    assert created is False and e1 == e2
    row = list_events(conn, "default", "semiconductor", "all", "", 50, None)["items"][0]
    assert row["published_at"] == published


# --- 复审 I-1: 无空格中英混排标题搜索 ---

def test_mixed_cjk_latin_title_searchable(conn):
    eid, _ = upsert_item(conn, item("https://example.com/mix-cjk", title="美光HBM4芯片量产启动"))
    # 整段混排查询（照抄标题子串）
    assert [r["id"] for r in list_events(conn, "default", "semiconductor", "all", "美光HBM4芯片", 50, None)["items"]] == [eid]
    # 嵌在 CJK 中的拉丁型号
    assert [r["id"] for r in list_events(conn, "default", "semiconductor", "all", "HBM4", 50, None)["items"]] == [eid]
    # CJK 子串
    assert [r["id"] for r in list_events(conn, "default", "semiconductor", "all", "芯片", 50, None)["items"]] == [eid]
    assert [r["id"] for r in list_events(conn, "default", "semiconductor", "all", "量产启动", 50, None)["items"]] == [eid]


def test_mixed_cjk_latin_multiword_and(conn):
    eid1, _ = upsert_item(conn, item("https://example.com/mix1", title="美光HBM4芯片量产启动"))
    upsert_item(conn, item("https://example.com/mix2", title="三星HBM3E量产"))
    rows = list_events(conn, "default", "semiconductor", "all", "美光 HBM4", 50, None)["items"]
    assert [r["id"] for r in rows] == [eid1]
    rows = list_events(conn, "default", "semiconductor", "all", "HBM4芯片", 50, None)["items"]
    assert [r["id"] for r in rows] == [eid1]


# --- 复审 I-2: 不吞真实数据库故障，FTS 保留词安全 ---

@pytest.mark.parametrize("q", ["AND", "OR", "NOT", "NEAR", "AND OR NOT NEAR", "NEAR(", "AND(", "TSMC AND HBM4"])
def test_search_fts_reserved_words_never_raise(conn, q):
    upsert_item(conn, item("https://example.com/rw1", title="TSMC HBM4 roadmap"))
    page = list_events(conn, "default", "semiconductor", "all", q, 50, None)
    assert isinstance(page["items"], list)


def test_search_raises_when_fts_table_missing(conn):
    upsert_item(conn, item("https://example.com/drop1", title="TSMC HBM4 roadmap"))
    conn.execute("DROP TABLE event_search")
    with pytest.raises(sqlite3.OperationalError):
        list_events(conn, "default", "semiconductor", "all", "HBM4", 50, None)


# --- profile 隔离 ---

def test_profile_isolation(conn):
    eid, _ = upsert_item(conn, item("https://example.com/prof"))
    set_read(conn, "alice", eid, True)
    set_favorite(conn, "alice", eid, True)
    # bob 的视角：未读、未收藏
    row = list_events(conn, "bob", "semiconductor", "all", "", 50, None)["items"][0]
    assert row["read"] is False and row["favorite"] is False
    assert list_events(conn, "bob", "semiconductor", "read", "", 50, None)["items"] == []
    assert list_events(conn, "bob", "favorites", "all", "", 50, None)["items"] == []
    # alice 的视角：已读、已收藏
    assert counts(conn, "alice")["favorites"] == 1
    assert counts(conn, "bob")["favorites"] == 0
    assert counts(conn, "default")["semiconductor"]["unread"] == 1


# --- favorites + read + search 三组合 ---

def test_favorites_read_search_compose(conn):
    e1, _ = upsert_item(conn, item("https://example.com/f1", title="HBM advances"))
    e2, _ = upsert_item(conn, item("https://example.com/f2", title="HBM review", domain="music", source_name="Mu"))
    e3, _ = upsert_item(conn, item("https://example.com/f3", title="GPU news"))
    set_favorite(conn, "default", e1, True)
    set_favorite(conn, "default", e3, True)
    # favorites + unread + 中文/英文搜索
    rows = list_events(conn, "default", "favorites", "unread", "HBM", 50, None)["items"]
    assert [r["id"] for r in rows] == [e1]
    assert rows[0]["favorite"] is True and rows[0]["read"] is False
    # e2 不属于 favorites（未收藏），music 域也不匹配
    assert e2 not in [r["id"] for r in rows]
    # 已读后从 unread 组合中消失
    set_read(conn, "default", e1, True)
    assert list_events(conn, "default", "favorites", "unread", "HBM", 50, None)["items"] == []
    assert [r["id"] for r in list_events(conn, "default", "favorites", "read", "HBM", 50, None)["items"]] == [e1]
