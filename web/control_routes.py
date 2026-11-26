"""Loopback, authenticated control endpoints and browser-origin protection."""
from __future__ import annotations

import os
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.control_contract import error_result
from src.control_store import ControlError
from web.control_service import get_control_service

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def install_control(app):
    router = APIRouter(prefix="/api/control/v2")
    web_origin = os.environ.get("BINGGO_WEB_ORIGIN", "").strip().rstrip("/")

    @app.middleware("http")
    async def control_security(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            host = request.url.hostname
            origin = request.headers.get("origin")
            # testserver is only an in-process ASGI testing hostname.
            if host not in _LOCAL_HOSTS | {"testserver"}:
                return JSONResponse({"detail": "Invalid Host"}, status_code=403)
            control = path.startswith("/api/control/v2")
            if web_origin and not control:
                if origin is not None and origin != web_origin:
                    return JSONResponse({"detail": "Invalid Origin"}, status_code=403)
                # Cross-site browser requests cannot supply this header without
                # a CORS preflight, which the dashboard does not allow.
                if request.method not in _SAFE_METHODS and request.headers.get("x-binggo-dashboard") != "1":
                    return JSONResponse({"detail": "Dashboard request required"}, status_code=403)
            elif origin:
                try:
                    parsed = urlsplit(origin)
                    valid = parsed.scheme in {"http", "https"} and parsed.hostname in _LOCAL_HOSTS and parsed.port == request.url.port
                except ValueError:
                    valid = False
                if not valid:
                    return JSONResponse({"detail": "Invalid Origin"}, status_code=403)
            if request.method not in _SAFE_METHODS and not path.startswith(("/api/updates/", "/api/cluster/peer/")):
                from src.update_state import maintenance
                if maintenance.is_set():
                    return JSONResponse({"detail": "版本更新中，请等待当前任务结束及服务重启"}, status_code=409)
        if path.startswith("/api/control/v2"):
            access = get_control_service().access()
            if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + access["token"]):
                return JSONResponse({"detail": "Control authentication required"}, status_code=401)
            request.state.control_scopes = set(access["scopes"])
        return await call_next(request)

    @router.get("/identity")
    def identity():
        return get_control_service().descriptor

    @router.get("/capabilities")
    def capabilities():
        return get_control_service().dispatch("capabilities_get", {})

    @router.post("/tools/{name}")
    def call(name: str, arguments: dict, request: Request):
        return get_control_service().invoke(name, arguments, request.state.control_scopes)

    @router.post("/shutdown")
    def shutdown(request: Request):
        if "app:control" not in request.state.control_scopes:
            return JSONResponse({"detail": "Missing app:control"}, status_code=403)
        service = get_control_service()
        service.ensure_idle()
        from src.dashboard_server import request_shutdown
        import threading
        threading.Timer(0.2, request_shutdown).start()
        return {"stopping": True}

    app.include_router(router)
    from contextlib import asynccontextmanager
    previous_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def control_lifespan(application):
        get_control_service()
        async with previous_lifespan(application) as state:
            yield state
    app.router.lifespan_context = control_lifespan
