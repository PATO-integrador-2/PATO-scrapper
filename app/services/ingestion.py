from __future__ import annotations

import logging
from uuid import UUID

import httpx

from app.adapters.sources import build_adapters
from app.api.ingestion import DiscoverRequest
from app.core.config import Settings
from app.db.repository import IngestionRepository
from app.services.downloader import Downloader
from app.services.storage import RawObjectStorage
from app.etl.processor import ETLProcessor

log = logging.getLogger(__name__)


class IngestionService:
    def __init__(self, settings: Settings, repo: IngestionRepository) -> None:
        self.settings = settings
        self.repo = repo

    async def discover(self, overrides: "DiscoverRequest" | None = None) -> dict:
        headers = {"User-Agent": self.settings.user_agent}
        timeout = httpx.Timeout(
            self.settings.discovery_request_timeout,
            connect=10.0,
            read=self.settings.discovery_request_timeout,
        )

        seed_urls = tuple(overrides.seed_urls) if overrides and overrides.seed_urls else self.settings.discovery_seed_urls
        allowed_domains = tuple(overrides.allowed_domains) if overrides and overrides.allowed_domains else self.settings.allowed_domains
        keywords = tuple(overrides.keywords) if overrides and overrides.keywords else self.settings.discovery_keywords
        max_depth = overrides.max_depth if overrides and overrides.max_depth is not None else self.settings.discovery_max_depth
        max_pages = overrides.max_pages if overrides and overrides.max_pages is not None else self.settings.discovery_max_pages

        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            adapters = build_adapters(
                settings=self.settings,
                client=client,
                seed_urls=seed_urls,
                allowed_domains=allowed_domains,
                keywords=keywords,
                max_depth=max_depth,
                max_pages=max_pages
            )
            result = {"sources": {}}
            for adapter in adapters:
                if not adapter.seed_urls:
                    continue
                base_url = adapter.seed_urls[0]
                source_id = await self.repo.upsert_source(
                    adapter.code, adapter.name, adapter.organization, base_url, "OfficialWebAdapter"
                )
                run_id = await self.repo.create_run(source_id, "manual_discovery")
                try:
                    log.info(
                        "crawl_started",
                        extra={
                            "extra_fields": {
                                "source": adapter.code,
                                "seeds": list(adapter.seed_urls),
                                "max_depth": self.settings.discovery_max_depth,
                                "max_pages": self.settings.discovery_max_pages,
                            }
                        },
                    )
                    resources = await adapter.discover()
                    count = await self.repo.add_resources(run_id, source_id, resources)

                    # ------------------------------------------------------------
                    # Encola PDFs descubiertos para el pipeline de knowledge.
                    # ------------------------------------------------------------
                    queued = 0
                    if self.settings.knowledge_ingest_enabled:
                        pdfs = [r for r in resources if r.resource_type == "pdf"]
                        if pdfs:
                            queued = await self.repo.enqueue_knowledge_ingestion(
                                run_id=run_id,
                                source_code=adapter.code,
                                items=[(r.url, r.title) for r in pdfs],
                            )
                            log.info(
                                "knowledge_queue_filled",
                                extra={
                                    "extra_fields": {
                                        "source": adapter.code,
                                        "pdfs_found": len(pdfs),
                                        "queued_new": queued,
                                    }
                                },
                            )

                    await self.repo.finish_run(
                        run_id, "completed",
                        {
                            "adapter": "OfficialWebAdapter",
                            "discovered": count,
                            "pdfs_queued": queued,
                        },
                    )
                    log.info(
                        "crawl_completed",
                        extra={
                            "extra_fields": {
                                "source": adapter.code,
                                "discovered": count,
                                "pdfs_queued": queued,
                            }
                        },
                    )
                    result["sources"][adapter.code] = {
                        "run_id": str(run_id),
                        "discovered": count,
                        "pdfs_queued": queued,
                    }
                except Exception as exc:  # noqa: BLE001
                    log.exception("discovery_failed", extra={"extra_fields": {"source": adapter.code}})
                    await self.repo.mark_error(run_id, None, "DISCOVERY_ERROR", str(exc))
                    await self.repo.finish_run(run_id, "failed")
                    result["sources"][adapter.code] = {"run_id": str(run_id), "error": str(exc)}
        return result

    async def download_run(self, run_id: str, limit: int = 20) -> dict:
        headers = {"User-Agent": self.settings.user_agent}
        storage = RawObjectStorage(
            self.settings.object_storage_endpoint, self.settings.object_storage_access_key,
            self.settings.object_storage_secret_key, self.settings.object_storage_bucket_raw,
        )
        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout, follow_redirects=True, headers=headers
        ) as client:
            downloader = Downloader(
                client, storage, self.settings.max_download_mb * 1024 * 1024, self.settings.allowed_domains
            )
            resources = await self.repo.get_discovered(run_id, limit)
            downloaded = 0
            errors = 0
            for resource in resources:
                try:
                    result = await downloader.download(resource["url"])
                    await self.repo.mark_downloaded(resource["id"], result)
                    downloaded += 1
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    await self.repo.mark_error(
                        UUID(run_id), resource["id"], "DOWNLOAD_ERROR",
                        str(exc), {"url": resource["url"]}
                    )
            status = "completed" if errors == 0 else ("partial" if downloaded else "failed")
            await self.repo.finish_run(
                UUID(run_id), status,
                {"downloaded": downloaded, "errors": errors}
            )
            return {"run_id": run_id, "downloaded": downloaded, "errors": errors}

    async def process_run(self, run_id: str, limit: int = 20) -> dict:
        storage = RawObjectStorage(
            self.settings.object_storage_endpoint, self.settings.object_storage_access_key,
            self.settings.object_storage_secret_key, self.settings.object_storage_bucket_raw,
        )
        processor = ETLProcessor(self.repo, storage, self.settings)
        return await processor.process_run(UUID(run_id), limit)

    async def publish_run(self, run_id: str, limit: int = 5000) -> dict:
        records = await self.repo.get_valid_staging(UUID(run_id), limit)
        data_repo = getattr(self, "data_repo", None)
        if data_repo is None:
            raise RuntimeError("data_repository_not_configured")

        required_keys = {
            "methodology_code", "dataset_code", "indicator_code",
            "year", "value", "geography_code", "dataset_version",
        }

        transformed: list[dict] = []
        for r in records:
            # Caso 1: CEPALSTAT u otras fuentes ya producen el formato que
            # import_batch necesita. Pasan directo.
            if required_keys.issubset(r.keys()):
                row = dict(r)
                # Si no hay geo, saltamos (no podemos insertar sin geo).
                if not row.get("geography_code"):
                    continue
                # Normalizar year a int (por si viene como string).
                try:
                    row["year"] = int(row["year"])
                except (TypeError, ValueError):
                    continue
                transformed.append(row)
                continue

            # Caso 2: pipeline tabular (Excel DANE). Los registros traen
            # _canonical con year/ipm/region. Mapeamos a MPI.
            canonical = r.get("_canonical") or {}
            year = canonical.get("year")
            ipm = canonical.get("ipm")
            if year is None or ipm is None:
                continue
            geo = (
                canonical.get("region")
                or canonical.get("department")
                or canonical.get("country")
                or "nacional"
            )
            geo_code = str(geo).strip().lower().replace(" ", "_")[:100]
            try:
                year_int = int(year)
                value_float = float(ipm)
            except (TypeError, ValueError):
                continue
            transformed.append({
                "methodology_code": "DANE_IPM",
                "dataset_code": "DANE_IPM",
                "dataset_version": f"run-{run_id[:8]}",
                "indicator_code": "MPI",
                "geography_code": geo_code,
                "year": year_int,
                "period": str(year_int),
                "value": value_float,
                "unit": "percentage",
            })

        ok, bad = await data_repo.import_batch(transformed)
        return {
            "run_id": run_id,
            "records_sent": len(transformed),
            "published": ok,
            "rejected": bad,
        }