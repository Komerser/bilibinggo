"""FIFO admission to the dashboard's single native job runner."""
from __future__ import annotations

import threading
import time
import uuid
from copy import deepcopy
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from src.app_logging import get_logger
from src.control_store import ControlError, utc_now
from src.job_store import get_job, list_recent_jobs, sanitize_params
from src.log_redact import redact_obj, redact_text
from web.api_errors import AppError, ErrorCode
from web.control_service import get_control_service
from web.user_messages import JOB_ACTION_LABELS

logger = get_logger("task_queue")
QUEUEABLE_ACTIONS = frozenset({"participate", "participate_triple", "participate_sequential", "refresh_all", "refresh_source", "refresh_watch", "refresh_status", "refresh_publishers"})
_WAITING = frozenset({"queued", "starting", "running"})
_RETRYABLE_ACTIONS = frozenset({"control_discover", "refresh_all", "refresh_source", "refresh_watch", "refresh_status", "refresh_publishers"})


def _can_retry(entry):
    return entry.get("action") in _RETRYABLE_ACTIONS and (
        entry["state"] in {"error", "interrupted"} or entry.get("partial", False)
    )


def task_label(action, params):
    if action == "control_discover":
        label = "补查低信号视频" if params.get("_review_deferred") else "检索新视频"
        return label + " · " + "、".join(params["keywords"])
    if action in {"participate_triple", "participate_sequential"}:
        return "自动顺序参与"
    label = JOB_ACTION_LABELS.get(action, action)
    target = params.get("dynamic_id") or params.get("source_id")
    return f"{label} · {target}" if target else label


