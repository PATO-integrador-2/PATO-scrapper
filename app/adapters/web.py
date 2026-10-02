"""Adapter genérico para páginas oficiales que publican enlaces de archivos."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.domain.models import DiscoveredResource

_FILE_EXTENSIONS = {
    ".csv": "csv", ".xlsx": "xlsx", ".xls": "xls", ".json": "json",
    ".pdf": "pdf", ".zip": "zip", ".html": "html",
}


def _resource_type(url: str, content_type: str | None = None) -> str:
    path = urlparse(url).path.lower()
    for ext, kind in _FILE_EXTENSIONS.items():
        if path.endswith(ext):
            return kind
    ctype = (content_type or "").lower()
    if "pdf" in ctype:
        return "pdf"
    if "json" in ctype:
        return "json"
    if "spreadsheet" in ctype or "excel" in ctype:
        return "xlsx"
    if "csv" in ctype:
        return "csv"
    if "html" in ctype:
        return "html"
    return "other"


class OfficialWebAdapter:
    """Descubre recursos desde páginas conocidas, respetando una allowlist de dominios."""

    def __init__(
        self,
        code: str,
        name: str,
        organization: str,
        seed_urls: tuple[str, ...],
        allowed_domains: tuple[str, ...],
        client: httpx.AsyncClient,
        *,
        max_depth: int = 2,
        max_pages: int = 200,
        rate_limit_seconds: float = 1.0,
        keywords: tuple[str, ...] = (),
        respect_robots: bool = True,
        same_domain_only: bool = True,
    ) -> None:
        self.code = code
        self.name = name
        self.organization = organization
        self.seed_urls = seed_urls
        self.allowed_domains = tuple(d.lower() for d in allowed_domains)
        self.client = client
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.rate_limit_seconds = rate_limit_seconds
        self.keywords = keywords
        self.respect_robots = respect_robots
        self.same_domain_only = same_domain_only

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)

    async def discover(self) -> list[DiscoveredResource]:
        # Import diferido para evitar ciclo (crawler importa _resource_type de aquí).
        from app.adapters.crawler import Crawler

        if not self.seed_urls:
            return []

        crawler = Crawler(
            client=self.client,
            seed_urls=self.seed_urls,
            allowed_domains=self.allowed_domains,
            source_code=self.code,
            source_name=self.name,
            max_depth=self.max_depth,
            max_pages=self.max_pages,
            rate_limit_seconds=self.rate_limit_seconds,
            keywords=self.keywords,
            respect_robots=self.respect_robots,
            same_domain_only=self.same_domain_only,
        )
        return await crawler.crawl()