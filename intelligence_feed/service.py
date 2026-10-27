"""Small synchronous application boundary; connections never escape a call."""
import logging
import os
from contextlib import closing
from urllib.parse import quote, quote_plus

from . import collector, db, repository, sources, translation

logger = logging.getLogger(__name__)


def configured_credentials():
    """Return raw and URL-encoded configured source credentials for redaction."""
    values = set()
    for source in sources.load_sources():
        env_name = source.get("api_key_env")
        value = os.environ.get(env_name, "").strip() if env_name else ""
        if value:
            values.update((value, quote(value, safe=""), quote_plus(value, safe="")))
    return values


def list_events(*, profile, domain, read, q, limit, cursor):
    with closing(db.connect()) as conn:
        return repository.list_events(conn, profile, domain, read, q, limit, cursor)


def counts(profile):
    with closing(db.connect()) as conn:
        return repository.counts(conn, profile)


def list_sources():
    with closing(db.connect()) as conn:
        return sources.list_source_settings(conn, sources.load_sources())


def set_source_enabled(source_id, value):
    with closing(db.connect()) as conn:
        return sources.set_source_enabled(conn, sources.load_sources(), source_id, value)


def set_read(profile, event_id, value):
    with closing(db.connect()) as conn:
        return repository.set_read(conn, profile, event_id, value)


def set_favorite(profile, event_id, value):
    with closing(db.connect()) as conn:
        return repository.set_favorite(conn, profile, event_id, value)


def latest_run():
    with closing(db.connect()) as conn:
        # One read transaction keeps the run and its sources in one snapshot.
        with conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM update_runs ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return {"run": None, "source_runs": []}
            sources = conn.execute(
                "SELECT * FROM source_runs WHERE run_id=? ORDER BY id", (row["id"],)
            ).fetchall()
            result = [dict(source) for source in sources]
            # Persisted diagnostics are not public data: older collector errors
            # may contain SQL, paths or credentials even after sanitization.
            for source in result:
                if source["error"] is not None:
                    source["error"] = "Source collection failed"
            return {"run": dict(row), "source_runs": result}


def run_update():
    # Collection remains authoritative. Translation is a best-effort local
    # post-processing phase and must never turn a successful collection into
    # a failed update.
    result = collector.run_update()
    try:
        with closing(db.connect()) as conn:
            translation.translate_pending(conn, limit=100)
    except Exception:
        logger.warning("Intelligence feed title translation failed")
    return result
