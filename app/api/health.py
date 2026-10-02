"""Handlers HTTP de salud. Sin lógica de negocio."""

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import Settings


async def _check_tcp(addr: str, timeout: float = 2.0) -> bool:
    host, _, port = addr.rpartition(":")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout)
    except (TimeoutError, OSError, ValueError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, str]:
        """Liveness: no comprueba dependencias."""
        return {"status": "ok", "service": settings.service_name, "version": settings.version}

    @router.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness: conectividad TCP con las dependencias declaradas (pings reales en fases posteriores)."""
        results = await asyncio.gather(*(_check_tcp(a) for a in settings.ready_deps.values()))
        names = settings.ready_deps
        checks = {n: ("ok" if ok else "unreachable") for n, ok in zip(names, results, strict=True)}
        is_ready = all(results)
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "not_ready",
                "service": settings.service_name,
                "checks": checks,
            },
        )

    return router
