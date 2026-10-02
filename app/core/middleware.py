"""Request/correlation IDs, logging de peticiones, métricas y errores centralizados."""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import correlation_id_var, request_id_var

log = logging.getLogger("http")

_CODES = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden"}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id_var.get()}},
        headers={"X-Request-ID": request_id_var.get(), "X-Correlation-ID": correlation_id_var.get()},
    )


def install(app: FastAPI, service_name: str) -> CollectorRegistry:
    registry = CollectorRegistry()
    requests_total = Counter(
        "http_requests_total",
        "Total de peticiones HTTP.",
        ["service", "method", "path", "status"],
        registry=registry,
    )

    @app.middleware("http")
    async def _context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        cid = request.headers.get("X-Correlation-ID") or rid
        request_id_var.set(rid)
        correlation_id_var.set(cid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - último recurso, se responde con el formato central
            log.exception("unhandled_exception")
            response = _error(500, "internal_error", "error interno del servidor")
        response.headers["X-Request-ID"] = rid
        response.headers["X-Correlation-ID"] = cid
        requests_total.labels(service_name, request.method, request.url.path, str(response.status_code)).inc()
        log.info(
            "http_request",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000),
                }
            },
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODES.get(exc.status_code, f"http_{exc.status_code}")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            422,
            "validation_error",
            "entrada inválida: "
            + "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()),
        )

    @app.get("/metrics", include_in_schema=False)
    async def _metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return registry
