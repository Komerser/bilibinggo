
"""Dashboard controls and the separately authenticated cluster peer protocol."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, Query, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from src.app_paths import DATA_DIR
from web.account_service import get_account_profile, has_login_cookie
from web.api_errors import AppError, ErrorCode, require_login
from web.cluster_protocol import (Analysis, DetectionPageRequest, DispatchSettings, Exchange, HostRequest,
                                  JoinRequest, NodeId, NodeName, PEER_PATH, ParticipationPreference, Register, RenameNode,
                                  WatchAssignment, WatchBatch, WatchFindings, WorkAssignments)
from web.cluster_service import ClusterService
from web.cluster_store import ClusterError

MAX_BODY = 256 * 1024


def _bearer(request):
    value = request.headers.get("authorization", "")
    prefix, _, token = value.partition(" ")
    if prefix != "Bearer" or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise AppError(ErrorCode.AUTH_REQUIRED, "需要集群凭据")
    return token


async def _read(request, schema):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BODY:
            raise AppError(ErrorCode.VALIDATION_ERROR, "集群数据超出大小限制", status_code=413)
        data.extend(chunk)
    try:
        return schema.model_validate_json(data)
    except ValidationError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, "集群协议或参数无效") from exc


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ClusterError as exc:
        code = ErrorCode.AUTH_REQUIRED if exc.status == 401 else ErrorCode.VALIDATION_ERROR
        raise AppError(code, str(exc), status_code=exc.status) from exc
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc


def _dashboard(request, *, check_login=False):
    if request.headers.get("x-binggo-dashboard") != "1":
        raise AppError(ErrorCode.AUTH_REQUIRED, "请从 Binggo 集群页面操作", status_code=403)
    if check_login:
        require_login(get_account_profile())


def install_cluster(app):
    dashboard = APIRouter(prefix="/api/cluster", tags=["cluster"])
    peer = APIRouter(prefix=PEER_PATH, tags=["cluster-peer"])
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application) as state:
            service = ClusterService(DATA_DIR / "cluster")
            application.state.cluster = service
            service.start()
            try:
                yield state
            finally:
                application.state.cluster.stop()

    app.router.lifespan_context = lifespan

    @dashboard.get("/status")
    def status(request: Request):
        return request.app.state.cluster.snapshot()

    @dashboard.get("/detected-activities")
    def detected_activities(request: Request, page: int = Query(default=1, ge=1, le=1_000_000)):
        _dashboard(request)
        return _run(request.app.state.cluster.detected_activities, page)

    @dashboard.post("/host")
    def host(request: Request, body: HostRequest):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.host, body)
        return service.snapshot()

    @dashboard.get("/tracked-users")
    def tracked_users(request: Request, page: int = Query(default=1, ge=1, le=1_000_000)):
        _dashboard(request)
        return _run(request.app.state.cluster.tracked_users, page)

    @dashboard.get("/publisher-sources")
    def publisher_sources(request: Request, page: int = Query(default=1, ge=1, le=1_000_000)):
        _dashboard(request)
        return _run(request.app.state.cluster.publisher_listing, page)

    @peer.post("/publisher-sources")
    async def peer_publisher_sources(request: Request):
        token = _bearer(request)
        body = await _read(request, DetectionPageRequest)
        return await run_in_threadpool(_run, request.app.state.cluster.store.publisher_listing,
                                       body.page, node=body.node, token=token)

    @dashboard.post("/watch-assignment")
    def watch_assignment(request: Request, body: WatchAssignment):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.store.assign_watch, body)
        service.wake.set()
        return service.snapshot()

    @dashboard.post("/watch-batch")
    def watch_batch(request: Request, body: WatchBatch):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.store.assign_watch_batch, body)
        service.wake.set()
        return service.snapshot()

    @dashboard.post("/dispatch")
    def dispatch(request: Request, body: DispatchSettings):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.store.set_dispatch, body)
        service.wake.set()
        return service.snapshot()

    @dashboard.post("/work-assignments")
    def work_assignments(request: Request, body: WorkAssignments):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.store.set_work_assignments, body)
        service.wake.set()
        return service.snapshot()

    @dashboard.post("/watch-start")
    def watch_start(request: Request):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.store.start_watch)
        service.wake.set()
        return service.snapshot()

    @dashboard.post("/watch-stop")
    def watch_stop(request: Request):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.store.stop_watch)
        service.wake.set()
        return service.snapshot()

    @dashboard.post("/node-name")
    def node_name(request: Request, body: NodeName):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.rename, body.name)
        return service.snapshot()

    @dashboard.post("/collection-pause")
    def collection_pause(request: Request, paused: bool = True):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.store.set_collection_paused, paused)
        service.wake.set()
        return service.snapshot()

    @dashboard.post("/round-participation")
    def round_participation(request: Request, body: ParticipationPreference):
        _dashboard(request, check_login=body.enabled)
        service = request.app.state.cluster
        _run(service.set_round_participation, body.enabled)
        return service.snapshot()

    @dashboard.post("/join")
    def join(request: Request, body: JoinRequest):
        _dashboard(request, check_login=True)
        service = request.app.state.cluster
        _run(service.join, body)
        return service.snapshot()

    @dashboard.post("/leave")
    def leave(request: Request):
        _dashboard(request)
        service = request.app.state.cluster
        service.leave()
        return service.snapshot()

    @dashboard.post("/invite")
    def invite(request: Request):
        _dashboard(request)
        if not has_login_cookie():
            raise AppError(ErrorCode.AUTH_REQUIRED, "请先扫码登录")
        return _run(request.app.state.cluster.invite)

    @dashboard.post("/pause")
    def pause(request: Request, paused: bool = True):
        _dashboard(request)
        service = request.app.state.cluster
        _run(service.store.set_paused, paused)
        return service.snapshot()

    @peer.post("/watch-findings")
    async def watch_findings(request: Request):
        token = _bearer(request)
        body = await _read(request, WatchFindings)
        return await run_in_threadpool(_run, request.app.state.cluster.store.publish_watch_findings, body, token)

    @peer.post("/rename")
    async def rename(request: Request):
        token = _bearer(request)
        body = await _read(request, RenameNode)
        return await run_in_threadpool(_run, request.app.state.cluster.store.rename_node, body, token)

    @peer.post("/join")
    async def register(request: Request):
        token = _bearer(request)
        body = await _read(request, Register)
        return await run_in_threadpool(_run, request.app.state.cluster.store.register, body, token,
                                       video_history=request.headers.get("x-binggo-video-history") == "1")

    @peer.post("/exchange")
    async def exchange(request: Request):
        token = _bearer(request)
        body = await _read(request, Exchange)
        if request.headers.get("x-binggo-video-detection") == "1":
            body.video_detection = True
        if request.headers.get("x-binggo-unknown-activity-time") == "1":
            body.unknown_activity_time = True
        return await run_in_threadpool(_run, request.app.state.cluster.store.exchange, body, token)

    @peer.post("/detected-activities")
    async def peer_detections(request: Request):
        token = _bearer(request)
        body = await _read(request, DetectionPageRequest)
        return await run_in_threadpool(_run, request.app.state.cluster.store.detected_activities,
                                       body.page, node=body.node, token=token)

    @peer.post("/analysis")
    async def analysis(request: Request):
        token = _bearer(request)
        body = await _read(request, Analysis)
        return await run_in_threadpool(_run, request.app.state.cluster.store.analysis, body, token)

    @peer.post("/tracked-users")
    async def peer_tracked_users(request: Request):
        token = _bearer(request)
        body = await _read(request, DetectionPageRequest)
        return await run_in_threadpool(_run, request.app.state.cluster.store.tracked_users,
                                       body.page, node=body.node, token=token)

    @peer.post("/leave/{node}")
    def unregister(request: Request, node: NodeId):
        _run(request.app.state.cluster.store.leave, node, _bearer(request))
        return {"ok": True}

    app.include_router(dashboard)
    app.include_router(peer)
