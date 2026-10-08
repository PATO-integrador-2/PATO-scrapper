from app.adapters.cepal import CepalAdapter
from app.adapters.dane import DaneAdapter
from app.adapters.web import OfficialWebAdapter


class GenericAdapter(OfficialWebAdapter):
    """
    Adaptador genérico para cualquier sitio web que no tenga un adaptador específico.
    Usa el dominio de la URL semilla como código y nombre.
    """
    def __init__(self, seed_urls, allowed_domains, client, **kwargs):
        # Usamos el dominio de la primera URL como nombre para identificarlo
        domain = "generic"
        if seed_urls:
            from urllib.parse import urlparse
            domain = urlparse(seed_urls[0]).netloc or "generic"
        
        super().__init__(
            code=domain,
            name=domain,
            organization=domain,
            seed_urls=seed_urls,
            allowed_domains=allowed_domains,
            client=client,
            **kwargs
        )


def build_adapters(settings, client):
    """
    Construye los adaptadores basándose en las URLs semilla.
    - Si la URL es de cepal.org, usa CepalAdapter.
    - Si la URL es de dane.gov.co, usa DaneAdapter.
    - Para cualquier otra URL, usa GenericAdapter.
    """
    crawl_kwargs = {
        "max_depth": settings.discovery_max_depth,
        "max_pages": settings.discovery_max_pages,
        "rate_limit_seconds": settings.discovery_rate_limit_seconds,
        "keywords": settings.discovery_keywords,
        "respect_robots": settings.discovery_respect_robots,
        "same_domain_only": settings.discovery_same_domain_only,
    }

    adapters = []
    processed_urls = set()

    for url in settings.discovery_seed_urls:
        if url in processed_urls:
            continue
        processed_urls.add(url)

        if "cepal.org" in url:
            adapters.append(CepalAdapter((url,), settings.allowed_domains, client, **crawl_kwargs))
        elif "dane.gov.co" in url:
            adapters.append(DaneAdapter((url,), settings.allowed_domains, client, **crawl_kwargs))
        else:
            # Si es un dominio nuevo, usamos el adaptador genérico
            adapters.append(GenericAdapter((url,), settings.allowed_domains, client, **crawl_kwargs))
    
    return adapters