from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.config import Settings
from app.services.ingestion import IngestionService
from app.services.cepal_api_ingestion import CepalAPIIngestionService


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/ingestion", tags=["ingestion"])

    def service(request: Request) -> IngestionService:
        svc = IngestionService(settings, request.app.state.ingestion_repo)
        svc.data_repo = request.app.state.data_repo
        return svc

    @router.post("/cepal/indicators/{indicator_id}")
    async def ingest_cepal_indicator(
        request: Request,
        indicator_id: int,
        lang: str = Query(default="es"),
        members: str | None = Query(default=None),
        include_all_dimensions: int = Query(default=1, ge=0, le=1),
        path: int = Query(default=0, ge=0, le=1),
    ) -> dict:
        try:
            svc = CepalAPIIngestionService(settings, request.app.state.ingestion_repo)
            return await svc.ingest_indicator(
                indicator_id, lang=lang, members=members,
                include_all_dimensions=include_all_dimensions, path=path,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail="cepal_api_ingestion_failed") from exc

    @router.post("/cepal/indicators")
    async def ingest_configured_cepal_indicators(request: Request) -> dict:
        results = []
        svc = CepalAPIIngestionService(settings, request.app.state.ingestion_repo)
        for indicator_id in settings.cepalstat_indicators:
            try:
                results.append(await svc.ingest_indicator(indicator_id))
            except Exception as exc:  # noqa: BLE001
                results.append({"indicator_id": indicator_id, "error": str(exc)})
        return {"results": results}

    @router.post("/discover")
    async def discover(request: Request) -> dict:
        return await service(request).discover()

    @router.post("/runs/{run_id}/download")
    async def download(request: Request, run_id: UUID, limit: int = Query(default=20, ge=1, le=100)) -> dict:
        try:
            return await service(request).download_run(str(run_id), limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="download_run_failed") from exc

    @router.post("/runs/{run_id}/process")
    async def process(request: Request, run_id: UUID, limit: int = Query(default=20, ge=1, le=100)) -> dict:
        try:
            return await service(request).process_run(str(run_id), limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="etl_process_failed") from exc

    @router.post("/runs/{run_id}/publish")
    async def publish(request: Request, run_id: UUID, limit: int = Query(default=5000, ge=1, le=20000)) -> dict:
        try:
            return await service(request).publish_run(str(run_id), limit)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger("api.ingestion").exception(
                "publish_failed_internal",
                extra={"extra_fields": {"run_id": str(run_id), "error": str(exc)}},
            )
            raise HTTPException(status_code=502, detail="publish_failed") from exc
        # ------------------------------------------------------------------
    # Cola de ingesta de PDFs hacia knowledge
    # ------------------------------------------------------------------

    @router.get("/knowledge/queue")
    async def knowledge_queue_stats(request: Request) -> dict:
        """Estadísticas de la cola de PDFs pendientes/fallidos/procesados."""
        return await request.app.state.ingestion_repo.knowledge_queue_stats()

    @router.get("/knowledge/queue/items")
    async def knowledge_queue_items(
        request: Request,
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict:
        """Lista items de la cola, filtrable por estado (pending/processing/done/failed)."""
        items = await request.app.state.ingestion_repo.list_pending_knowledge(
            limit=limit, status=status
        )
        return {"items": items, "count": len(items)}

    @router.post("/knowledge/queue/run")
    async def knowledge_queue_run_once(
        request: Request,
        batch: int = Query(default=10, ge=1, le=200),
    ) -> dict:
        """
        Fuerza un ciclo del worker (útil para testing o para procesar
        la cola manualmente sin esperar al intervalo).
        """
        worker = getattr(request.app.state, "knowledge_worker", None)
        if worker is None:
            raise HTTPException(status_code=503, detail="knowledge_worker_disabled")
        return await worker.run_once(batch=batch)

    @router.post("/knowledge/queue/retry-failed")
    async def knowledge_queue_retry_failed(request: Request) -> dict:
        """
        Resetea a 'pending' todos los items en estado 'failed' para
        forzar su reintento en el próximo ciclo.
        """
        pool = request.app.state.db_pool
        result = await pool.execute(
            """
            UPDATE ingestion.pending_knowledge_ingestion
            SET status = 'pending', attempts = 0, updated_at = now(), last_error = NULL
            WHERE status = 'failed'
            """
        )
        return {"reset": int(result.split()[-1]) if result else 0}

    return router


