from __future__ import annotations
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
import json
import math
import asyncpg
from app.domain.models import DiscoveredResource, DownloadResult


def _json_safe(obj):
    """
    Convierte NaN, +Inf y -Inf a None recursivamente para que
    json.dumps() genere JSON estrictamente válido (Postgres rechaza
    tokens 'NaN'/'Infinity' en columnas jsonb).
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, Decimal):
        if obj.is_nan() or obj.is_infinite():
            return None
        return float(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _dumps(obj) -> str:
    return json.dumps(_json_safe(obj), default=str, ensure_ascii=False)


class IngestionRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def upsert_source(self, code, name, organization, base_url, adapter) -> int:
        row = await self.pool.fetchrow(
            '''INSERT INTO ingestion.sources(code,name,base_url,adapter) VALUES($1,$2,$3,$4)
            ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,base_url=EXCLUDED.base_url,adapter=EXCLUDED.adapter,updated_at=now() RETURNING id''',
            code, name, base_url, adapter,
        )
        return int(row['id'])

    async def create_run(self, source_id: int | None, run_type='manual') -> UUID:
        row = await self.pool.fetchrow(
            'INSERT INTO ingestion.ingestion_runs(source_id,run_type) VALUES($1,$2) RETURNING id',
            source_id, run_type,
        )
        return row['id']

    async def add_resources(self, run_id: UUID, source_id: int, resources: list[DiscoveredResource]) -> int:
        count = 0
        async with self.pool.acquire() as c, c.transaction():
            for item in resources:
                r = await c.fetchrow(
                    '''INSERT INTO ingestion.discovered_resources(source_id,url,resource_type,title) VALUES($1,$2,$3,$4)
                    ON CONFLICT(source_id,url) DO UPDATE SET resource_type=EXCLUDED.resource_type,title=COALESCE(EXCLUDED.title,ingestion.discovered_resources.title)
                    RETURNING id''',
                    source_id, item.url, item.resource_type, item.title,
                )
                inserted = await c.execute(
                    '''INSERT INTO ingestion.run_resources(run_id,resource_id) VALUES($1,$2) ON CONFLICT DO NOTHING''',
                    run_id, r['id'],
                )
                count += int(inserted.endswith('1'))
        await self.pool.execute(
            'UPDATE ingestion.ingestion_runs SET discovered_count=$2 WHERE id=$1',
            run_id, count,
        )
        return count

    async def add_api_resource(self, run_id: UUID, source_id: int, url: str, title: str | None = None) -> int:
        row = await self.pool.fetchrow(
            """INSERT INTO ingestion.discovered_resources(source_id,url,resource_type,title)
               VALUES($1,$2,'json',$3)
               ON CONFLICT(source_id,url) DO UPDATE SET title=COALESCE(EXCLUDED.title, ingestion.discovered_resources.title)
               RETURNING id""",
            source_id, url, title,
        )
        resource_id = int(row["id"])
        await self.pool.execute(
            "INSERT INTO ingestion.run_resources(run_id,resource_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
            run_id, resource_id,
        )
        return resource_id

    async def get_discovered(self, run_id: UUID, limit=100):
        rows = await self.pool.fetch(
            '''SELECT dr.id,dr.url,dr.resource_type,dr.title FROM ingestion.discovered_resources dr JOIN ingestion.run_resources rr ON rr.resource_id=dr.id WHERE rr.run_id=$1 ORDER BY dr.id LIMIT $2''',
            run_id, limit,
        )
        return [dict(r) for r in rows]

    async def find_download_by_sha(self, sha256: str):
        row = await self.pool.fetchrow(
            'SELECT * FROM ingestion.downloads WHERE sha256=$1 LIMIT 1',
            sha256,
        )
        return dict(row) if row else None

    async def mark_downloaded(self, resource_id: int, result: DownloadResult) -> bool:
        async with self.pool.acquire() as c, c.transaction():
            existing = await c.fetchrow(
                'SELECT storage_bucket,object_key FROM ingestion.downloads WHERE sha256=$1 ORDER BY id LIMIT 1',
                result.sha256,
            )
            bucket, key = (
                (existing['storage_bucket'], existing['object_key'])
                if existing
                else (result.bucket, result.object_key)
            )
            # FIX: 9 columnas -> 9 placeholders ($1..$9). Antes había un $10 sobrante.
            inserted = await c.execute(
                '''INSERT INTO ingestion.downloads(resource_id,http_status,content_type,file_name,file_size,sha256,storage_bucket,object_key,downloaded_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(resource_id,sha256) DO NOTHING''',
                resource_id, result.status_code, result.content_type, result.file_name,
                result.file_size, result.sha256, bucket, key, result.downloaded_at,
            )
            await c.execute(
                'UPDATE ingestion.discovered_resources SET status=\'downloaded\' WHERE id=$1',
                resource_id,
            )
            return inserted.endswith('1')

    async def mark_error(self, run_id: UUID, resource_id: int | None, code: str, message: str, payload=None):
        etl = await self.pool.fetchrow(
            'INSERT INTO ingestion.etl_runs(ingestion_run_id,stage,status) VALUES($1,\'download\',\'failed\') RETURNING id',
            run_id,
        )
        await self.pool.execute(
            'INSERT INTO ingestion.etl_errors(etl_run_id,resource_id,error_code,message,payload) VALUES($1,$2,$3,$4,$5::jsonb)',
            etl['id'], resource_id, code, message, _dumps(payload or {}),
        )
        if resource_id:
            await self.pool.execute(
                'UPDATE ingestion.discovered_resources SET status=\'error\' WHERE id=$1',
                resource_id,
            )

    async def finish_run(self, run_id: UUID, status: str, metadata=None):
        await self.pool.execute(
            'UPDATE ingestion.ingestion_runs SET status=$2,finished_at=$3,metadata=$4::jsonb WHERE id=$1',
            run_id, status, datetime.now(UTC), _dumps(metadata or {}),
        )

    async def get_downloaded(self, run_id: UUID, limit=20):
        rows = await self.pool.fetch(
            '''SELECT dr.id,dr.url,dr.resource_type,dr.title,d.storage_bucket,d.object_key,d.file_name,d.sha256 FROM ingestion.discovered_resources dr JOIN ingestion.run_resources rr ON rr.resource_id=dr.id JOIN ingestion.downloads d ON d.resource_id=dr.id WHERE rr.run_id=$1 AND dr.status='downloaded' ORDER BY dr.id LIMIT $2''',
            run_id, limit,
        )
        return [dict(r) for r in rows]

    async def get_latest_download(self, resource_id: int):
        row = await self.pool.fetchrow(
            'SELECT * FROM ingestion.downloads WHERE resource_id=$1 ORDER BY downloaded_at DESC LIMIT 1',
            resource_id,
        )
        return dict(row) if row else None

    async def create_etl_run(self, ingestion_run_id: UUID, stage: str) -> UUID:
        row = await self.pool.fetchrow(
            'INSERT INTO ingestion.etl_runs(ingestion_run_id,stage) VALUES($1,$2) RETURNING id',
            ingestion_run_id, stage,
        )
        return row['id']

    async def create_staging_resource(self, resource_id: int, etl_run_id: UUID, parser: str) -> int:
        row = await self.pool.fetchrow(
            '''INSERT INTO ingestion.staging_resources(resource_id,etl_run_id,parser) VALUES($1,$2,$3) ON CONFLICT(resource_id,parser) DO UPDATE SET etl_run_id=EXCLUDED.etl_run_id RETURNING id''',
            resource_id, etl_run_id, parser,
        )
        return int(row['id'])

    async def insert_staging_records(self, staging_resource_id: int, records):
        if not records:
            return
        async with self.pool.acquire() as c, c.transaction():
            for n, h, raw, clean, status, errors in records:
                await c.execute(
                    '''INSERT INTO ingestion.staging_records(staging_resource_id,row_number,row_hash,data,normalized_data,quality_status,quality_errors) VALUES($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7::jsonb) ON CONFLICT(staging_resource_id,row_hash) DO UPDATE SET normalized_data=EXCLUDED.normalized_data,quality_status=EXCLUDED.quality_status,quality_errors=EXCLUDED.quality_errors''',
                    staging_resource_id, n, h,
                    _dumps(raw),
                    _dumps(clean),
                    status,
                    _dumps(errors),
                )

    async def finish_staging_resource(self, staging_id, read, cleaned, rejected, status):
        await self.pool.execute(
            'UPDATE ingestion.staging_resources SET records_read=$2,records_cleaned=$3,records_rejected=$4,status=$5 WHERE id=$1',
            staging_id, read, cleaned, rejected, status,
        )

    async def mark_resource_status(self, resource_id, status):
        await self.pool.execute(
            'UPDATE ingestion.discovered_resources SET status=$2 WHERE id=$1',
            resource_id, status,
        )

    async def add_etl_error(self, run_id, resource_id, code, message):
        await self.mark_error(run_id, resource_id, code, message)

    async def finish_etl_run(self, ingestion_run_id, status, metadata):
        await self.pool.execute(
            'UPDATE ingestion.etl_runs SET status=$2,finished_at=now(),metadata=$3::jsonb WHERE ingestion_run_id=$1 AND stage=\'parse\' AND status=\'running\'',
            ingestion_run_id, status, _dumps(metadata),
        )

    async def finish_etl_stage(self, etl_id, input_count, output_count, rejected_count, status, metadata):
        await self.pool.execute(
            'UPDATE ingestion.etl_runs SET status=$2,finished_at=now(),input_count=$3,output_count=$4,rejected_count=$5,metadata=$6::jsonb WHERE id=$1',
            etl_id, status, input_count, output_count, rejected_count, _dumps(metadata),
        )

    async def get_valid_staging(self, run_id: UUID, limit=5000):
        rows = await self.pool.fetch(
            '''SELECT sr.normalized_data FROM ingestion.staging_records sr JOIN ingestion.staging_resources sres ON sres.id=sr.staging_resource_id JOIN ingestion.etl_runs er ON er.id=sres.etl_run_id WHERE er.ingestion_run_id=$1 AND sr.quality_status='valid' ORDER BY sr.id LIMIT $2''',
            run_id, limit,
        )
        result = []
        for r in rows:
            data = r['normalized_data']
            if isinstance(data, str):
                data = json.loads(data)
            result.append(data)
        return result

    # =========================================================================
    # Cola de ingesta de PDFs hacia el pipeline de knowledge
    # =========================================================================

    async def enqueue_knowledge_ingestion(
        self,
        *,
        run_id: UUID,
        source_code: str,
        items: list[tuple[str, str | None]],
    ) -> int:
        """
        Encola PDFs descubiertos por el crawler. `items` es una lista de
        (pdf_url, title). Devuelve cuántos se insertaron nuevos (los ya
        presentes en la cola se ignoran por UNIQUE(pdf_url)).
        """
        if not items:
            return 0
        queued = 0
        async with self.pool.acquire() as c, c.transaction():
            for url, title in items:
                result = await c.execute(
                    """
                    INSERT INTO ingestion.pending_knowledge_ingestion
                        (pdf_url, title, source_code, run_id)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (pdf_url) DO NOTHING
                    """,
                    url, title, source_code, run_id,
                )
                queued += int(result.endswith("1"))
        return queued

    async def claim_pending_knowledge(self, batch: int, max_attempts: int) -> list[dict]:
        """
        Toma hasta `batch` items pendientes (o fallidos con intentos < max_attempts)
        y los marca como 'processing' de forma atómica. Usa FOR UPDATE SKIP LOCKED
        para que varios workers puedan correr en paralelo sin pisarse.
        """
        rows = await self.pool.fetch(
            """
            UPDATE ingestion.pending_knowledge_ingestion
            SET status = 'processing',
                attempts = attempts + 1,
                updated_at = now()
            WHERE id IN (
                SELECT id
                FROM ingestion.pending_knowledge_ingestion
                WHERE status = 'pending'
                   OR (status = 'failed' AND attempts < $2)
                ORDER BY created_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, pdf_url, title, attempts, source_code, run_id
            """,
            batch, max_attempts,
        )
        return [dict(r) for r in rows]

    async def mark_knowledge_ingested(self, item_id: int, document_id: str | None) -> None:
        await self.pool.execute(
            """
            UPDATE ingestion.pending_knowledge_ingestion
            SET status = 'done',
                document_id = $2,
                last_error = NULL,
                updated_at = now()
            WHERE id = $1
            """,
            item_id, document_id,
        )

    async def mark_knowledge_failed(self, item_id: int, error: str) -> None:
        await self.pool.execute(
            """
            UPDATE ingestion.pending_knowledge_ingestion
            SET status = 'failed',
                last_error = $2,
                updated_at = now()
            WHERE id = $1
            """,
            item_id, error[:1000],
        )

    async def reset_stale_knowledge_processing(self, stale_minutes: int = 15) -> int:
        """
        Recupera items que quedaron en 'processing' porque el worker murió
        a mitad de camino. Los devuelve a 'pending' para reintento.
        """
        result = await self.pool.execute(
            """
            UPDATE ingestion.pending_knowledge_ingestion
            SET status = 'pending',
                updated_at = now(),
                last_error = COALESCE(last_error, 'reset_stale')
            WHERE status = 'processing'
              AND updated_at < now() - ($1 || ' minutes')::interval
            """,
            str(stale_minutes),
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def knowledge_queue_stats(self) -> dict:
        rows = await self.pool.fetch(
            """
            SELECT status, COUNT(*) AS count
            FROM ingestion.pending_knowledge_ingestion
            GROUP BY status
            """
        )
        stats = {row["status"]: int(row["count"]) for row in rows}
        for key in ("pending", "processing", "done", "failed"):
            stats.setdefault(key, 0)
        stats["total"] = sum(stats.values())
        return stats

    async def list_pending_knowledge(self, limit: int = 100, status: str | None = None) -> list[dict]:
        if status:
            rows = await self.pool.fetch(
                """
                SELECT id, pdf_url, title, source_code, run_id, status, attempts,
                       last_error, document_id, created_at, updated_at
                FROM ingestion.pending_knowledge_ingestion
                WHERE status = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                status, limit,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT id, pdf_url, title, source_code, run_id, status, attempts,
                       last_error, document_id, created_at, updated_at
                FROM ingestion.pending_knowledge_ingestion
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]