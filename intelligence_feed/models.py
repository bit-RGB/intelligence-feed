# intelligence_feed/models.py
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceItem:
    source_id: str
    domain: str
    source_name: str
    url: str
    title: str
    summary: str
    published_at: str | None


@dataclass(frozen=True)
class FetchResponse:
    status: int
    body: bytes
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


@dataclass(frozen=True)
class SourceFetchResult:
    status: str  # "success" | "not_modified" | "skipped_missing_credential"
    items: list
    etag: str | None = None
    last_modified: str | None = None
