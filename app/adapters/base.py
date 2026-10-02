from abc import ABC, abstractmethod
from urllib.parse import urlparse

from app.domain.models import DiscoveredResource


class SourceAdapter(ABC):
    code: str
    name: str
    organization: str
    base_url: str
    priority: int = 1

    @property
    def allowed_domains(self) -> tuple[str, ...]:
        return (urlparse(self.base_url).netloc,)

    @abstractmethod
    async def discover(self) -> list[DiscoveredResource]:
        raise NotImplementedError
