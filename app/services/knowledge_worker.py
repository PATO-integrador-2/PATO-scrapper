"""
Worker que consume la cola ingestion.pending_knowledge_ingestion
y llama al endpoint /knowledge/ingest por cada PDF pendiente.

Diseño:
- Corre como tarea de fondo dentro del proceso FastAPI (por defecto),
  arrancada desde el lifespan en main.py.
- Toma items en lotes con FOR UPDATE SKIP LOCKED: se pueden correr
  varias instancias del worker en paralelo sin pisarse.
- Reintenta items fallidos hasta `max_attempts` veces.
- Recupera items que quedaron 'processing' por crash (reset_stale).
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.db.repository import IngestionRepository

log = logging.getLogger("services.knowledge_worker")

# Textos de anchor genéricos que no aportan información como título.
GENERIC_TITLES = {
    "descargar", "download", "ver", "ver más", "ver mas",
    "aquí", "aqui", "click aquí", "click aqui", "leer más", "leer mas",
}


class KnowledgeIngestionWorker:
    def __init__(self, settings: Settings, repo: IngestionRepository) -> None:
        self.settings = settings
        self.repo = repo
        self._stop = asyncio.Event()

    async def run_once(self, batch: int | None = None) -> dict:
        """
        Procesa un lote de la cola. Devuelve un resumen:
        {claimed, ok, failed}.
        """
        batch = batch or self.settings.knowledge_worker_batch_size

        # Recupera items colgados en 'processing' antes de reclamar nuevos.
        recovered = await self.repo.reset_stale_knowledge_processing(stale_minutes=15)
        if recovered:
            log.warning(
                "knowledge_worker_recovered_stale",
                extra={"extra_fields": {"recovered": recovered}},
            )

        rows = await self.repo.claim_pending_knowledge(
            batch=batch,
            max_attempts=self.settings.knowledge_worker_max_attempts,
        )
        if not rows:
            return {"claimed": 0, "ok": 0, "failed": 0}

        ok = failed = 0
        timeout = httpx.Timeout(self.settings.knowledge_ingest_timeout, connect=10.0)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for row in rows:
                # Si el título es genérico o vacío, usa el nombre del archivo
                # como título. Los anchors del DANE suelen decir "Descargar".
                title = (row["title"] or "").strip()
                if not title or title.lower() in GENERIC_TITLES:
                    filename = urlparse(row["pdf_url"]).path.split("/")[-1]
                    if filename:
                        title = filename

                try:
                    resp = await client.post(
                        self.settings.knowledge_ingest_url,
                        json={
                            "url": row["pdf_url"],
                            "title": title,
                            "organization": None,
                        },
                    )
                    if resp.status_code >= 400:
                        raise RuntimeError(
                            f"knowledge_ingest_http_{resp.status_code}: {resp.text[:300]}"
                        )
                    document_id = None
                    try:
                        document_id = resp.json().get("document_id")
                    except Exception:  # noqa: BLE001
                        pass
                    await self.repo.mark_knowledge_ingested(row["id"], document_id)
                    ok += 1
                    log.info(
                        "knowledge_worker_item_ok",
                        extra={
                            "extra_fields": {
                                "id": row["id"],
                                "pdf_url": row["pdf_url"],
                                "title": title,
                                "attempts": row["attempts"],
                                "document_id": document_id,
                            }
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    await self.repo.mark_knowledge_failed(row["id"], str(exc))
                    log.warning(
                        "knowledge_worker_item_failed",
                        extra={
                            "extra_fields": {
                                "id": row["id"],
                                "pdf_url": row["pdf_url"],
                                "attempts": row["attempts"],
                                "error": str(exc),
                            }
                        },
                    )

        return {"claimed": len(rows), "ok": ok, "failed": failed}

    async def run_forever(self, interval: float | None = None) -> None:
        """
        Bucle infinito. Se detiene cuando se llama a `stop()`.
        Pensado para correr como asyncio.Task dentro del lifespan.
        """
        interval = interval or self.settings.knowledge_worker_interval_seconds
        log.info(
            "knowledge_worker_started",
            extra={"extra_fields": {"interval_seconds": interval}},
        )
        while not self._stop.is_set():
            try:
                summary = await self.run_once()
                if summary["claimed"]:
                    log.info(
                        "knowledge_worker_cycle",
                        extra={"extra_fields": summary},
                    )
            except Exception:  # noqa: BLE001
                log.exception("knowledge_worker_cycle_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        log.info("knowledge_worker_stopped")

    def stop(self) -> None:
        self._stop.set()