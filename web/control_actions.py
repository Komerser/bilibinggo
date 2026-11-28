"""Control jobs reuse the application worker and its platform clients."""
from __future__ import annotations
import re
import time
from datetime import date
from src.control_store import ControlError, digest, utc_now
from src.log_redact import redact_obj


def check_cancel(event):
    if event is not None and event.is_set():
        raise RuntimeError("任务已取消")


def run_control_action(action, params, *, on_progress, cancel_event):
    from src.control_telemetry import network_span
    workflow = params.get("workflow") or {}
    read_only = action in {"control_discover", "control_import", "control_discovery_filter", "control_network", "control_llm_test"} or action == "control_workflow" and "execute" not in workflow.get("stages", [])
    if action == "control_discover":
        budget = params.get("request_budget", 180)
    elif action == "control_import" and params.get("queue_verified"):
        budget = None
    else:
        budget = workflow.get("request_budget", 110)
    with network_span(read_only=read_only, max_requests=budget) as counts:
        result = _run_control_action(action, params, on_progress=on_progress, cancel_event=cancel_event)
        result["result"]["network_counts"] = {key: value for key, value in counts.items() if key.startswith("platform_")}
        return result


def _run_control_action(action, params, *, on_progress, cancel_event):
    from web.control_service import get_control_service
    service = get_control_service()
    job_id = params["_control_job_id"]
    check_cancel(cancel_event)
    on_progress(step=0, total=1, message="正在执行MCP控制任务")
    if action == "control_discover":
        result = discover(service, job_id, params, cancel_event, on_progress)
    elif action == "control_import":
        if params.get("queue_verified"):
            from web.discovery_queue import enqueue_verified
            with discovery_client(service, cancel_event, budget=None) as client:
                try:
                    result = enqueue_verified(service, params["discovery_job_id"], client, cancel_event, on_progress, params.get("selected_ids"))
                finally:
                    client.save()
        else:
            result = import_candidates(service, params)
    elif action == "control_discovery_filter":
        from web.discovery_filters import preview_filters
        from src.lottery_discovery import DiscoveryStopped
        with discovery_client(service, cancel_event, budget=110) as client:
            try:
                result = preview_filters(service, params["discovery_job_id"], params["settings"], client, on_progress)
            except DiscoveryStopped as exc:
                result = {"message": str(exc)}
            finally:
                client.save()
    elif action == "control_participate":
        result = execute_plan(service, job_id, params["plan_id"], on_progress, cancel_event)
    elif action == "control_network":
        result = diagnose(params["targets"])
    elif action == "control_llm_test":
        from src.llm_config import load_llm_config
        from src.llm_client import test_llm_connection
        config = load_llm_config()
        if not config:
            raise ControlError("LLM_NOT_READY", "未保存LLM配置")
        result = {"result": test_llm_connection(config)}
    elif action == "control_workflow":
        result = workflow(service, job_id, params["workflow"], on_progress, cancel_event)
    else:
        raise ControlError("NOT_FOUND", "未知控制任务")
    cancelled = False
    if action == "control_discover":
        cancelled = result.get("completion_reason") == "cancelled" or bool(cancel_event and cancel_event.is_set())
        prefix = ("已停止检索，进度已保存" if cancelled else {
            "request_budget": "本轮请求额度已用完，检索暂停，进度已保存",
            "platform_paused": "平台限制，检索暂停，进度已保存",
            "target_reached": "本轮检索已结束，可继续检索",
            "error": "检索中断，进度已保存",
            "search_exhausted": "搜索结果已耗尽，未达到新视频目标",
        }.get(result.get("completion_reason"), "检索完成"))
        if result.get("target_new_videos") and result.get("completion_reason") == "target_reached":
            prefix = "目标新视频检索完成"
        message = f"{prefix}：找到 {result['eligible']} 条可导入活动，检查 {result.get('videos_checked', 0)} 个视频"
        if result.get("deferred_videos"):
            message += f"，{result['deferred_videos']} 条低信号视频待复核"
    elif action == "control_discovery_filter":
        cancelled = bool(cancel_event and cancel_event.is_set())
        message = result.get("message") or f"筛选完成，匹配 {result['matched']} 条活动"
    elif action == "control_import" and params.get("queue_verified"):
        cancelled = bool(cancel_event and cancel_event.is_set())
        message = result["message"] or f"活动导入完成，本次报告已入库 {result['imported']} 条"
    else:
        check_cancel(cancel_event)
        message = "控制任务完成"
    if action == "control_discover" and result.get("target_new_videos"):
        on_progress(step=result.get("new_videos_checked", 0), total=result["target_new_videos"], message=message)
    else:
        on_progress(step=1, total=1, message=message)
    failed = action == "control_discover" and result.get("completion_reason") == "error"
    return {"ok": not (cancelled or failed), "cancelled": cancelled, "message": message, "log": message, "result": redact_obj(result)}


