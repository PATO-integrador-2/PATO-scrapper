"""Utilidades puras para normalización y filtrado de URLs durante el crawl."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse, urljoin, parse_qsl, urlencode

from app.adapters.web import _resource_type


_FILE_EXTENSIONS = {".csv", ".xlsx", ".xls", ".json", ".pdf", ".zip"}


def normalize_url(url: str) -> str:
    """
    Normaliza una URL para deduplicación y para que sea descargable:
    - colapsa backslashes a forward slashes (bug de Windows)
    - elimina el fragmento (#...)
    - ordena los query params
    - elimina query params de tracking comunes
    - URL-encodea caracteres inseguros (espacios, tildes, etc.) en el path y el query
    """
    from urllib.parse import quote

    url = url.strip()

    # 1. Reemplazar backslashes por forward slashes (Windows path bug)
    #    Solo en la parte "path" para no romper esquemas raros.
    url = url.replace("\\", "/")
    # Colapsar múltiples slashes seguidos (excepto el `//` del esquema)
    url = re.sub(r"(?<!:)//+", "/", url)
    # Colapsar slashes generados por backslashes en el path
    url = re.sub(r"/{2,}", "/", url)

    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()

    # 2. Query params: quitar tracking, ordenar
    TRACKING_PARAMS = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "sessionid", "sid",
    }
    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))

    # 3. Path: normalizar slashes y encodear caracteres inseguros
    path = parsed.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # URL-encodear solo lo que hace falta, sin tocar `/` ni caracteres ya válidos
    # `safe="/"` mantiene los slashes; el resto (espacios, tildes, etc.) se codifica
    path = quote(path, safe="/:@!$&'()*+,;=-._~")

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