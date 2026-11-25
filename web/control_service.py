"""Authenticated application control; all business work shares JobRunner."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.control_contract import catalog, envelope, error_result, validate_input, validate_output
from src.control_store import ControlError, ControlStore, digest, utc_now

BOOT_ID = os.environ.get("BINGGO_CONTROL_BOOT_ID") or uuid.uuid4().hex
LOCAL_GROUPS = {"development", "testing"}
LOCAL_NAMES = {"binggo_instances_list", "binggo_app_start", "binggo_app_stop", "binggo_app_restart"}
_instances = {}
_instances_lock = threading.Lock()


def instance_identifier(home):
    return "binggo-" + digest(str(Path(home).resolve()).casefold())[:16]


class ControlService:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.instance_id = instance_identifier(home)
        self.store = ControlStore(self.home / "data/control")
        from src.lottery_discovery_broad import restore_video_index
        restore_video_index(self.store, self.home / "data/lottery-discovery-cache.json")
        self.lock = threading.RLock()
        self.access_path = self.home / "config/control-access.json"
        if not self.access_path.exists():
            from src.secure_files import write_text_secret
            write_text_secret(self.access_path, json.dumps({"token": secrets.token_urlsafe(48), "scopes": [
                "app:read", "app:control", "data:write", "settings:write", "account:manage",
                "platform:read", "platform:write", "automation:write", "test:run", "dev:build", "dev:release"]}))
        self.write_descriptor()

    def write_descriptor(self):
        from src.app_paths import __version__, runtime_label
        from src.dashboard_server import get_dashboard_url
        self.descriptor = {"instance_id": self.instance_id, "boot_id": BOOT_ID, "version": __version__,
                           "runtime": runtime_label(), "state": "running", "endpoint": get_dashboard_url(),
                           "data_root_id": digest(str(self.home).casefold())[:24], "control_contract_version": 2,
                           "verified_identity": True, "pid": os.getpid(), "started_at": utc_now()}
        from src.db.schema import SCHEMA_VERSION
        self.descriptor["business_schema_version"] = SCHEMA_VERSION
        workflow = self.store.get("workflow", "current")
        if workflow and workflow.get("running") and workflow.get("boot_id") != BOOT_ID:
            workflow.update(running=False, reason="application_restarted")
            self.store.put("workflow", "current", workflow)
        from src.user_data_io import atomic_write_json
        atomic_write_json(self.store.root / "instance.json", self.descriptor)

    def access(self):
        return json.loads(self.access_path.read_text(encoding="utf-8"))

    def invoke(self, name, arguments, scopes, caller="local-owner"):
        operation_key = None
        owns_operation = False
        request_id = uuid.uuid4().hex
        try:
            entry = validate_input(name, arguments)
            if arguments.get("instance_id") != self.instance_id:
                raise ControlError("INSTANCE_NOT_FOUND", "实例与本应用数据根不匹配")
            if arguments.get("expected_boot_id", BOOT_ID) != BOOT_ID:
                raise ControlError("INSTANCE_CHANGED", "应用已重启，请重新读取身份和计划")
            needed = set(entry["x-authorizationScopes"])
            if name in {"binggo_automation_configure", "binggo_automation_start"}:
                workflow = arguments.get("workflow") or (self.store.get("workflow", "current") or {}).get("workflow", {})
                if "execute" in workflow.get("stages", []):
                    needed.add("platform:write")
            if not needed.issubset(scopes):
                raise ControlError("SCOPE_DENIED", "本机连接未授予所需权限")
            with self.lock:
                if "idempotency_key" in arguments:
                    operation_key = f"{self.instance_id}:{caller}:{name}:{arguments['idempotency_key']}"
                    prior = self.store.claim(operation_key, arguments)
                    if prior is not None:
                        return prior
                    owns_operation = True
                data = self.dispatch(name.removeprefix("binggo_"), arguments)
                from src.log_redact import redact_obj
                data = redact_obj(data)
                result = envelope(self.instance_id, data, status="accepted" if entry["x-execution"] == "submit-and-return-job-id" else "completed", request_id=request_id)
                validate_output(name, result)
                if operation_key:
                    self.store.complete(operation_key, result)
                return result
        except Exception as exc:
            result = error_result(self.instance_id, exc, request_id)
            if operation_key and owns_operation:
                self.store.complete(operation_key, result)
            return result

    def account(self, refresh=False):
        from src.bilibili_auth import get_login_uid
        from web.account_service import _load_account_cache, get_account_profile, has_login_cookie
        value = get_account_profile() if refresh else _load_account_cache()
        present = has_login_cookie()
        network = "offline" if value.get("network_error") else ("online" if refresh and value.get("logged_in") else "unknown")
        auth = "absent" if not present else ("unknown" if not refresh or network == "offline" else ("valid" if value.get("logged_in") else "invalid"))
        if refresh and (value.get("connection_failed") or value.get("network_unavailable")):
            network, auth = "offline", "unknown"
        return {"credentials_present": present, "auth_state": auth, "network_state": network,
                "account_ref": str(get_login_uid()) if get_login_uid() else None, "checked_at": utc_now() if refresh else None,
                "uname": value.get("uname", ""), "message": value.get("message", "")}

    def settings(self):
        from web.app import _build_settings_payload
        value = _build_settings_payload()
        value["revision"] = digest(value)
        return value

    def require_revision(self, expected, value):
        if expected != value:
            raise ControlError("REVISION_CONFLICT", "配置已变化，请重新读取后修改")

    def start(self, action, params=None, *, require_account=True, require_llm=False, source="mcp"):
        from web.job_runner import runner
        if require_account and not self.account()["credentials_present"]:
            raise ControlError("AUTH_REQUIRED", "应用尚未保存登录，请扫码登录")
        if require_llm:
            from src.llm_settings import is_llm_ready
            if not is_llm_ready():
                raise ControlError("LLM_NOT_READY", "请先在应用中完成LLM配置与连接测试")
        if runner.is_running():
            raise ControlError("JOB_BUSY", "已有网页、调度或MCP任务正在运行", retryable=True)
        metadata = {"source": source}
        if action == "control_discover":
            metadata["keywords"] = (params or {}).get("keywords", [])
            metadata["keyword_match"] = (params or {}).get("keyword_match")
            metadata["discovery_mode"] = (params or {}).get("mode", "standard")
            metadata["target_new_videos"] = (params or {}).get("target_new_videos")
        elif action == "control_discovery_filter" or (action == "control_import" and (params or {}).get("queue_verified")):
            metadata["discovery_queue"] = params["discovery_job_id"]
        job = self.store.new_job(action, BOOT_ID, metadata=metadata)
        job_id = job["job_id"]
        values = {**(params or {}), "_control_job_id": job_id}
        native_id = runner.try_start(action, values, source=source)
        if native_id is None:
            self.store.update_job(job_id, state="error", result={"code": "JOB_BUSY"})
            raise ControlError("JOB_BUSY", "任务槽被其他操作占用", job_id=job_id, retryable=True)
        self.store.bind_job(job_id, native_id)
        return self.job(job_id)

    def job(self, identifier):
        owner, native_id, value = self.store.job_record(identifier)
        if owner != "app" or value["state"] != "running":
            return value
        if value["boot_id"] != BOOT_ID:
            return self.store.update_job(identifier, state="interrupted", result={"code": "INSTANCE_CHANGED"})
        if native_id is None:
            return value
        from web.job_runner import runner
        status = runner.resolve_job_status(native_id).to_dict()
        result = status.get("result")
        return self.store.update_job(identifier, state=status["state"], result=result,
            can_cancel=status["state"] == "running" and (status["action"] == "login" or status["action"].startswith("control_")),
            progress={"step": status["progress_step"], "total": status["progress_total"], "message": status.get("message") or ""})

    def map_current_job(self):
        from web.job_runner import runner
        current = runner.get_status()
        if current.id is None:
            return None
        with self.store.transaction() as db:
            found = db.execute("SELECT id,payload FROM control_jobs WHERE owner='app' AND backend_id=?",
                               (current.id,)).fetchall()
        for row in found:
            if json.loads(row[1]).get("boot_id") == BOOT_ID:
                return self.job(row[0])
        value = self.store.new_job(current.action, BOOT_ID, metadata={"source": current.source})
        self.store.bind_job(value["job_id"], current.id)
        return self.job(value["job_id"])

    @staticmethod
    def activity(value):
        from src.lottery_classifier import PARTICIPATABLE_TYPES, is_charging_lottery_activity
        status = value.get("activity_status")
        excluded = []
        if value.get("draw_status") == "ended" or status == "已结束":
            excluded.append("ended")
        if status == "已参加":
            excluded.append("already_joined")
        if is_charging_lottery_activity(value):
            excluded.append("paid_only")
        kind = value.get("lottery_type") or "unknown"
        if kind not in PARTICIPATABLE_TYPES:
            excluded.append("unsupported_or_unclassified")
        return {**value, "dynamic_id": str(value["dynamic_id"]), "title": str(value.get("activity_title") or value.get("prize") or ""),
                "lottery_type": kind, "can_participate": not excluded and status == "未参加",
                "participation_state": {"已参加": "joined", "未参加": "not_joined"}.get(status, "unknown"),
                "draw_state": value.get("draw_status") if value.get("draw_status") in {"active", "ended"} else "unknown",
                "excluded_reasons": excluded, "checked_at": None}

    def activity_record(self, dynamic_id):
        from src.activity_store import load_activities
        item = next((v for v in load_activities() if str(v.get("dynamic_id")) == dynamic_id), None)
        if not item:
            raise ControlError("NOT_FOUND", "活动尚未导入或已删除")
        return item

    def dispatch(self, name, args):
        from web.job_runner import runner
        if name == "instance_get":
            return self.descriptor
        if name == "capabilities_get":
            return {"control_contract_version": 2, "tool_names": sorted(catalog()),
                    "supported_scopes": self.access()["scopes"], "protocol_versions": ["2026-07-28", "2025-11-25"]}
        if name == "health_get":
            from src.config_health import run_config_health_checks
            current = self.map_current_job()
            active = current if current and current["state"] == "running" else None
            return {"app_state": "running", "network_state": "unknown", "active_job_id": active["job_id"] if active else None,
                    "findings": run_config_health_checks().to_dict()["findings"]}
        if name in {"account_get", "account_refresh"}:
            return self.account(refresh=name.endswith("refresh"))
        if name == "login_start":
            return self.start("login", require_account=False)
        if name == "login_qrcode":
            job = self.job(args["job_id"])
            if job["action"] != "login" or job["boot_id"] != BOOT_ID or job["state"] != "running":
                raise ControlError("NOT_FOUND", "该任务没有进行中的二维码")
            from src.app_paths import QR_IMAGE_PATH
            if not QR_IMAGE_PATH.is_file():
                raise ControlError("NOT_FOUND", "二维码尚未生成，请稍后查询")
            result = job.get("result") or {}
            return {"job_id": args["job_id"], "login_phase": result.get("login_phase") or "waiting",
                    "qrcode_refreshed_at": datetime.fromtimestamp(result.get("qrcode_refreshed_at") or time.time(), timezone.utc).isoformat(),
                    "image_artifact": self.store.artifact("login.png", QR_IMAGE_PATH.read_bytes(), "image/png", redacted=False)}
        if name == "account_logout":
            self.ensure_idle()
            from web.account_service import clear_login_cookie
            clear_login_cookie()
            return {"logged_out": True}
        if name == "account_ack_notice":
            from web.account_service import ack_at_unread_notice
            return ack_at_unread_notice(args["current"])
        if name in {"summary_get", "sources_list"}:
            from web.activity_service import get_summary
            summary = get_summary()
            return summary if name == "summary_get" else {"sources": summary.get("sources") or [], "next_cursor": None}
        if name == "activities_list":
            from web.activity_service import _filtered_activity_rows
            filters = {k: v for k, v in args.items() if k in {"status", "lottery_type", "draw", "draw_window", "q", "sort", "order"}}
            rows = _filtered_activity_rows(**filters)
            page, size = args.get("page", 1), args.get("page_size", 20)
            return {"activities": [self.activity(row) for row in rows[(page-1)*size:page*size]], "page": page, "page_size": size, "total": len(rows)}
        if name == "activity_get":
            return self.activity(self.activity_record(args["dynamic_id"]))
        if name == "watch_users_list":
            from src.watch_users import get_watch_users_payload
            payload = get_watch_users_payload(ensure_seeded=False)
            return {"users": payload.get("users") or payload.get("watch_users") or [], "next_cursor": None}
        if name in {"watch_user_add", "watch_user_remove"}:
            self.ensure_idle()
            from src.watch_users import add_watch_user, remove_watch_user
            function = add_watch_user if name.endswith("add") else remove_watch_user
            value = function(int(args["mid"]))
            return value if isinstance(value, dict) else {"updated": True}
        if name == "refresh_start":
            scope = args["scope"]
            return self.start("refresh_" + scope, {"source_id": args["source_id"]} if scope == "source" else {}, require_llm=scope != "status")
        if name == "discovery_start":
            return self.start("control_discover", args, require_llm=True)
        if name == "discovery_get":
            value = self.store.get("discovery", str(args["job_id"]))
            if value is None:
                job = self.job(args["job_id"])
                if job["state"] == "running":
                    return {"job_id": job["job_id"], "candidates": [], "errors": [], "stopped": False, "requests_used": 0, "next_cursor": None}
                raise ControlError("NOT_FOUND", "该任务没有发现报告")
            offset, size = int(args.get("cursor") or 0), args.get("limit", 100)
            rows = value["candidates"]
            return {**{k: value[k] for k in ("job_id", "errors", "stopped", "requests_used")}, "candidates": rows[offset:offset+size],
                    "next_cursor": str(offset+size) if offset+size < len(rows) else None,
                    **{k: value[k] for k in ("session_id", "resumed", "scan_completed", "has_more", "verification_remaining", "sources_remaining") if k in value}}
        if name == "discovery_import":
            return self.start("control_import", args, require_llm=True)
        if name == "participation_plan":
            self.ensure_idle()
            return self.make_plan(args["dynamic_ids"], args.get("ttl_seconds", 300))
        if name == "participation_execute":
            plan = self.get_plan(args["plan_id"])
            if self.store.get("consumed_plan", plan["plan_id"]):
                raise ControlError("OUTCOME_UNKNOWN", "该计划已提交；请查询原任务并核验结果")
            job = self.start("control_participate", {"plan_id": plan["plan_id"]}, require_llm=True)
            self.store.put("consumed_plan", plan["plan_id"], {"job_id": job["job_id"]})
            return job
        if name == "participation_verify":
            return self.verify(args["job_id"])
        if name == "job_get":
            return self.job(args["job_id"])
        if name == "jobs_list":
            self.map_current_job()
            rows = self.store.list_jobs(before=int(args.get("cursor") or 2147483647), limit=args.get("limit", 50), state=args.get("state"))
            return {"jobs": [self.job(v["job_id"]) for v in rows], "next_cursor": str(rows[-1]["job_id"]) if len(rows) == args.get("limit", 50) else None}
        if name == "job_wait":
            deadline = time.monotonic() + args.get("timeout_seconds", 10)
            while True:
                job = self.job(args["job_id"])
                if job["state"] != "running" or time.monotonic() >= deadline:
                    return job
                time.sleep(0.25)
        if name == "job_events":
            self.job(args["job_id"])
            return self.store.events(args["job_id"], args.get("after_seq", 0), args.get("limit", 100))
        if name == "job_cancel":
            value = self.job(args["job_id"])
            _, native_id, _ = self.store.job_record(args["job_id"])
            if not value["can_cancel"] or runner.get_status().id != native_id:
                raise ControlError("JOB_NOT_CANCELLABLE", "该任务当前不支持安全取消")
            runner.cancel()
            return self.store.update_job(args["job_id"], cancel_requested=True)
        if name.startswith("automation_"):
            return self.automation(name, args)
        if name == "settings_get":
            return self.settings()
        if name == "settings_update":
            self.ensure_idle()
            current = self.settings()
            self.require_revision(args["expected_revision"], current["revision"])
            from src import user_settings as settings
            patch = args["patch"]
            if patch.get("reset_to_defaults"):
                settings.set_participate_text(settings.DEFAULT_PARTICIPATE_TEXT)
                settings.set_participate_fallback_text(settings.DEFAULT_PARTICIPATE_FALLBACK_TEXT)
            for field in ("participate_text", "participate_fallback_text", "participate_text_mode"):
                if field in patch:
                    getattr(settings, "set_" + field)(patch[field])
            return self.settings()
        if name in {"llm_get", "llm_update"}:
            from src.llm_settings import get_llm_settings_public, load_llm_values, save_llm_settings
            value = get_llm_settings_public()
            value["revision"] = digest(value)
            if name == "llm_get":
                return value
            self.ensure_idle()
            self.require_revision(args["expected_revision"], value["revision"])
            if args.get("provider_ref") or args.get("credential_ref"):
                raise ControlError("VALIDATION_ERROR", "请通过本机应用设置管理提供商和密钥；现有凭证保持不变")
            raw = load_llm_values()
            save_llm_settings(api_key=None, base_url=raw.get("LLM_BASE_URL", ""), model_name=args.get("model_name") or raw.get("LLM_MODEL_NAME", ""))
            return self.dispatch("llm_get", {})
        if name == "llm_test":
            return self.start("control_llm_test", require_account=False)
        if name == "network_diagnose":
            return self.start("control_network", args, require_account=False)
        if name == "logs_query":
            from src.log_query import query_log_lines
            from src.log_redact import redact_text
            native_id = self.store.job_record(args["job_id"])[1] if args.get("job_id") else None
            lines = query_log_lines(job_id=native_id, limit=args.get("limit", 100))["lines"]
            return {"lines": [redact_text(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))[:4000] for v in lines], "next_cursor": None}
        if name == "diagnostics_export":
            from src.diagnostics import build_diagnostics_bundle
            from src.log_redact import redact_text
            native_id = self.store.job_record(args["job_id"])[1] if args.get("job_id") else None
            value = build_diagnostics_bundle(job_id=native_id, current_job=runner.get_status().to_dict(), auto_status={})
            return self.store.artifact(value["filename"], redact_text(value["text"]))
        if name == "updates_check":
            from src.update_check import check_for_updates
            value = check_for_updates().to_dict()
            if value.get("ok") is False:
                raise ControlError("NETWORK_UNREACHABLE", value.get("error") or value.get("message") or "更新源读取失败", retryable=True)
            return value
        raise ControlError("NOT_FOUND", "此工具由本机MCP执行器处理")

    def ensure_idle(self):
        from web.job_runner import runner
        if runner.is_running():
            raise ControlError("JOB_BUSY", "业务任务进行中，请等待后再修改状态", retryable=True)

    def make_plan(self, dynamic_ids, ttl):
        from src.control_telemetry import network_span
        self.ensure_idle()
        with network_span(read_only=True):
            return self._make_plan(dynamic_ids, ttl)

    def _make_plan(self, dynamic_ids, ttl):
        from src.bilibili_client import BilibiliClient
        from src.bilibili_auth import get_login_uid
        from src.forward_parser import _extract_from_detail_item
        from src.lottery_api import fetch_dynamic_detail
        from src.lottery_actions import build_dynamic_context
        from src.user_settings import get_participate_text
        account_ref = str(get_login_uid() or "")
        if not account_ref:
            raise ControlError("AUTH_REQUIRED", "没有可用的已保存账号")
        targets, actions, excluded, snapshots = [], [], [], {}
        text = get_participate_text()
        target_texts = {}
        with BilibiliClient(timeout=8, warmup=False) as client:
            for dynamic_id in dict.fromkeys(dynamic_ids):
                row = self.activity_record(dynamic_id)
                target = self.activity(row)
                if not target["can_participate"]:
                    excluded.append({"dynamic_id": dynamic_id, "reasons": target["excluded_reasons"]})
                    continue
                detail = fetch_dynamic_detail(client, dynamic_id)
                if not detail:
                    raise ControlError("PLATFORM_404", "计划目标详情无法读取")
                snapshots[dynamic_id] = digest(_extract_from_detail_item(detail))
                from src.participate_text import resolve_participate_text_for_activity
                target_text = resolve_participate_text_for_activity(client, dynamic_id=dynamic_id).text
                target_texts[dynamic_id] = target_text
                if target["lottery_type"] == "预约抽奖":
                    recipe = [{"action": "follow", "required": True}, {"action": "reserve", "required": True}]
                else:
                    context = build_dynamic_context(client, dynamic_id=dynamic_id, action_text=target_text, lottery_type=target["lottery_type"])
                    recipe = [{"action": key, "required": (key != "favorite" or not context.favorite_skip_reason) and not (key == "comment" and target["lottery_type"] == "互动抽奖"),
                               "skipped": (key == "favorite" and bool(context.favorite_skip_reason)) or bool(getattr(context, {"like": "liked", "follow": "followed", "favorite": "favorited", "repost": "reposted", "comment": "commented"}[key])),
                               "reason": context.favorite_skip_reason if key == "favorite" else "existing_business_rule"}
                              for key in ("follow", "like", "favorite", "repost", "comment")]
                targets.append(target)
                actions.extend({"dynamic_id": dynamic_id, **v} for v in recipe)
        if not targets:
            raise ControlError("VALIDATION_ERROR", "没有符合条件的活动")
        value = {"plan_id": uuid.uuid4().hex, "boot_id": BOOT_ID, "account_ref": account_ref,
                 "expires_at": (datetime.now(timezone.utc)+timedelta(seconds=ttl)).isoformat(),
                 "settings_revision": self.settings()["revision"], "rules_digest": digest(snapshots),
                 "targets": targets, "actions": actions, "excluded": excluded,
                 "rule_snapshots": snapshots, "action_text": text, "target_texts": target_texts, "estimated_requests": min(110, len(targets)*15)}
        self.store.put("plan", value["plan_id"], value)
        return value

    def get_plan(self, identifier):
        from src.bilibili_auth import get_login_uid
        value = self.store.get("plan", identifier)
        if not value:
            raise ControlError("NOT_FOUND", "计划不存在")
        if value["boot_id"] != BOOT_ID or value["account_ref"] != str(get_login_uid() or "") or datetime.fromisoformat(value["expires_at"]) <= datetime.now(timezone.utc) or value["settings_revision"] != self.settings()["revision"]:
            raise ControlError("PLAN_STALE", "计划已过期，或账号/应用/配置已变化")
        return value

    def verify(self, job_id):
        from src.control_telemetry import network_span
        with network_span(read_only=True):
            return self._verify(job_id)

    def _verify(self, job_id):
        job = self.job(job_id)
        report = self.store.get("participation", str(job_id))
        if report is None:
            raise ControlError("NOT_FOUND", "该任务没有参与证据")
        actions = list(report.get("actions") or [])
        # Fresh read verification; absence or a failed read never means success.
        from src.bilibili_client import BilibiliClient
        from src.lottery_actions import build_dynamic_context
        from src.lottery_api import fetch_reserve_button_status, fetch_notice_for_interact
        with BilibiliClient(timeout=8, warmup=False) as client:
            for target in report.get("targets", []):
                identifier, kind = target["dynamic_id"], target["lottery_type"]
                try:
                    if kind == "预约抽奖":
                        states = {"reserve": fetch_reserve_button_status(client, identifier)}
                    else:
                        context = build_dynamic_context(client, dynamic_id=identifier, action_text=report.get("target_texts", {}).get(identifier, report.get("action_text") or ""), lottery_type=kind, reuse_local_receipts=False)
                        states = {"follow": context.followed, "like": context.liked, "favorite": context.favorited,
                                  "repost": context.reposted, "comment": context.commented}
                        if kind == "互动抽奖":
                            official = fetch_notice_for_interact(client, identifier)
                            states["join"] = bool(official and official[0].get("participated"))
                    for action in actions:
                        if action["dynamic_id"] == identifier and states.get(action["action"]) is True:
                            action["verification"] = "verified"
                except Exception:
                    pass
        report["actions"] = actions
        self.store.put("participation", str(job_id), report)
        return {"job_id": job_id, "participation_outcome": report["participation_outcome"], "actions": actions}

    def automation(self, name, args):
        current = self.store.get("workflow", "current") or {"revision": "initial", "workflow": {}, "running": False, "reason": "not_configured"}
        if name == "automation_status":
            return {**current, "active_job_id": next((v["job_id"] for v in self.store.list_jobs(state="running")), None)}
        if name == "automation_configure":
            if current.get("running"):
                raise ControlError("JOB_BUSY", "先停止调度再修改配置")
            self.require_revision(args["expected_revision"], current["revision"])
            workflow = args["workflow"]
            if "execute" in workflow["stages"] and workflow["max_platform_actions_per_day"] < 1:
                raise ControlError("VALIDATION_ERROR", "真实参与工作流必须设置正数每日动作预算")
            value = {"revision": digest(workflow), "workflow": workflow, "running": False, "reason": "configured"}
            self.store.put("workflow", "current", value)
            return value
        if name == "automation_stop":
            current.update(running=False, reason="user_stopped")
            self.store.put("workflow", "current", current)
            return {**current, "active_job_id": next((v["job_id"] for v in self.store.list_jobs(state="running")), None)}
        self.require_revision(args["workflow_revision"], current["revision"])
        if not current["workflow"]:
            raise ControlError("VALIDATION_ERROR", "请先配置工作流")
        if current.get("running"):
            return current
        current.update(running=True, reason="started", boot_id=BOOT_ID)
        self.store.put("workflow", "current", current)
        threading.Thread(target=self.workflow_loop, daemon=True, name="control-workflow").start()
        return current

    def workflow_loop(self):
        try:
            while True:
                current = self.store.get("workflow", "current")
                if not current or not current.get("running"):
                    return
                workflow = current["workflow"]
                with self.lock:
                    job = self.start("control_workflow", {"workflow": workflow}, require_llm=True)
                while self.job(job["job_id"])["state"] == "running":
                    time.sleep(0.5)
                completed = self.job(job["job_id"])
                if completed["state"] != "success":
                    raise ControlError("WORKFLOW_PAUSED", "本轮失败，调度已暂停")
                if (completed.get("result") or {}).get("paused"):
                    raise ControlError("WORKFLOW_PAUSED", "已保留并导入核实结果；本轮检索不完整，调度已暂停")
                until = time.monotonic() + workflow["interval_minutes"]*60
                while time.monotonic() < until:
                    if not (self.store.get("workflow", "current") or {}).get("running"):
                        return
                    time.sleep(0.5)
        except Exception as exc:
            current = self.store.get("workflow", "current") or {}
            current.update(running=False, reason=str(exc))
            self.store.put("workflow", "current", current)


def get_control_service():
    from src.app_paths import user_home
    home = user_home().resolve()
    with _instances_lock:
        if home not in _instances:
            _instances[home] = ControlService(home)
        return _instances[home]


def persist_runner_event(event, data):
    from src.log_redact import redact_obj
    from src.app_paths import user_home
    service = _instances.get(user_home().resolve())
    if service is None or data.get("id") is None:
        return
    with service.store.transaction() as db:
        rows = db.execute("SELECT id FROM control_jobs WHERE owner='app' AND backend_id=? "
                          "AND json_extract(payload,'$.boot_id')=?", (data["id"], BOOT_ID)).fetchall()
    for row in rows:
        service.store.event(row[0], {"kind": event, "data": redact_obj(data)})