def discovery_candidates(rows):
    values = []
    for row in rows:
        url = row.get("url") or ""
        match = re.search(r"(?:/opus/|/dynamic/|t\.bilibili\.com/)(\d{15,22})", url)
        identifier = match[1] if match else None
        source = row.get("source_video") or url
        category = row.get("status", "review")
        evidence = row.get("evidence") or {}
        quotes = list(dict.fromkeys(v[:2000] for v in [*(evidence.get(k) for k in ("rules", "deadline", "draw")), row.get("rule_text")]
                                   if isinstance(v, str) and v))
        value = {"candidate_id": digest({"url": url, "source": source, "author": row.get("publisher_uid")})[:24],
            "dynamic_id": identifier, "source_url": source, "target_url": url or None,
            "resolution": "ambiguous" if row.get("ambiguous") else ("unresolved" if not identifier else ("author_home" if row.get("found_via") == "publisher_home" else "direct")),
            "evidence_chain": row.get("source_chain") or [{"source": source, "author_mid": row.get("publisher_uid"), "target": url}],
            "rule_quotes": quotes, "excluded_reasons": [] if category == "open" and identifier else [category, row.get("reason") or "not_eligible"],
            "eligible": category == "open" and identifier is not None, "checked_at": utc_now(), "raw": row}
        if str(row.get("publisher_uid") or "").isdigit() and int(row["publisher_uid"]) > 0:
            value["author_mid"] = str(row["publisher_uid"])
        values.append(value)
    return values


def discovery_client(service, event, *, budget, min_interval_seconds=2.0):
    from src.lottery_discovery import DiscoveryClient, DiscoveryStopped

    class CancellableClient(DiscoveryClient):
        stop_reason = ""

        def _wait_for_request(self, seconds):
            if event is None:
                return super()._wait_for_request(seconds)
            if event.wait(seconds):
                raise DiscoveryStopped("用户取消")
        def extract(self, *args, **kwargs):
            if event is not None and event.is_set():
                raise DiscoveryStopped("用户取消")
            return super().extract(*args, **kwargs)
        def read(self, *args, **kwargs):
            if event is not None and event.is_set():
                raise DiscoveryStopped("用户取消")
            return super().read(*args, **kwargs)
        def _http_get(self, *args, **kwargs):
            try:
                if event is not None and event.is_set():
                    raise DiscoveryStopped("用户取消")
                return super()._http_get(*args, **kwargs)
            except DiscoveryStopped as exc:
                self.stop_reason = str(exc)
                raise

    return CancellableClient(service.home / "data/lottery-discovery-cache.json", budget=budget,
                             min_interval_seconds=min_interval_seconds)


