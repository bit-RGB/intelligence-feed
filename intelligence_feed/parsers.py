# intelligence_feed/parsers.py
"""RSS / Atom / Last.fm JSON / HTML 列表解析。

所有摘要统一走 clean_summary：去 HTML 标签、合并空白、截断 280 字符。
日期输出为带时区的 ISO 字符串；repository 层负责归一到北京时间。
"""
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from .models import SourceItem

SUMMARY_LIMIT = 280

_TAG_RE = re.compile(r"<[^>]+>")

# 部分站点（如爱集微）把相对时间前缀混进标题文本："16小时前 上海交大…"。
# 入库前剥掉，标题只留新闻本体。
_TITLE_TIME_PREFIX = re.compile(
    r"^\s*(?:\d+\s*(?:秒|分钟|小时|天)前|昨天|今天|刚刚|"
    r"\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—:：|]?\s*"
)


def clean_title(title: str | None) -> str:
    return _TITLE_TIME_PREFIX.sub("", (title or "").strip()).strip()


class _TextExtractor(HTMLParser):
    """Extract visible text from an HTML fragment."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def clean_summary(text: str | None, limit: int = SUMMARY_LIMIT) -> str:
    """Strip HTML tags, collapse whitespace, truncate to `limit` chars."""
    if not text:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(text)
        plain = " ".join("".join(extractor.parts).split())
    except Exception:  # noqa: BLE001 - malformed HTML falls back to regex strip
        plain = " ".join(_TAG_RE.sub(" ", text).split())
    return plain[:limit]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(el, *names: str) -> str | None:
    wanted = {n.lower() for n in names}
    for child in el:
        if _local(child.tag) in wanted and child.text and child.text.strip():
            return child.text.strip()
    return None


def _parse_date(raw: str | None) -> str | None:
    """RFC822 (pubDate) 或 ISO-8601；解析失败返回 None。"""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _item(source: dict, url, title, summary, published_at) -> SourceItem | None:
    """单一收口：所有解析器产出的条目都必须通过这里。

    非 HTTPS 原地址直接丢弃（返回 None），调用方跳过该条而非整源失败；
    fragment 统一去除，避免去重键噪声。
    """
    if not url:
        return None
    url, _frag = urldefrag(url.strip())
    if urlsplit(url).scheme != "https":
        return None
    return SourceItem(
        source_id=source["id"],
        domain=source["domain"],
        source_name=source["name"],
        url=url,
        title=clean_title(title) or "(untitled)",
        summary=clean_summary(summary),
        published_at=published_at,
    )


def _abs_url(href: str | None, base: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href:
        return None
    if base:
        href = urljoin(base, href)
    return href or None


# --- RSS 2.0 / RDF ---

def parse_rss(data: bytes, source: dict) -> list[SourceItem]:
    root = ET.fromstring(data)
    channel_link = None
    for el in root.iter():
        if _local(el.tag) == "channel":
            channel_link = _child_text(el, "link")
            break
    # item 相对链接以来源 feed URL 为 base；channel link 常指向站点首页，
    # 用它解析会把相对链接指错主机（评审 Minor 4）。
    base = source.get("url") or channel_link
    items: list[SourceItem] = []
    for el in root.iter():
        if _local(el.tag) != "item":
            continue
        title = _child_text(el, "title")
        link = _abs_url(_child_text(el, "link"), base)
        if not link:
            guid = _child_text(el, "guid", "id")
            if guid and guid.startswith("https://"):
                link = guid
        summary = _child_text(el, "description", "encoded", "content", "summary")
        date = _parse_date(_child_text(el, "pubdate", "date", "published", "updated"))
        if not link:
            continue
        item = _item(source, link, title, summary, date)
        if item is not None:
            items.append(item)
    return items


# --- Atom ---

def _atom_link(entry) -> str | None:
    fallback = None
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if not href:
            continue
        rel = (child.get("rel") or "alternate").lower()
        if rel == "alternate":
            return href
        if fallback is None:
            fallback = href
    return fallback


def parse_atom(data: bytes, source: dict) -> list[SourceItem]:
    root = ET.fromstring(data)
    feed_link = None
    for child in root:
        if _local(child.tag) == "link" and (child.get("rel") or "alternate").lower() == "alternate":
            feed_link = child.get("href")
            break
    base = source.get("url") or feed_link
    items: list[SourceItem] = []
    for entry in root:
        if _local(entry.tag) != "entry":
            continue
        title = _child_text(entry, "title")
        link = _abs_url(_atom_link(entry), base)
        if not link:
            guid = _child_text(entry, "id")
            if guid and guid.startswith("https://"):
                link = guid
        summary = _child_text(entry, "summary", "content")
        date = _parse_date(_child_text(entry, "published", "updated", "date"))
        if not link:
            continue
        item = _item(source, link, title, summary, date)
        if item is not None:
            items.append(item)
    return items


def parse_feed(data: bytes, source: dict) -> list[SourceItem]:
    """按根元素本地名分派 RSS 或 Atom。"""
    root = ET.fromstring(data)
    name = _local(root.tag)
    if name == "feed":
        return parse_atom(data, source)
    if name in ("rss", "rdf"):
        return parse_rss(data, source)
    raise ValueError(f"unrecognized feed root element: {root.tag!r}")


# --- 微博热搜 JSON ---

def parse_weibo_hot(data: bytes, source: dict) -> list[SourceItem]:
    """微博热搜接口（side/hotSearch）的 JSON 解析。

    realtime 条目的 num 是真实热度数值，格式化为“万”存进 summary 供前端展示；
    无热度的条目保留但 summary 为空。链接统一指向微博搜索页。
    """
    payload = json.loads(data)
    realtime = ((payload.get("data") or {}).get("realtime")) or []
    # 微博没有词条发布时间；用本次抓取时间标记整张榜单。所有条目共享同一个时间，
    # 重复词条再次上榜时 upsert 会把它刷新到当前批次，而不是留在历史日期中。
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items: list[SourceItem] = []
    for row in realtime:
        word = (row.get("word") or "").strip()
        if not word:
            continue
        hot = row.get("num") or row.get("raw_hot")
        if isinstance(hot, (int, float)) and hot > 0:
            summary = f"{hot / 10000:.1f}万" if hot >= 10000 else str(int(hot))
        else:
            summary = ""
        item = _item(
            source,
            f"https://s.weibo.com/weibo?q=%23{quote(word)}%23",
            word,
            summary,
            captured_at,
        )
        if item is not None:
            items.append(item)
    return items



# --- Last.fm JSON ---

def parse_lastfm(data: bytes, source: dict) -> list[SourceItem]:
    payload = json.loads(data)
    tracks = (payload.get("tracks") or {}).get("track") or []
    items: list[SourceItem] = []
    for track in tracks:
        name = (track.get("name") or "").strip()
        url = (track.get("url") or "").strip()
        artist = track.get("artist") or {}
        artist_name = (artist.get("#text") or artist.get("name") or "").strip()
        if not name or not url:
            continue
        title = f"{artist_name} — {name}" if artist_name else name
        stats = []
        if track.get("listeners"):
            stats.append(f"{track['listeners']} listeners")
        if track.get("playcount"):
            stats.append(f"{track['playcount']} plays")
        item = _item(source, url, title, " · ".join(stats) or None, None)
        if item is not None:
            items.append(item)
    return items


# --- HTML 列表 ---

def _normalized_origin(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) 全小写、默认端口补全，用于同源比较。"""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host, port)


