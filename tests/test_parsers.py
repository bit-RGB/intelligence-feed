# tests/test_parsers.py
import json
from pathlib import Path

from intelligence_feed.parsers import (
    clean_summary,
    parse_atom,
    parse_feed,
    parse_html_list,
    parse_lastfm,
    parse_rss,
)

F = Path(__file__).parent / "fixtures"

SEMI = {"id": "semi", "domain": "semiconductor", "name": "Semi"}
CORP = {"id": "corp", "domain": "semiconductor", "name": "Corp",
        "base_url": "https://corp.example"}
MUSIC = {"id": "lastfm-top-tracks", "domain": "music", "name": "Last.fm"}


# --- brief 自带测试 ---

def test_rss_extracts_required_fields():
    rows = parse_rss((F / "rss.xml").read_bytes(), SEMI)
    assert rows[0].url == "https://example.com/a"
    assert rows[0].title == "Chiplet update"


def test_html_rejects_javascript_urls():
    rows = parse_html_list((F / "company-news.html").read_bytes(), CORP)
    assert all(r.url.startswith("https://") for r in rows)


# --- RSS ---

def test_rss_strips_html_and_collapses_whitespace_in_summary():
    rows = parse_rss((F / "rss.xml").read_bytes(), SEMI)
    assert rows[0].summary == "HBM capacity grows"
    assert "<" not in rows[0].summary and "  " not in rows[0].summary


def test_rss_summary_truncated_to_280_chars():
    rows = parse_rss((F / "rss.xml").read_bytes(), SEMI)
    assert len(rows[2].summary) == 280
    assert rows[2].summary.startswith("Advanced packaging")


def test_rss_uses_guid_when_link_missing():
    rows = parse_rss((F / "rss.xml").read_bytes(), SEMI)
    assert rows[1].url == "https://example.com/b"
    assert rows[1].title == "Guid only item"


def test_rss_parses_rfc822_and_iso_dates_to_offset_aware_iso():
    rows = parse_rss((F / "rss.xml").read_bytes(), SEMI)
    assert rows[0].published_at == "2025-09-11T08:00:00+00:00"
    assert rows[1].published_at == "2025-09-10T09:30:00+00:00"
    # +0200 offset must be normalized away from the wall clock
    assert rows[2].published_at == "2025-09-10T08:00:00+00:00"


def test_rss_items_carry_source_identity():
    rows = parse_rss((F / "rss.xml").read_bytes(), SEMI)
    assert all(r.source_id == "semi" for r in rows)
    assert all(r.domain == "semiconductor" for r in rows)
    assert all(r.source_name == "Semi" for r in rows)


# --- Atom ---

def test_atom_prefers_alternate_link_over_enclosure():
    rows = parse_atom((F / "atom.xml").read_bytes(), SEMI)
    assert rows[0].url == "https://example.org/one"


def test_atom_link_defaults_to_alternate_rel():
    rows = parse_atom((F / "atom.xml").read_bytes(), SEMI)
    assert rows[1].url == "https://example.org/two"


def test_atom_parses_iso_dates_with_z_and_numeric_offsets():
    rows = parse_atom((F / "atom.xml").read_bytes(), SEMI)
    assert rows[0].published_at == "2025-09-11T07:15:00+00:00"
    assert rows[1].published_at == "2025-09-10T10:00:00+00:00"


def test_atom_falls_back_from_summary_to_content():
    rows = parse_atom((F / "atom.xml").read_bytes(), SEMI)
    assert rows[0].summary == "Atom summary & details"
    assert rows[1].summary == "Content body"


def test_parse_feed_sniffs_rss_and_atom():
    rss_rows = parse_feed((F / "rss.xml").read_bytes(), SEMI)
    atom_rows = parse_feed((F / "atom.xml").read_bytes(), SEMI)
    assert [r.url for r in rss_rows][:2] == ["https://example.com/a", "https://example.com/b"]
    assert [r.url for r in atom_rows] == ["https://example.org/one", "https://example.org/two"]


# --- Last.fm ---

def test_lastfm_builds_artist_title_and_uses_track_url():
    rows = parse_lastfm((F / "lastfm.json").read_bytes(), MUSIC)
    assert rows[0].title == "Artist A — Song A"
    assert rows[0].url == "https://www.last.fm/music/Artist+A/_/Song+A"
    assert rows[0].domain == "music"
    assert rows[0].source_id == "lastfm-top-tracks"
    assert len(rows) == 2


# --- HTML 列表 ---

def test_html_keeps_only_same_origin_https_and_dedupes():
    rows = parse_html_list((F / "company-news.html").read_bytes(), CORP)
    urls = [r.url for r in rows]
    assert urls == [
        "https://corp.example/news/first",
        "https://corp.example/news/second",
        "https://corp.example/news/third",
    ]


def test_html_uses_time_datetime_and_strips_anchor_text():
    rows = parse_html_list((F / "company-news.html").read_bytes(), CORP)
    assert rows[0].published_at == "2025-09-11T01:00:00+00:00"  # = 09:00 +08:00
    assert rows[0].title == "First announcement"
    assert rows[2].title == "Third with image"


def test_html_prefers_heading_over_category_and_summary_text():
    html = b'''<html><body>
      <a href="/news/first">
        <span>Announcements</span>
        <h2>Actual article title</h2>
        <p>Long summary that must not become part of the title.</p>
      </a>
    </body></html>'''
    rows = parse_html_list(html, CORP)
    assert rows[0].title == "Actual article title"


def test_html_rejects_cross_origin_and_insecure_and_fragment_links():
    rows = parse_html_list((F / "company-news.html").read_bytes(), CORP)
    urls = [r.url for r in rows]
    assert not any("other.example" in u for u in urls)
    assert not any(u.startswith("http://") for u in urls)
    assert not any("#" in u for u in urls)


