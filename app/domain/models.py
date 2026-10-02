from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ResourceType = Literal["csv", "xlsx", "xls", "json", "pdf", "html", "zip", "other"]


@dataclass(frozen=True)
class DiscoveredResource:
    url: str
    resource_type: ResourceType
    title: str | None = None
    source_code: str | None = None
    depth: int = 0
    discovered_from: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    url: str
    status_code: int
    content_type: str
    file_name: str
    file_size: int
    sha256: str
    bucket: str
    object_key: str
    downloaded_at: datetime