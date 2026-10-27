# tests/conftest.py
import pytest

from intelligence_feed.db import connect


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_FEED_DATA_DIR", str(tmp_path))
    c = connect()
    yield c
    c.close()