def discover(service, job_id, params, event, progress=None):
    from src.control_store import digest
    from src.lottery_discovery import expand_keywords
    from src.lottery_discovery_broad import discover_page as find
    words = expand_keywords(params["keywords"]) if params.get("expand_keywords") else params["keywords"]
    comprehensive = params.get("mode") == "comprehensive_page"
    target = params.get("target_new_videos")
    checkpoint_id = params.get("_discovery_checkpoint_id") or params.get("_queue_checkpoint_id") or "direct:" + digest(
        [words, params.get("video_limit", 36), comprehensive, params.get("author_dynamic_limit", 2), target]
    )
    checkpoint_state = service.store.discovery_checkpoint(checkpoint_id, max_age=7 * 86400)
    review_deferred = bool(params.get("_review_deferred"))
    if review_deferred and (not checkpoint_state or checkpoint_state.get("capture_id") != params.get("_capture_id")):
        raise ControlError("VALIDATION_ERROR", "待复核的检索进度已不可用")
    review_params = {key: params[key] for key in (
        "keywords", "keyword_match", "mode", "video_limit", "target_new_videos", "author_dynamic_limit",
        "expand_keywords", "candidate_limit", "collect_followers", "min_interval_seconds",
    ) if key in params}
    review_context = {"checkpoint_id": checkpoint_id, "params": review_params}
    stats_keys = ("target_new_videos", "new_videos_checked", "duplicates_skipped", "search_exhausted",
                  "deferred_videos", "comment_reads_saved")
    saved_checks = {}
    def publish(snapshot):
        checks = snapshot.get("video_checks", {})
        changed = {bvid: check for bvid, check in checks.items() if saved_checks.get(bvid) != check}
        value = {"job_id": job_id, "candidates": discovery_candidates(snapshot["rows"]),
                 "errors": [{"message": str(v)} for v in snapshot["errors"][:100]], "stopped": False, "requests_used": snapshot["requests"], "keywords": words,
                 **{k: snapshot[k] for k in ("session_id", "resumed", "scan_completed", "verification_remaining")}}
        target_stats = {k: snapshot[k] for k in stats_keys if k in snapshot}
        state = snapshot["checkpoint_state"]
        review_context["capture_id"] = state.get("capture_id")
        value["review_context"] = review_context
        value.update(raw={**target_stats, "has_more": not state["done"],
                          "resume_expires_at": int(time.time()) + 7 * 86400})
        service.store.save_discovery_checkpoint(
            checkpoint_id, state, job_id=job_id, report=redact_obj(value),
            checks=redact_obj(changed), video_page=snapshot.get("video_page"),
        )
        saved_checks.update(changed)
        if progress:
            progress(step=snapshot["videos_checked"], total=target or max(1, min(params.get("video_limit", 36), snapshot["search_video_count"])),
                     message=f"{snapshot['message']} · 请求 {snapshot['requests']}，可导入 {snapshot['eligible']}",
                     result_patch={"discovery_job_id": job_id, "eligible": snapshot["eligible"], "requests_used": snapshot["requests"],
                                   "videos_checked": snapshot["videos_checked"], "phase": snapshot["phase"], **target_stats,
                                   **{k: snapshot[k] for k in ("session_id", "resumed", "scan_completed", "verification_remaining")}})
    from src.participation_store import load_participations
    from src.activity_store import known_activity_ids
    joined = {str(k) for k, v in load_participations().items() if v.user_status == "已参加"}
    with discovery_client(service, event, budget=params.get("request_budget", 180),
                          min_interval_seconds=params.get("min_interval_seconds", 2.0)) as client:
        try:
            target_options = ({"target_new_videos": target, "known_bvids": service.store.discovery_video_ids()} if target is not None else {})
            report = find(client, words, video_limit=params.get("video_limit", 36), now=int(time.time()),
                          comprehensive_page=comprehensive, joined_ids=joined, known_ids=known_activity_ids(), on_progress=publish,
                          candidate_limit=params.get("candidate_limit", 9), author_dynamic_limit=params.get("author_dynamic_limit", 2),
                          resume=params.get("resume", True), replay_completed=params.get("replay_completed", True),
                          checkpoint_state=checkpoint_state, checkpoint_batches=True,
                          review_deferred=review_deferred, screen_videos=target is not None,
                          search_cutoffs={} if comprehensive else service.store.discovery_search_coverage(),
                          collect_followers=params.get("collect_followers", False), **target_options)
            rows = discovery_candidates([row for category in ("open", "review", "closed", "joined", "rejected", "source")
                                         for row in report.get(category, [])])
            value = {"job_id": job_id, "candidates": rows, "errors": [{"message": str(v)} for v in report["errors"][:100]],
                     "stopped": bool(report["stopped"]), "requests_used": report["requests"], "raw": report, "keywords": words,
                    **{k: report[k] for k in ("session_id", "resumed", "scan_completed", "has_more", "verification_remaining", "sources_remaining")}}
            value["raw"]["resume_expires_at"] = int(time.time()) + 7 * 86400
            value["review_context"] = review_context
            service.store.put("discovery", str(job_id), redact_obj(value))
            from web.app import app
            from web.cluster_protocol import PublisherFinding

            cluster = getattr(app.state, "cluster", None)
            if cluster:
                findings = []
                for item in rows:
                    raw = item["raw"]
                    mid = item.get("author_mid", "")
                    dynamic = item.get("dynamic_id") or ""
                    published = raw.get("published_at")
                    if (item["eligible"] and not raw.get("is_forward") and re.fullmatch(r"[1-9][0-9]{0,19}", mid)
                            and re.fullmatch(r"[0-9]{18,19}", dynamic) and published):
                        findings.append(PublisherFinding(activity=dynamic, mid=mid,
                            name=str(raw.get("publisher") or mid)[:64], published=published))
                if findings:
                    cluster.store.record_publishers(findings)
        finally:
            try:
                client.save()
            except OSError:
                from src.app_logging import get_logger
                get_logger("job").warning("检索缓存未写入，已提交的检查点仍可恢复", exc_info=True)
    return {"discovery_job_id": job_id, "candidates": len(rows), "eligible": sum(v["eligible"] for v in rows),
            "requests_used": client.request_count, "partial": bool(value["stopped"] or value["errors"] or report["has_more"]),
            "errors": value["errors"],
            **{k: report[k] for k in stats_keys if k in report},
            **{k: report[k] for k in ("videos_checked", "search_video_count", "completion_reason", "cache_hits", "llm_requests", "llm_cache_hits",
                                     "session_id", "resumed", "scan_completed", "has_more", "verification_remaining", "sources_remaining",
                                     "min_interval_seconds", "elapsed_seconds")}}