class TaskQueue:
    def __init__(self, service, runner):
        self.service = service
        self.runner = runner
        self.lock = threading.RLock()
        self.worker = None
        state = service.store.get("dashboard", "task_queue") or {}
        self.entries = state.get("entries") or []
        self.pause_reason = state.get("pause_reason") or ""
        interrupted = False
        for entry in self.entries:
            if entry["state"] in {"starting", "running"}:
                status = self.runner.resolve_job_status(entry["job_id"]) if entry.get("job_id") else None
                if status and status.state not in {"running", "interrupted"}:
                    entry.update(state=status.state, message=status.message)
                    result = status.result or {}
                    entry["partial"] = bool(result.get("source_errors") or (result.get("pipeline") or {}).get("deferred"))
                else:
                    entry.update(state="interrupted", message="程序重启，任务未确认完成")
                interrupted = True
        if any(entry["state"] == "queued" for entry in self.entries):
            self.pause_reason = "程序已重启，等待继续执行队列"
            interrupted = True
        if interrupted:
            self._save()

    def _save(self):
        done = [entry for entry in self.entries if entry["state"] not in _WAITING]
        retained = {entry["id"] for entry in done[-50:]}
        self.entries = [entry for entry in self.entries if entry["state"] in _WAITING or entry["id"] in retained]
        self.service.store.put("dashboard", "task_queue", {"entries": self.entries, "pause_reason": self.pause_reason})

    def _start(self, entry):
        action, params = entry["action"], entry["params"]
        if action in {"control_discover", "refresh_watch"}:
            params = {**params, "_queue_checkpoint_id": entry.get("checkpoint_id", entry["id"])}
        if action == "control_discover":
            params = {**params, "request_budget": None}
            with self.service.lock:
                control_job = self.service.start(action, params, require_llm=True, source="queue")
            _, native_id, _ = self.service.store.job_record(control_job["job_id"])
            return native_id, control_job["job_id"]
        if not self.service.account()["credentials_present"]:
            raise ControlError("AUTH_REQUIRED", "请先扫码登录")
        if action != "refresh_status":
            from src.llm_settings import is_llm_ready

            if not is_llm_ready():
                raise ControlError("LLM_NOT_READY", "请先完成 LLM 配置与连接测试")
        if action in {"participate_triple", "participate_sequential"}:
            params = {**params, "_queued_serial": True}
        return self.runner.try_start(action, params, source="queue"), None

    def _advance(self):
        active = next((entry for entry in self.entries if entry["state"] == "running"), None)
        if active:
            job = self.runner.resolve_job_status(active["job_id"])
            if job.state == "running":
                return
            active.update(state=job.state, message=job.message)
            result = job.result or {}
            active["partial"] = bool(result.get("source_errors") or (result.get("pipeline") or {}).get("deferred"))
            if result.get("completion_reason") in {"platform_paused", "request_budget"}:
                active.update(state="interrupted", message="检索未完成，队列已暂停")
                self.pause_reason = "检索遇到平台限制或请求配额，剩余任务已暂停"
            self._save()
        if self.runner.is_running():
            return
        entry = next((item for item in self.entries if item["state"] == "queued"
                      and (not self.pause_reason or item.get("run_while_paused"))), None)
        if not entry:
            return
        if entry["action"] == "refresh_publishers":
            from web.app import app

            cluster = getattr(app.state, "cluster", None)
            if cluster and cluster.publisher_work_busy():
                return
        entry.update(state="starting", message="正在提交任务")
        self._save()
        try:
            native_id, control_id = self._start(entry)
            if native_id is None:
                raise ControlError("JOB_BUSY", "等待前一个任务结束")
            entry.update(state="running", job_id=native_id, control_job_id=control_id, message="任务运行中")
        except ControlError as exc:
            if exc.code == "JOB_BUSY":
                entry.update(state="queued", message="等待前一个任务结束")
            else:
                entry.update(state="error", message=str(exc))
        except Exception as exc:
            logger.exception("队列任务启动失败")
            entry.update(state="error", message=redact_text(str(exc))[:500])
        self._save()

    def _drain(self):
        try:
            while True:
                with self.lock:
                    self._advance()
                    waiting = any(entry["state"] in _WAITING for entry in self.entries)
                    if not waiting or (self.pause_reason and not self._has_explicit_retry()):
                        return
                time.sleep(0.25)
        except Exception:
            logger.exception("任务队列异常暂停")
            with self.lock:
                self.pause_reason = "任务队列异常，等待恢复"
                for entry in self.entries:
                    entry.pop("run_while_paused", None)
                self._save()
        finally:
            with self.lock:
                self.worker = None
                self._ensure_worker()

    def _has_explicit_retry(self):
        return any(entry.get("run_while_paused") and entry["state"] in _WAITING for entry in self.entries)

    def _ensure_worker(self):
        allowed = not self.pause_reason or self._has_explicit_retry()
        if self.worker is None and allowed and any(entry["state"] in _WAITING for entry in self.entries):
            self.worker = threading.Thread(target=self._drain, name="dashboard-task-queue", daemon=True)
            self.worker.start()

    def submit(self, action, params):
        params = sanitize_params(params)
        entry = {"id": uuid.uuid4().hex, "created_at": utc_now(), "action": action, "label": task_label(action, params),
                 "params": params, "state": "queued", "message": "等待前一个任务结束", "job_id": None, "control_job_id": None}
        with self.lock:
            if action == "refresh_publishers":
                existing = next((item for item in self.entries if item["action"] == action and item["state"] in _WAITING), None)
                if existing:
                    return deepcopy(existing)
            if action == "participate":
                for waiting in self.entries:
                    if (waiting["state"] in _WAITING and waiting["action"] == action
                            and waiting["params"].get("dynamic_id") == params.get("dynamic_id")):
                        return deepcopy(waiting)
            if action == "control_discover" and params.get("_review_deferred"):
                for waiting in self.entries:
                    if (waiting["state"] in _WAITING and waiting["action"] == action
                            and waiting["params"].get("_discovery_checkpoint_id") == params["_discovery_checkpoint_id"]):
                        return deepcopy(waiting)
            self.entries.append(entry)
            self._save()
            if action == "refresh_publishers" and not self.pause_reason:
                self.runner.pause_round_participation()
            self._advance()
            self._ensure_worker()
            return deepcopy(entry)

    def publisher_waiting(self):
        with self.lock:
            return any(entry["action"] == "refresh_publishers" and entry["state"] in _WAITING
                       and (not self.pause_reason or entry.get("run_while_paused")) for entry in self.entries)

    def snapshot(self):
        with self.lock:
            entries = deepcopy(self.entries)
            pause_reason = self.pause_reason
        retried = {entry.get("retry_of") for entry in entries}
        for entry in entries:
            entry["can_retry"] = _can_retry(entry) and entry["id"] not in retried
        return {"entries": entries, "pause_reason": pause_reason}

    def retry(self, identifier):
        with self.lock:
            previous = next((entry for entry in self.entries if entry["id"] == identifier), None)
            if previous is None or not _can_retry(previous):
                raise AppError(ErrorCode.VALIDATION_ERROR, "这项任务无法从队列重试")
            if any(entry.get("retry_of") == identifier for entry in self.entries):
                self._ensure_worker()
                return self.snapshot()
            params = deepcopy(previous["params"])
            if previous["action"] == "control_discover":
                params.update(resume=True, replay_completed=False)
            entry = {
                "id": uuid.uuid4().hex, "created_at": utc_now(), "action": previous["action"],
                "label": previous["label"], "params": params, "state": "queued",
                "message": "等待重新执行", "job_id": None, "control_job_id": None,
                "retry_of": identifier, "checkpoint_id": previous.get("checkpoint_id", identifier),
                "run_while_paused": True,
            }
            self.entries.append(entry)
            self._save()
            # An explicitly retried task can clear a pause only when it is the sole waiting entry.
            if self.pause_reason and not any(item is not entry and item["state"] in _WAITING for item in self.entries):
                self.pause_reason = ""
                self._save()
            self._ensure_worker()
            return self.snapshot()

    def change(self, identifier, operation):
        with self.lock:
            waiting = [index for index, entry in enumerate(self.entries) if entry["state"] == "queued"]
            index = next((index for index in waiting if self.entries[index]["id"] == identifier), None)
            if index is None:
                raise AppError(ErrorCode.VALIDATION_ERROR, "只有等待中的任务可以调整或移出队列")
            if operation == "remove":
                self.entries[index].update(state="cancelled", message="已移出等待队列")
            else:
                position = waiting.index(index) + (-1 if operation == "up" else 1)
                if 0 <= position < len(waiting):
                    other = waiting[position]
                    self.entries[index], self.entries[other] = self.entries[other], self.entries[index]
            self._save()
            return self.snapshot()

    def resume(self):
        with self.lock:
            self.pause_reason = ""
            self._save()
            self._ensure_worker()
            return self.snapshot()


