# intelligence_feed/paths.py
import os
from pathlib import Path


def data_dir() -> Path:
    override = os.getenv("INTELLIGENCE_FEED_DATA_DIR")
    root = Path(override) if override else Path(os.getenv("LOCALAPPDATA", Path.home())) / "hermes" / "data" / "intelligence-feed"
    root.mkdir(parents=True, exist_ok=True)
    return root


def database_path() -> Path:
    return data_dir() / "intelligence.db"