def import_candidates(service, params):
    from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
    from web.discovery_queue import is_verified_candidate
    report = service.store.get("discovery", str(params["discovery_job_id"]))
    if not report:
        raise ControlError("NOT_FOUND", "发现报告不存在")
    mapping = {v["candidate_id"]: v for v in report["candidates"]}
    if not set(params["candidate_ids"]).issubset(mapping):
        raise ControlError("VALIDATION_ERROR", "候选ID不属于该报告")
    selected = [mapping[k] for k in dict.fromkeys(params["candidate_ids"])]
    if any(not is_verified_candidate(v) for v in selected):
        raise ControlError("VALIDATION_ERROR", "仅允许导入已解析的可用动态活动")
    if any((v.get("raw") or {}).get("draw_time") and v["raw"]["draw_time"] <= time.time() for v in selected):
        raise ControlError("PLAN_STALE", "候选开奖时间已过，请重新检索")
    result = run_new_links_pipeline([v["target_url"] for v in selected], workers=1)
    if not result.ok:
        raise ControlError("PLATFORM_REJECTED", result.message)
    from web.activity_service import invalidate_activity_cache
    invalidate_activity_cache()
    return {"imported": result.persisted_count, "classified": result.classified_count, "message": result.message}


def execute_plan(service, job_id, plan_id, progress, event):
    from src.bilibili_client import BilibiliClient
    from src.forward_parser import _extract_from_detail_item
    from src.lottery_api import fetch_dynamic_detail
    from src.participate_preflight import ensure_activity_participatable
    from src.participation import participate_activity
    plan = service.get_plan(plan_id)
    report = {"targets": plan["targets"], "action_text": plan["action_text"], "target_texts": plan.get("target_texts", {}), "actions": [], "participation_outcome": "unknown", "results": []}
    service.store.put("participation", str(job_id), report)
    with BilibiliClient(timeout=12, warmup=False) as client:
        for index, target in enumerate(plan["targets"]):
            check_cancel(event)
            service.get_plan(plan_id)
            identifier, kind = target["dynamic_id"], target["lottery_type"]
            detail = fetch_dynamic_detail(client, identifier)
            if not detail or digest(_extract_from_detail_item(detail)) != plan["rule_snapshots"][identifier]:
                raise ControlError("PLAN_STALE", "活动正文已变化，未继续执行")
            ensure_activity_participatable(client, identifier, lottery_type_hint=kind)
            key = f"{plan['account_ref']}:{identifier}"
            prior = service.store.get("participation_intent", key)
            if prior:
                raise ControlError("OUTCOME_UNKNOWN", "目标已有执行记录，请先核验原任务", job_id=prior["job_id"])
            service.store.put("participation_intent", key, {"job_id": job_id, "plan_id": plan_id, "state": "sending", "ts": utc_now()})
            progress(step=index, total=len(plan["targets"]), message=f"正在参与 {identifier}")
            def on_step(step, total, message, action):
                check_cancel(event)
                progress(step=index * total + step, total=len(plan["targets"]) * total, message=message)
            recipe = {v["action"]: v for v in plan["actions"] if v["dynamic_id"] == identifier}
            allowed = {name for name, action in recipe.items() if not action.get("skipped")}
            value = participate_activity(client, dynamic_id=identifier, lottery_type=kind, action_text=plan.get("target_texts", {}).get(identifier, plan["action_text"]), dry_run=False, persist=True, on_step=on_step, allowed_actions=allowed)
            for item in value.actions:
                skipped = bool(recipe.get(item.action, {}).get("skipped"))
                report["actions"].append({"dynamic_id": identifier, "action": item.action, "required": recipe.get(item.action, {}).get("required", True), "attempted": not skipped,
                    "verification": "skipped" if skipped and not recipe.get(item.action, {}).get("required", True) else ("accepted" if item.ok else "failed"), "reason": item.detail, "remote_id": None, "evidence_artifact_ids": []})
            report["results"].append(redact_obj(value.to_dict()))
            report["participation_outcome"] = "joined" if all(v["status"] == "joined" for v in report["results"]) and len(report["results"]) == len(plan["targets"]) else "partial"
            service.store.put("participation", str(job_id), report)
            service.store.put("participation_intent", key, {"job_id": job_id, "state": value.status, "ts": utc_now()})
            if value.status != "joined":
                raise ControlError("PLATFORM_REJECTED", value.message, job_id=job_id)
    return {"participation_outcome": report["participation_outcome"], "participation_job_id": job_id}