def get_task_queue():
    from web.job_runner import runner

    service = get_control_service()
    with service.lock:
        if not hasattr(service, "task_queue"):
            service.task_queue = TaskQueue(service, runner)
        return service.task_queue


class QueueChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["up", "down", "remove"]


def install_task_queue(app):
    router = APIRouter(prefix="/api/task-queue", tags=["stable"])

    @router.get("")
    def current():
        from web.job_runner import runner

        jobs = [{key: row[key] for key in ("id", "label", "action", "state", "message", "started_at")}
                for row in list_recent_jobs(limit=30)]
        return {**get_task_queue().snapshot(), "jobs": jobs, "current_job": redact_obj(runner.get_status().to_dict())}

    @router.get("/logs/{job_id}")
    def log(job_id: int):
        from web.job_runner import runner

        if get_job(job_id) is None and runner.get_status().id != job_id:
            raise AppError(ErrorCode.NOT_FOUND, "任务日志已过保留期或不存在")
        return redact_obj(runner.resolve_job_status(job_id).to_dict())

    @router.post("/resume")
    def resume():
        return get_task_queue().resume()

    @router.patch("/{entry_id}")
    def change(entry_id: str, request: QueueChangeRequest):
        return get_task_queue().change(entry_id, request.operation)

    @router.post("/{entry_id}/retry")
    def retry(entry_id: str):
        return get_task_queue().retry(entry_id)

    app.include_router(router)
