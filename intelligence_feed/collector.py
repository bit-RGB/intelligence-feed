"""Budgeted collection with atomic source writes and event evidence merging."""
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from urllib.parse import quote, quote_plus

from .db import connect
from .repository import _now_beijing, rebuild_event_search, upsert_item
from .sources import fetch_source, load_sources, sync_sources


@dataclass(frozen=True)
class CollectResult:
    run_id: int
    status: str
    inserted_count: int
    merged_count: int
    error_count: int
    started_source_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def _tokens(title):
    title = unicodedata.normalize("NFKC", title).lower()
    tokens = set(re.findall(r"[a-z0-9]+", title))
    for run in re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+", title):
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _merge_event(conn, event_id):
    event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    tokens = _tokens(event["headline"])
    if not tokens or event["published_at"] is None:
        return False
    candidates = conn.execute(
        """SELECT id,headline FROM events WHERE id<>? AND domain=?
        AND ABS(unixepoch(published_at)-unixepoch(?))<=259200 ORDER BY id""",
        (event_id, event["domain"], event["published_at"]),
    ).fetchall()
    for candidate in candidates:
        other = _tokens(candidate["headline"])
        if len(tokens & other) / len(tokens | other) < 0.78:
            continue
        survivor = candidate["id"]
        conn.execute("DELETE FROM event_articles WHERE event_id=?", (event_id,))
        conn.execute("INSERT INTO event_articles(event_id,article_id,is_primary) VALUES(?,?,0)",
                     (survivor, event["primary_article_id"]))
        conn.execute("DELETE FROM event_search WHERE rowid=?", (event_id,))
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        rebuild_event_search(conn, survivor)
        return True
    return False


def _sanitize(exc, credentials):
    message = str(exc)
    # Remove query/fragment data before credential replacement can damage
    # the URL scheme and prevent recognition. Cover absolute and relative URLs.
    message = re.sub(r"(?<!\S)([^\s?#]+)[?#][^\s]*", r"\1", message)
    for value in sorted(credentials, key=len, reverse=True):
        message = re.sub(re.escape(value), "[REDACTED]", message, flags=re.IGNORECASE)
    return " ".join(message.split())[:500]


def run_update(*, source_loader=load_sources, source_fetcher=fetch_source,
               clock=time.monotonic, target_seconds=120, hard_stop_seconds=150) -> CollectResult:
    started_at = clock()
    sources = list(source_loader())
    credentials = set()
    for source in sources:
        value = os.environ.get(source.get("api_key_env", ""), "").strip()
        if value:
            credentials.update((value, quote(value, safe=""), quote_plus(value, safe="")))
    conn = connect()
    inserted = merged = errors = started = successful = 0
    try:
        with conn:
            conn.execute("BEGIN")
            run_id = conn.execute("INSERT INTO update_runs(started_at,status) VALUES(?,'running')",
                                  (_now_beijing(),)).lastrowid
            sync_sources(conn, sources)
        for source in sources:
            state = conn.execute("SELECT enabled,etag,last_modified FROM sources WHERE id=?",
                                 (source["id"],)).fetchone()
            if not state["enabled"]:
                continue
            source_start = clock()
            if source_start - started_at >= min(target_seconds, hard_stop_seconds):
                with conn:
                    conn.execute("INSERT INTO source_runs(run_id,source_id,status) VALUES(?,?,'not_started_budget')",
                                 (run_id, source["id"]))
                continue
            started += 1
            try:
                fetch_state = {"etag": state["etag"], "last_modified": state["last_modified"]}
                if source_fetcher is fetch_source:
                    deadline = started_at + min(target_seconds, hard_stop_seconds)

                    def request_guard():
                        if clock() >= deadline:
                            raise TimeoutError("Collection request deadline reached")

                    fetch_state["_request_guard"] = request_guard
                result = source_fetcher(source, fetch_state)
                if result.status not in {"success", "not_modified", "skipped_missing_credential"}:
                    raise ValueError("Unexpected source fetch status")
                source_inserted = source_merged = 0
                with conn:
                    # Repository helpers must not commit any portion of this source early.
                    conn.execute("BEGIN")
                    if result.status == "success":
                        for item in result.items:
                            event_id, created = upsert_item(conn, item)
                            source_inserted += int(created)
                            if created:
                                source_merged += int(_merge_event(conn, event_id))
                    if result.status in {"success", "not_modified"}:
                        conn.execute("UPDATE sources SET etag=?,last_modified=?,last_success_at=? WHERE id=?",
                                     (result.etag, result.last_modified, _now_beijing(), source["id"]))
                    conn.execute("""INSERT INTO source_runs(run_id,source_id,status,item_count,elapsed_ms)
                                  VALUES(?,?,?,?,?)""",
                                 (run_id, source["id"], result.status, len(result.items),
                                  max(0, int((clock() - source_start) * 1000))))
                inserted += source_inserted
                merged += source_merged
                successful += int(result.status in {"success", "not_modified"})
            except Exception as exc:
                conn.rollback()
                with conn:
                    conn.execute("""INSERT INTO source_runs(run_id,source_id,status,error,elapsed_ms)
                                  VALUES(?,?,'failed',?,?)""",
                                 (run_id, source["id"], _sanitize(exc, credentials),
                                  max(0, int((clock() - source_start) * 1000))))
                errors += 1
        status = "success" if not errors else "partial" if successful else "failed"
        with conn:
            conn.execute("""UPDATE articles SET raw_metadata=NULL
                          WHERE datetime(fetched_at)<datetime('now','-90 days') AND raw_metadata IS NOT NULL""")
            conn.execute("""UPDATE update_runs SET finished_at=?,status=?,inserted_count=?,merged_count=?,error_count=?
                          WHERE id=?""", (_now_beijing(), status, inserted, merged, errors, run_id))
        return CollectResult(run_id, status, inserted, merged, errors, started)
    finally:
        conn.close()
