"""Hermes Cron launcher for the installed intelligence-feed plugin."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def installed_plugin_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes" / "plugins" / "intelligence-feed"


def _load_collector():
    root = installed_plugin_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from intelligence_feed.service import run_update
    return run_update


def main(collector=None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = (collector or _load_collector())()
    except Exception:
        print("intelligence-feed update failed", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
