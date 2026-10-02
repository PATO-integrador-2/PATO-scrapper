"""Crawler BFS respetuoso para sitios oficiales (allowlist + robots.txt + rate limit)."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.adapters.url_utils import (
    is_download_endpoint,
    is_file_url,
    is_in_allowed_domains,
    matches_keywords,
    normalize_url,
    resolve,
    resource_kind,
)
from app.domain.models import DiscoveredResource

log = logging.getLogger("adapters.crawler")


class Crawler:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        seed_urls: tuple[str, ...],
        allowed_domains: tuple[str, ...],
        source_code: str,
        source_name: str,
        max_depth: int = 2,
        max_pages: int = 200,
        rate_limit_seconds: float = 1.0,
        keywords: tuple[str, ...] = (),
        respect_robots: bool = True,
        same_domain_only: bool = True,
    ) -> None:
        self.client = client
        self.seed_urls = tuple(seed_urls)
        self.allowed_domains = tuple(d.lower() for d in allowed_domains)
        self.source_code = source_code
        self.source_name = source_name
        self.max_depth = max(1, int(max_depth))
        self.max_pages = max(1, int(max_pages))
        self.rate_limit_seconds = max(0.0, float(rate_limit_seconds))
        self.keywords = tuple(keywords)
        self.respect_robots = respect_robots
        self.same_domain_only = same_domain_only
        self._robots: dict[str, RobotFileParser | None] = {}
        self._visited: set[str] = set()

    async def _robots_for(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        robots_url = f"{origin}/robots.txt"
        try:
            response = await self.client.get(robots_url, timeout=10.0)
            if response.status_code >= 400:
                self._robots[origin] = None
                return None
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
            self._robots[origin] = parser
            return parser
        except Exception:  # noqa: BLE001
            self._robots[origin] = None
            return None

    async def _allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = await self._robots_for(url)
        if parser is None:
            return True
        user_agent = self.client.headers.get("User-Agent", "*")
        return parser.can_fetch(user_agent, url)

    def _in_allowed_domains(self, url: str) -> bool:
        return is_in_allowed_domains(url, self.allowed_domains)

    def _same_seed_domain(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(
            host == urlparse(seed).netloc.lower()
            or host.endswith("." + urlparse(seed).netloc.lower())
            for seed in self.seed_urls
        )

    async def crawl(self) -> list[DiscoveredResource]:
        discovered: dict[str, DiscoveredResource] = {}
        queue: deque[tuple[str, int]] = deque()

        for seed in self.seed_urls:
            if not self._in_allowed_domains(seed):
                log.warning("seed_outside_allowlist", extra={"extra_fields": {"seed": seed}})
                continue
            queue.append((normalize_url(seed), 0))

        pages_fetched = 0

        while queue and pages_fetched < self.max_pages:
            url, depth = queue.popleft()
            if url in self._visited:
                continue
            self._visited.add(url)

            if not self._in_allowed_domains(url):
                continue
            if self.same_domain_only and not self._same_seed_domain(url):
                continue
            if not await self._allowed_by_robots(url):
                log.info("blocked_by_robots", extra={"extra_fields": {"url": url}})
                continue

            if pages_fetched > 0 and self.rate_limit_seconds > 0:
                await asyncio.sleep(self.rate_limit_seconds)

            try:
                response = await self.client.get(url)
            except httpx.HTTPError as exc:
                log.warning(
                    "crawl_fetch_failed",
                    extra={"extra_fields": {"url": url, "error": str(exc)}},
                )
                continue

            pages_fetched += 1
            content_type = response.headers.get("content-type", "").lower()

            if is_file_url(url) or is_download_endpoint(url):
                kind = resource_kind(url, content_type)
                discovered.setdefault(
                    url,
                    DiscoveredResource(
                        url=url,
                        resource_type=kind,  # type: ignore[arg-type]
                        title=None,
                        source_code=self.source_code,
                        depth=depth,
                        discovered_from=None,
                    ),
                )
                continue

            if "html" not in content_type and not url.lower().endswith((".html", ".htm", "/")):
                kind = resource_kind(url, content_type)
                if kind not in {"other", "html"}:
                    discovered.setdefault(
                        url,
                        DiscoveredResource(
                            url=url,
                            resource_type=kind,  # type: ignore[arg-type]
                            title=None,
                            source_code=self.source_code,
                            depth=depth,
                            discovered_from=None,
                        ),
                    )
                continue

            if response.status_code >= 400:
                continue

            soup = BeautifulSoup(response.text, "html.parser")

            page_title = ""
            if soup.title and soup.title.string:
                page_title = soup.title.string.strip()

            for anchor in soup.find_all("a", href=True):
                href = str(anchor.get("href", "")).strip()
                target = resolve(str(response.url), href)
                if target is None:
                    continue
                target = normalize_url(target)

                if not self._in_allowed_domains(target):
                    continue
                if self.same_domain_only and not self._same_seed_domain(target):
                    continue

                anchor_title = " ".join(anchor.stripped_strings) or None

                if is_file_url(target) or is_download_endpoint(target):
                    if not matches_keywords(
                        target, anchor_title, page_title, keywords=self.keywords
                    ):
                        continue
                    kind = resource_kind(target)
                    discovered.setdefault(
                        target,
                        DiscoveredResource(
                            url=target,
                            resource_type=kind,  # type: ignore[arg-type]
                            title=anchor_title,
                            source_code=self.source_code,
                            depth=depth + 1,
                            discovered_from=url,
                        ),
                    )
                    continue

                if depth + 1 < self.max_depth and target not in self._visited:
                    queue.append((target, depth + 1))

        log.info(
            "crawl_finished",
            extra={
                "extra_fields": {
                    "source": self.source_code,
                    "pages_fetched": pages_fetched,
                    "discovered": len(discovered),
                }
            },
        )
        return list(discovered.values())