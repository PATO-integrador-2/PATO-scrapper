from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.db.repository import IngestionRepository
from app.etl.cleaner import clean_row, row_hash
from app.etl.parsers import parse_bytes
from app.etl.semantic import canonical_fields
from app.services.storage import RawObjectStorage

log = logging.getLogger(__name__)


class ETLProcessor:
    def __init__(self, repo: IngestionRepository, storage: RawObjectStorage, settings: Any) -> None:
        self.repo = repo
        self.storage = storage
        self.settings = settings

    async def process_run(self, run_id: UUID, limit: int = 20) -> dict[str, Any]:
        resources = await self.repo.get_downloaded(run_id, limit)
        total = {"resources": 0, "records_read": 0, "records_cleaned": 0, "records_rejected": 0, "failed": 0}
        for resource in resources:
            total["resources"] += 1
            try:
                result = await self.process_resource(run_id, resource)
                for key in ("records_read", "records_cleaned", "records_rejected"):
                    total[key] += result[key]
            except Exception as exc:  # noqa: BLE001
                total["failed"] += 1
                log.exception("etl_resource_failed", extra={"extra_fields": {"resource_id": resource["id"]}})
                await self.repo.mark_error(run_id, resource["id"], "ETL_PROCESS_ERROR", str(exc))
        status = "completed" if total["failed"] == 0 else ("partial" if total["resources"] else "failed")
        await self.repo.finish_etl_run(run_id, status, total)
        return {"run_id": str(run_id), "status": status, **total}

    async def process_resource(self, run_id: UUID, resource: dict[str, Any]) -> dict[str, int]:
        resource_type = resource["resource_type"]
        if resource_type in {"pdf", "html", "other"}:
            await self.repo.mark_resource_status(resource["id"], "skipped")
            await self.repo.add_etl_error(run_id, resource["id"], "NOT_TABULAR", f"Resource type {resource_type} is handled by another pipeline")
            return {"records_read": 0, "records_cleaned": 0, "records_rejected": 0}

        download = await self.repo.get_latest_download(resource["id"])
        if not download:
            raise ValueError("resource_has_no_download")
        raw = self.storage.get_object(download["storage_bucket"], download["object_key"])
        etl_id = await self.repo.create_etl_run(run_id, "parse")
        staging_id = await self.repo.create_staging_resource(resource["id"], etl_id, self._parser_name(resource_type))

        read = cleaned = rejected = 0
        seen_hashes: set[str] = set()
        batch: list[tuple[int, str, dict[str, Any], dict[str, Any], str, list[str]]] = []
        for row_number, raw_row in enumerate(parse_bytes(raw, resource_type, download["file_name"]), start=1):
            read += 1
            cleaned_row, errors = clean_row(raw_row)
            cleaned_row["_canonical"] = canonical_fields(cleaned_row)
            digest = row_hash(raw_row)
            if digest in seen_hashes:
                errors.append("DUPLICATE_ROW")
            seen_hashes.add(digest)
            status = "rejected" if errors else "valid"
            if errors:
                rejected += 1
            else:
                cleaned += 1
            batch.append((row_number, row_hash(raw_row), raw_row, cleaned_row, status, errors))
            if len(batch) >= 500:
                await self.repo.insert_staging_records(staging_id, batch)
                batch.clear()
        if batch:
            await self.repo.insert_staging_records(staging_id, batch)

        status = "completed" if rejected == 0 else "partial"
        await self.repo.finish_etl_stage(etl_id, read, cleaned, rejected, status, {"resource_type": resource_type})
        await self.repo.finish_staging_resource(staging_id, read, cleaned, rejected, status)
        await self.repo.mark_resource_status(resource["id"], "parsed")
        return {"records_read": read, "records_cleaned": cleaned, "records_rejected": rejected}

    @staticmethod
    def _parser_name(resource_type: str) -> str:
        return {"csv": "csv", "xlsx": "excel", "xls": "excel", "json": "json", "zip": "zip-tabular"}.get(resource_type, "unknown")
