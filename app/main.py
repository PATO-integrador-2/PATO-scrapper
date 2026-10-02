from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
import asyncio
 
import asyncpg
from fastapi import FastAPI

from app.api.health import build_router as build_health_router
from app.api.ingestion import build_router as build_ingestion_router
from app.api.data import build_router as build_data_router
from app.api.knowledge import build_knowledge_router
from app.core import middleware

from contextlib import asynccontextmanager
from app.core.config import Settings, load_settings
from app.core.logging import configure_logging
from app.db.repository import IngestionRepository
from app.db.data_repository import DataRepository
from app.db.knowledge_repository import Repository as KnowledgeRepository
from app.services.knowledge_worker import KnowledgeIngestionWorker

SERVICE_NAME = 'data-platform-service'


def create_app(settings: Settings | None = None) -> FastAPI:
    
    settings = settings or load_settings(SERVICE_NAME)
    print(f"DEBUG allowed_domains = {settings.allowed_domains}")
    print(f"DEBUG discovery_seed_urls = {settings.discovery_seed_urls}")
    print(f"DEBUG db_dsn = {settings.db_dsn[:60]}...")
    print(f"DEBUG object_storage_endpoint = {settings.object_storage_endpoint}")  # ← temporal
    print(f"DEBUG object_storage_bucket_raw = {settings.object_storage_bucket_raw}")  # ← temporal
    configure_logging(settings.service_name, settings.version, settings.log_level)

    @asynccontextmanager
    async def lifespan(app):
        data_platform_pool = await asyncpg.create_pool(settings.db_dsn, min_size=1, max_size=8)
        knowledge_pool = await asyncpg.create_pool(settings.knowledge_db_dsn, min_size=1, max_size=5)

        app.state.db_pool = data_platform_pool
        app.state.ingestion_repo = IngestionRepository(data_platform_pool)
        app.state.data_pool = data_platform_pool
        app.state.data_repo = DataRepository(data_platform_pool)
        app.state.knowledge_pool = knowledge_pool
        app.state.knowledge_repo = KnowledgeRepository(knowledge_pool)

        # --- Worker de la cola de PDFs hacia knowledge ---
        worker_task: asyncio.Task | None = None
        worker: KnowledgeIngestionWorker | None = None
        if settings.knowledge_worker_enabled:
            worker = KnowledgeIngestionWorker(settings, app.state.ingestion_repo)
            worker_task = asyncio.create_task(worker.run_forever())
            app.state.knowledge_worker = worker
            app.state.knowledge_worker_task = worker_task

        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
            if worker_task is not None:
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
            await knowledge_pool.close()
            await data_platform_pool.close()

    app = FastAPI(
        title='IPM Data Platform Service',
        version=settings.version,
        lifespan=lifespan,
    )
    middleware.install(app, SERVICE_NAME)
    app.include_router(build_health_router(settings))
    app.include_router(build_ingestion_router(settings))
    app.include_router(build_data_router())
    app.include_router(build_knowledge_router(settings))
    return app


app = create_app()