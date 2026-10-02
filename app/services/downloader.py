from __future__ import annotations

import httpx
from urllib.parse import urlparse

from app.domain.models import DownloadResult
from app.services.storage import RawObjectStorage


class Downloader:
    def __init__(
        self, client: httpx.AsyncClient, storage: RawObjectStorage, max_bytes: int,
        allowed_domains: tuple[str, ...]
    ) -> None:
        self.client = client
        self.storage = storage
        self.max_bytes = max_bytes
        self.allowed_domains = tuple(d.lower() for d in allowed_domains)

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)

    async def download(self, url: str) -> DownloadResult:
        if not self._allowed(url):
            raise ValueError("domain_not_allowed")
        async with self.client.stream("GET", url) as response:
            if not self._allowed(str(response.url)):
                raise ValueError("redirected_domain_not_allowed")
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > self.max_bytes:
                raise ValueError(f"resource_too_large:{content_length}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes(1024 * 1024):
                total += len(chunk)
                if total > self.max_bytes:
                    raise ValueError(f"resource_too_large:{total}")
                chunks.append(chunk)
            content = b"".join(chunks)
            return self.storage.put(content, str(response.url), response.headers.get("content-type", ""))
