"""Run one intelligence-feed collection batch without an LLM."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Supports direct execution from both the source tree and installed plugin tree.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from intelligence_feed.service import run_update  # noqa: E402


def main(collector=run_update) -> int:
    try:
        result = collector()
    except Exception:
        print("intelligence-feed update failed", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
