from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx

from app.adapters.cepal_api import CepalStatClient, normalize_indicator_records
from app.core.config import Settings
from app.db.repository import IngestionRepository
from app.services.storage import RawObjectStorage


class CepalAPIIngestionService:
    """Ingesta API-first de CEPALSTAT con almacenamiento RAW e idempotencia."""

    def __init__(self, settings: Settings, repo: IngestionRepository) -> None:
        self.settings = settings
        self.repo = repo

    async def ingest_indicator(
        self,
        indicator_id: int,
        *,
        lang: str = "es",
        members: str | None = None,
        include_all_dimensions: int = 1,
        path: int = 0,
    ) -> dict:
        headers = {"User-Agent": self.settings.user_agent, "Accept": "application/json"}
        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            api = CepalStatClient(client, self.settings.cepalstat_api_base_url)
            payload = await api.indicator_data(
                indicator_id,
                lang=lang,
                members=members,
                include_all_dimensions=include_all_dimensions,
                path=path,
            )

        body = payload.get("body") or {}
        records = normalize_indicator_records(indicator_id, body)
        raw_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        api_url = f"{self.settings.cepalstat_api_base_url.rstrip('/')}/indicator/{indicator_id}/data"

        source_id = await self.repo.upsert_source(
            "CEPALSTAT",
            "CEPALSTAT Open Data API",
            "Comisión Económica para América Latina y el Caribe",
            self.settings.cepalstat_api_base_url,
            "CepalStatAPI",
        )
        run_id = await self.repo.create_run(source_id, "cepal_api")
        resource_id = await self.repo.add_api_resource(
            run_id,
            source_id,
            api_url,
            f"CEPALSTAT indicator {indicator_id}",
        )

        storage = RawObjectStorage(
            self.settings.object_storage_endpoint,
            self.settings.object_storage_access_key,
            self.settings.object_storage_secret_key,
            self.settings.object_storage_bucket_raw,
            self.settings.object_storage_region,
        )
        stored = storage.put(raw_bytes, api_url + f"?indicator={indicator_id}", "application/json")
        await self.repo.mark_downloaded(resource_id, stored)

        etl_id = await self.repo.create_etl_run(run_id, "parse")
        staging_id = await self.repo.create_staging_resource(
            resource_id, etl_id, f"cepalstat-api-v1-indicator-{indicator_id}"
        )
        staged = []
        for number, record in enumerate(records, start=1):
            normalized = dict(record)
            row_hash = hashlib.sha256(
                json.dumps(record, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            status = "valid" if record.get("value") is not None else "invalid"
            errors = [] if status == "valid" else ["missing_value"]
            staged.append((number, row_hash, record, normalized, status, errors))
        await self.repo.insert_staging_records(staging_id, staged)
        await self.repo.finish_staging_resource(
            staging_id, len(records), sum(1 for x in records if x.get("value") is not None),
            sum(1 for x in records if x.get("value") is None), "loaded"
        )
        await self.repo.finish_etl_stage(
            etl_id, 1, len(records), sum(1 for x in records if x.get("value") is None), "completed",
            {"indicator_id": indicator_id, "sha256": digest, "api_url": api_url},
        )
        await self.repo.finish_run(
            run_id,
            "completed",
            {
                "adapter": "CepalStatAPI",
                "indicator_id": indicator_id,
                "records": len(records),
                "sha256": digest,
                "object_key": stored.object_key,
                "raw_bytes": len(raw_bytes),
                "retrieved_at": datetime.now(UTC).isoformat(),
            },
        )
        return {
            "run_id": str(run_id),
            "indicator_id": indicator_id,
            "records": len(records),
            "raw_sha256": digest,
            "object_key": stored.object_key,
            "dataset_code": f"cepalstat_indicator_{indicator_id}",
            "idempotent": True,
        }
