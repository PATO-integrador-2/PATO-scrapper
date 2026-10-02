"""Utilidades puras para normalización y filtrado de URLs durante el crawl."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse, urljoin, parse_qsl, urlencode

from app.adapters.web import _resource_type


_FILE_EXTENSIONS = {".csv", ".xlsx", ".xls", ".json", ".pdf", ".zip"}


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()

    TRACKING_PARAMS = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "sessionid", "sid",
    }
    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))

    path = parsed.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, "", query, ""))


def same_domain(url: str, domain: str) -> bool:
    host = urlparse(url).netloc.lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def is_in_allowed_domains(url: str, allowed_domains: tuple[str, ...]) -> bool:
    return any(same_domain(url, d) for d in allowed_domains)


def is_file_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _FILE_EXTENSIONS)


def is_download_endpoint(url: str) -> bool:
    import re
    return bool(re.search(r"/(download|descarga)(/|$)", urlparse(url).path, re.I))


def resource_kind(url: str, content_type: str | None = None) -> str:
    return _resource_type(url, content_type)


def matches_keywords(*texts: str | None, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return True

    import unicodedata

    def _norm(s: str) -> str:
        s = str(s or "").lower()
        return "".join(
            c for c in unicodedata.normalize("NFD", s)
            if unicodedata.category(c) != "Mn"
        )

    haystack = " ".join(_norm(t) for t in texts if t)
    return any(_norm(k) in haystack for k in keywords)


def resolve(base: str, href: str) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    try:
        return urljoin(base, href)
    except ValueError:
        return None