from app.adapters.cepal import CepalAdapter
from app.adapters.dane import DaneAdapter


def build_adapters(settings, client):
    cepal = tuple(u for u in settings.discovery_seed_urls if "cepal.org" in u)
    dane = tuple(u for u in settings.discovery_seed_urls if "dane.gov.co" in u)

    crawl_kwargs = {
        "max_depth": settings.discovery_max_depth,
        "max_pages": settings.discovery_max_pages,
        "rate_limit_seconds": settings.discovery_rate_limit_seconds,
        "keywords": settings.discovery_keywords,
        "respect_robots": settings.discovery_respect_robots,
        "same_domain_only": settings.discovery_same_domain_only,
    }

    return [
        CepalAdapter(cepal, settings.allowed_domains, client, **crawl_kwargs),
        DaneAdapter(dane, settings.allowed_domains, client, **crawl_kwargs),
    ]