def diagnose(targets):
    import httpx
    from src.bilibili_client import BilibiliClient, api_code
    rows = []
    for target in targets:
        started = time.monotonic()
        try:
            if target == "control":
                value = {"ok": True, "source": "application-process"}
            elif target == "bilibili":
                with BilibiliClient(timeout=8, warmup=False) as client:
                    payload = client.request_json("https://api.bilibili.com/x/web-interface/nav", retries=0)
                value = {"ok": api_code(payload) in {0, -101}, "platform_code": api_code(payload), "logged_in": bool((payload.get("data") or {}).get("isLogin"))}
            elif target == "updates":
                from src.update_check import check_for_updates
                payload = check_for_updates().to_dict()
                value = {"ok": payload.get("ok", False), "message": payload.get("message") or payload.get("error")}
            else:
                from src.llm_config import load_llm_config
                config = load_llm_config()
                if not config:
                    raise ControlError("LLM_NOT_READY", "未保存LLM配置")
                from src.usage_stats import record_llm_request
                with httpx.Client(timeout=8, follow_redirects=False) as client:
                    day = date.today()
                    try:
                        response = client.get(config.base_url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + config.api_key})
                    finally:
                        record_llm_request(config, day, completion=False)
                value = {"ok": response.status_code < 400, "http_status": response.status_code}
            rows.append({"target": target, "elapsed_ms": int((time.monotonic()-started)*1000), **value})
        except Exception as exc:
            from src.log_redact import redact_text
            rows.append({"target": target, "ok": False, "error_kind": type(exc).__name__, "message": redact_text(str(exc))[:500], "elapsed_ms": int((time.monotonic()-started)*1000)})
    return {"probes": rows, "network_state": "online" if all(v["ok"] for v in rows) else "degraded"}


def workflow(service, job_id, config, progress, event):
    from web.actions import run_action
    summary, stages = {}, config["stages"]
    if "refresh" in stages:
        for source_id in config.get("source_ids") or []:
            check_cancel(event)
            run_action("refresh_source", {"source_id": source_id}, on_progress=progress, cancel_event=event)
    if "discover" in stages:
        reserve = min(config["request_budget"] // 3, 8 * config["max_activities_per_run"])
        summary["discovery"] = discover(service, job_id, {"keywords": config.get("keywords") or ["转发抽奖"],
            "request_budget": config["request_budget"] - reserve, "video_limit": 36, "mode": "standard",
            "candidate_limit": config["max_activities_per_run"]}, event, progress)
        report = service.store.get("discovery", str(job_id))
        from web.discovery_queue import verified_candidates
        chosen = [v["candidate_id"] for v in verified_candidates(report).values()][:config["max_activities_per_run"]]
        if chosen:
            summary["import"] = import_candidates(service, {"discovery_job_id": job_id, "candidate_ids": chosen})
        if summary["discovery"]["partial"]:
            return {**summary, "paused": True, "pause_reason": "discovery_incomplete", "platform_execution": "not_run"}
        if not chosen:
            return {**summary, "skipped": "no_eligible_targets"}
    if "plan" in stages or "execute" in stages:
        from web.activity_service import pick_triple_participate_targets
        rows = ([v for v in report["candidates"] if v["candidate_id"] in chosen]
                if "discover" in stages else pick_triple_participate_targets()[:config["max_activities_per_run"]])
        if not rows:
            return {**summary, "skipped": "no_eligible_targets"}
        # This code already owns the application's business worker. Public planning
        # rejects a busy worker; invoking that public gate here would reject itself.
        plan = service._make_plan([str(v["dynamic_id"]) for v in rows], 300)
        summary["plan_id"] = plan["plan_id"]
        if "execute" in stages:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            date = datetime.now(ZoneInfo(config["timezone"])).date().isoformat()
            budget = service.store.get("daily_budget", date) or {"reserved": 0}
            count = sum(not v.get("skipped", False) for v in plan["actions"])
            if budget["reserved"] + count > config["max_platform_actions_per_day"]:
                raise ControlError("RATE_LIMITED", "每日动作预算已用尽")
            service.store.put("daily_budget", date, {"reserved": budget["reserved"] + count})
            service.store.put("consumed_plan", plan["plan_id"], {"job_id": job_id})
            summary["participation"] = execute_plan(service, job_id, plan["plan_id"], progress, event)
            if "verify" in stages:
                summary["verification"] = service._verify(job_id)
    return summary
