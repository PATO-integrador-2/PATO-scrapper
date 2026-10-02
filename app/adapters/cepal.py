from app.adapters.web import OfficialWebAdapter


class CepalAdapter(OfficialWebAdapter):
    code = "CEPAL"
    name = "CEPAL"
    organization = "Comisión Económica para América Latina y el Caribe"

    def __init__(
        self,
        seed_urls,
        allowed_domains,
        client,
        *,
        max_depth: int = 2,
        max_pages: int = 200,
        rate_limit_seconds: float = 1.0,
        keywords: tuple[str, ...] = (),
        respect_robots: bool = True,
        same_domain_only: bool = True,
    ):
        super().__init__(
            self.code,
            self.name,
            self.organization,
            seed_urls,
            allowed_domains,
            client,
            max_depth=max_depth,
            max_pages=max_pages,
            rate_limit_seconds=rate_limit_seconds,
            keywords=keywords,
            respect_robots=respect_robots,
            same_domain_only=same_domain_only,
        )