def _resolve_same_origin_https(href: str, base: str) -> str | None:
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    lowered = href.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None
    url, _frag = urldefrag(urljoin(base, href))
    parts = urlsplit(url)
    if parts.scheme != "https":
        return None
    if _normalized_origin(url) != _normalized_origin(base):
        return None
    return url


class _NewsListParser(HTMLParser):
    def __init__(self, source: dict, base: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.base = base
        self.rows: list[SourceItem] = []
        self._seen: set[str] = set()
        self._current: dict | None = None
        self._in_time = False
        self._heading_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._current = {"href": attrs.get("href"), "title": [], "heading": [], "time": None}
            self._heading_depth = 0
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._current is not None:
            self._heading_depth += 1
        elif tag == "time" and self._current is not None and not self._in_time:
            self._in_time = True
            if attrs.get("datetime"):
                self._current["time"] = attrs["datetime"]
        elif tag in ("br", "p", "li") and self._current is not None:
            self._current["title"].append(" ")

    def handle_endtag(self, tag):
        if tag == "time":
            self._in_time = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._heading_depth:
            self._heading_depth -= 1
        elif tag == "a" and self._current is not None:
            self._finish_anchor()

    def handle_data(self, data):
        if self._current is not None and not self._in_time:
            self._current["title"].append(data)
            if self._heading_depth:
                self._current["heading"].append(data)

    def _finish_anchor(self):
        cur = self._current
        self._current = None
        href = cur.get("href")
        if not href:
            return
        url = _resolve_same_origin_https(href, self.base)
        if not url:
            return
        if self.source.get("strip_query"):
            parts = urlsplit(url)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if url in self._seen:
            return
        include_url_regex = self.source.get("include_url_regex")
        if include_url_regex and re.search(include_url_regex, url) is None:
            return
        heading = " ".join("".join(cur["heading"]).split())
        title = heading or " ".join("".join(cur["title"]).split())
        if not title:
            return
        self._seen.add(url)
        item = _make_html_item(self.source, url, title, cur.get("time"))
        if item is not None:
            self.rows.append(item)


def _make_html_item(source, url, title, time_raw):
    return _item(source, url, title, None, _parse_date(time_raw))


def parse_html_list(data: bytes, source: dict) -> list[SourceItem]:
    """提取同源 HTTPS 链接列表。

    相对链接相对页面 URL（source["url"]）解析；同源与 HTTPS 校验以
    source["base_url"]（缺省为页面 URL 本身）的 origin 为准。
    """
    base = source.get("base_url") or source["url"]
    parser = _NewsListParser(source, base)
    parser.feed(data.decode("utf-8", errors="replace"))
    if parser._current is not None:
        parser._finish_anchor()
    max_items = source.get("max_items")
    if isinstance(max_items, int) and max_items >= 0:
        return parser.rows[:max_items]
    return parser.rows