# --- 摘要清洗 ---

def test_clean_summary_strips_tags_collapses_and_truncates():
    raw = "<p>  Hello <b>world</b>   again</p>" + " filler" * 60
    out = clean_summary(raw)
    assert out == (" ".join(("Hello world again" + " filler" * 60).split()))[:280]
    assert len(out) <= 280
    assert "<" not in out


def test_clean_summary_handles_script_style_blocks():
    assert clean_summary("<style>x{}</style>ok<script>bad()</script>") == "ok"


# --- HTTPS 产出强制（评审 C1） ---

FEED_BASED = {"id": "semi", "domain": "semiconductor", "name": "Semi",
              "url": "https://feed.example/rss.xml"}


def test_rss_skips_http_link_but_keeps_https_siblings():
    data = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title><link>https://example.com/</link>
      <item><title>insecure</title><link>http://example.com/insecure</link></item>
      <item><title>secure</title><link>https://example.com/secure</link></item>
    </channel></rss>"""
    rows = parse_rss(data, FEED_BASED)
    assert [r.url for r in rows] == ["https://example.com/secure"]


def test_rss_ignores_http_guid_fallback():
    data = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title><link>https://example.com/</link>
      <item><title>g</title><guid>http://example.com/guid-insecure</guid></item>
    </channel></rss>"""
    assert parse_rss(data, FEED_BASED) == []


def test_rss_accepts_https_guid_fallback():
    data = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title><link>https://example.com/</link>
      <item><title>g</title><guid>https://example.com/guid-secure</guid></item>
    </channel></rss>"""
    rows = parse_rss(data, FEED_BASED)
    assert [r.url for r in rows] == ["https://example.com/guid-secure"]


def test_rss_relative_link_resolves_against_feed_url():
    data = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>t</title><link>https://site.example/</link>
      <item><title>rel</title><link>/news/x</link></item>
    </channel></rss>"""
    rows = parse_rss(data, FEED_BASED)
    assert [r.url for r in rows] == ["https://feed.example/news/x"]


def test_atom_skips_http_link():
    data = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
      <entry><title>a</title><link href="http://example.org/insecure"/></entry>
      <entry><title>b</title><link href="https://example.org/secure"/></entry>
    </feed>"""
    rows = parse_atom(data, FEED_BASED)
    assert [r.url for r in rows] == ["https://example.org/secure"]


def test_atom_ignores_http_id_fallback():
    data = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
      <entry><title>a</title><id>http://example.org/id-insecure</id></entry>
    </feed>"""
    assert parse_atom(data, FEED_BASED) == []


def test_lastfm_skips_http_track_url():
    payload = json.dumps({"tracks": {"track": [
        {"name": "Insecure", "url": "http://last.fm/insecure", "artist": {"#text": "A"}},
        {"name": "Secure", "url": "https://last.fm/secure", "artist": {"#text": "A"}},
    ]}}).encode()
    rows = parse_lastfm(payload, MUSIC)
    assert [r.url for r in rows] == ["https://last.fm/secure"]


# --- HTML Minor：同源规范化与 fragment ---

def test_html_accepts_explicit_default_port_and_uppercase_host():
    html = b'<html><body><a href="HTTPS://CORP.EXAMPLE:443/news/x">X</a></body></html>'
    rows = parse_html_list(html, CORP)
    assert len(rows) == 1
    assert rows[0].url.lower() == "https://corp.example:443/news/x"


def test_html_strips_fragment_from_path_urls():
    html = b'<html><body><a href="/news/x#frag">X</a></body></html>'
    rows = parse_html_list(html, CORP)
    assert [r.url for r in rows] == ["https://corp.example/news/x"]


def test_title_time_prefix_stripped():
    from intelligence_feed.parsers import clean_title
    assert clean_title("16小时前 上海交大团队取得进展") == "上海交大团队取得进展"
    assert clean_title("16小时前 - 带连字符标题") == "带连字符标题"
    assert clean_title("3分钟前 快讯") == "快讯"
    assert clean_title("09-06 07:00 存储涨价") == "存储涨价"
    assert clean_title("2026-09-14 16:44:12 发布会") == "发布会"
    assert clean_title("正常标题") == "正常标题"
    assert clean_title("华为16小时续航新机") == "华为16小时续航新机"
    assert clean_title(None) == ""


def test_weibo_hot_parser_extracts_words_and_heat():
    from intelligence_feed.parsers import parse_weibo_hot
    payload = {"data": {"realtime": [
        {"word": "某新闻", "num": 1234567},
        {"word": "无热度置顶"},
        {"word": ""},
    ]}}
    src = {"id": "weibo", "domain": "weibo", "name": "微博热搜"}
    rows = parse_weibo_hot(json.dumps(payload).encode(), src)
    assert [r.title for r in rows] == ["某新闻", "无热度置顶"]
    assert rows[0].summary == "123.5万"
    assert rows[0].url.startswith("https://s.weibo.com/weibo?q=")
    # 同一次抓取必须拥有相同的榜单时间，重复词条再次出现时才能刷新到当前榜单。
    assert rows[0].published_at is not None
    assert {r.published_at for r in rows} == {rows[0].published_at}


def test_html_applies_url_filter_query_stripping_and_item_limit():
    html = b'''<html><body>
      <a href="/nav">Navigation</a>
      <a href="/news/1?tracking=yes">First</a>
      <a href="/news/2?tracking=yes">Second</a>
    </body></html>'''
    source = {
        **CORP,
        "include_url_regex": r"/news/\d+$",
        "strip_query": True,
        "max_items": 1,
    }
    rows = parse_html_list(html, source)
    assert [(row.url, row.title) for row in rows] == [
        ("https://corp.example/news/1", "First")
    ]
