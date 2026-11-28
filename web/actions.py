from __future__ import annotations

import contextvars
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Callable

from src.app_logging import get_logger
from src.bilibili_auth import get_login_uid
from src.bilibili_client import BilibiliClient
from src.bilibili_login import login_with_qrcode
from src.fetch_activity_info import mark_enriched_joined
from src.log_redact import redact_text
from src.log_span import PhaseSpanTracker, log_span
from src.lottery_actions import ActionResult
from src.lottery_classifier import PARTICIPATABLE_TYPES
from src.participate_preflight import ActivityPreflightRejected, ensure_activity_participatable
from src.participation import participate_activity
from src.participation_log import participation_succeeded
from src.pipeline.refresh_all_pipeline import PipelineResult, run_batched_new_links_pipeline, run_refresh_all_pipeline
from src.sources import (
    ds1_xiaozhuli,
    ds2_fanqiao,
    ds3_gongjuren,
    ds4_junming,
    ds5_hudong,
    ds6_nuomi,
    ds7_dajinli,
)
from src.sources.common import CheckResult, commit_source_checkpoint, is_valid_dynamic_id, normalize_activity_id
from src.sources.pending_links import clear_processed_source_links, pending_activity_urls, stage_source_links
from src.state_store import get_watch_last_synced_at, set_last_pipeline_persisted, set_watch_last_synced_at
from src.status_refresh import refresh_local_activity_statuses
from web.activity_service import (
    PARTICIPATE_TRIPLE_LIMIT,
    build_triple_progress_plan,
    build_triple_target_preview,
    invalidate_activity_cache,
    lookup_lottery_type,
    participate_step_budget,
    pick_triple_participate_targets,
    resolve_participate_lottery_type,
)
from src.watch_sync import save_watch_result, sync_watch_forwards
from web.user_messages import format_participation_log, sanitize_log

logger = get_logger("job")

PARTICIPATE_TRIPLE_WORKERS = PARTICIPATE_TRIPLE_LIMIT
REFRESH_ALL_PIPELINE_SUBSTEPS = 3
REFRESH_WATCH_PIPELINE_SUBSTEPS = 3
REFRESH_WATCH_TOTAL = 1 + REFRESH_WATCH_PIPELINE_SUBSTEPS


def _deserialize_payload_actions(payload: dict[str, Any]) -> list[ActionResult]:
    actions: list[ActionResult] = []
    for item in payload.get("actions") or []:
        if not isinstance(item, dict):
            continue
        action_name = str(item.get("action") or "").strip()
        if not action_name:
            continue
        actions.append(
            ActionResult(
                action=action_name,  # type: ignore[arg-type]
                ok=bool(item.get("ok")),
                detail=str(item.get("detail") or ""),
            )
        )
    return actions


def _payload_joined_success(payload: dict[str, Any]) -> bool:
    if str(payload.get("status") or "") != "joined":
        return False
    lottery_type = str(payload.get("lottery_type") or "")
    actions = _deserialize_payload_actions(payload)
    if not actions:
        return False
    if lottery_type in ("互动抽奖", "转发抽奖", "预约抽奖"):
        return participation_succeeded(actions, lottery_type=lottery_type)
    return all(item.ok for item in actions)


class ParticipationFailed(RuntimeError):
    """Keep completed steps available to both the UI and control clients."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(str(payload.get("message") or "参与失败"))
        self.participation_result = payload


def _require_participate_success(payload: dict[str, Any]) -> None:
    status = str(payload.get("status") or "")
    if status == "skipped":
        raise RuntimeError(str(payload.get("message") or "该活动不可参与"))
    if status != "joined" or not _payload_joined_success(payload):
        raise ParticipationFailed(payload)


def _normalize_participate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _require_participate_success(payload)
    return dict(payload)


def _participate_dynamic_payload(
    dynamic_id: str,
    on_step: Callable[[int, int, str, str], None],
    *,
    lottery_type_hint: str | None = None,
    client: BilibiliClient | None = None,
) -> dict[str, Any]:
    resolved_type = resolve_participate_lottery_type(dynamic_id, hint=lottery_type_hint)
    if client is not None:
        payload = _execute_participate(
            dynamic_id,
            on_step,
            lottery_type=resolved_type,
            client=client,
        )
    else:
        payload, _detail = _capture_output(
            _execute_participate,
            dynamic_id,
            on_step,
            lottery_type=resolved_type,
        )
    return _normalize_participate_payload(payload)


def _list_filter_params(params: dict[str, Any]) -> dict[str, str | None]:
    return {
        "status": str(params.get("status") or "").strip() or None,
        "lottery_type": str(params.get("lottery_type") or params.get("type") or "").strip() or None,
        "draw": str(params.get("draw") or "").strip() or None,
        "draw_window": str(params.get("draw_window") or "").strip() or None,
        "q": str(params.get("q") or "").strip() or None,
        "sort": str(params.get("sort") or "").strip() or None,
        "order": str(params.get("order") or "").strip() or None,
    }


def _execute_participate(
    dynamic_id: str,
    on_step: Callable[[int, int, str, str], None],
    *,
    lottery_type: str | None = None,
    client: BilibiliClient | None = None,
) -> dict[str, Any]:
    dynamic_id = str(dynamic_id or "").strip()
    if not is_valid_dynamic_id(dynamic_id):
        raise ValueError(f"活动 ID 无效: {dynamic_id}")
    resolved_type = str(lottery_type or "").strip()
    if resolved_type not in PARTICIPATABLE_TYPES:
        resolved_type = resolve_participate_lottery_type(dynamic_id)

    def _run(active_client: BilibiliClient) -> dict[str, Any]:
        result = participate_activity(
            active_client,
            dynamic_id=dynamic_id,
            lottery_type=resolved_type,
            dry_run=False,
            persist=True,
            on_step=on_step,
        )
        payload = result.to_dict()
        payload["lottery_type"] = resolved_type
        return payload

    if client is not None:
        return _run(client)
    with BilibiliClient() as owned_client:
        return _run(owned_client)


def _append_log_detail(log_lines: list[str], detail: str) -> None:
    cleaned = sanitize_log(detail.strip())
    if cleaned:
        log_lines.append(cleaned)


DS_HANDLERS: list[tuple[str, Callable[..., Any], Callable[[Any], Any]]] = [
    ("DS-1", ds1_xiaozhuli.check_update, ds1_xiaozhuli.save_result),
    ("DS-2", ds2_fanqiao.check_update, ds2_fanqiao.save_result),
    ("DS-3", ds3_gongjuren.check_update, ds3_gongjuren.save_result),
    ("DS-4", ds4_junming.check_update, ds4_junming.save_result),
    ("DS-5", ds5_hudong.check_update, ds5_hudong.save_result),
    ("DS-6", ds6_nuomi.check_update, ds6_nuomi.save_result),
    ("DS-7", ds7_dajinli.check_update, ds7_dajinli.save_result),
]
DS_HANDLER_BY_ID: dict[str, tuple[Callable[..., Any], Callable[[Any], Any]]] = {
    source_id: (check_update, save_result)
    for source_id, check_update, save_result in DS_HANDLERS
}

REFRESH_ALL_TOTAL = len(DS_HANDLERS) + REFRESH_ALL_PIPELINE_SUBSTEPS
REFRESH_SOURCE_TOTAL = 1 + REFRESH_ALL_PIPELINE_SUBSTEPS
ProgressCallback = Callable[..., None]


_SUBPROGRESS_SUFFIX_RE = re.compile(r"(?:\s*\(\s*\d+\s*/\s*\d+\s*\))+\s*$")


def _format_subprogress_message(message: str, done: int, total: int) -> str:
    base = _SUBPROGRESS_SUFFIX_RE.sub("", str(message or "").strip()).strip()
    if total <= 0:
        return base
    return f"{base} ({done}/{total})"


def _pipeline_substep_index(message: str) -> int:
    text = str(message or "")
    if "入库" in text or "落库" in text or "写入活动库" in text:
        return 3
    if "详情进度" in text or "活动详情" in text:
        return 2
    if "分类" in text or "新链接" in text:
        return 1
    return 1


_PIPELINE_PHASE_BY_SUBSTEP = {
    1: ("pipeline_classify", "pipeline_classify"),
    2: ("pipeline_detail", "pipeline_detail"),
    3: ("pipeline_persist", "pipeline_persist"),
}


def _make_refresh_all_pipeline_progress(
    progress: ProgressCallback,
    *,
    ds_count: int,
    span_tracker: PhaseSpanTracker | None = None,
    cancel_event: threading.Event | None = None,
) -> Callable[[int, int, str], None]:
    def on_pipeline_progress(done: int, total: int, message: str) -> None:
        _raise_if_cancelled(cancel_event)
        substep = _pipeline_substep_index(message)
        if span_tracker is not None:
            phase_name = _PIPELINE_PHASE_BY_SUBSTEP.get(substep)
            if phase_name:
                phase, span_name = phase_name
                span_tracker.set_phase(phase, name=span_name)
        progress(
            step=ds_count + substep,
            total=ds_count + REFRESH_ALL_PIPELINE_SUBSTEPS,
            message=_format_subprogress_message(message, done, total),
        )

    return on_pipeline_progress


def _make_refresh_watch_pipeline_progress(
    progress: ProgressCallback,
    *,
    span_tracker: PhaseSpanTracker | None = None,
) -> Callable[[int, int, str], None]:
    def on_pipeline_progress(done: int, total: int, message: str) -> None:
        substep = _pipeline_substep_index(message)
        if span_tracker is not None:
            phase_name = _PIPELINE_PHASE_BY_SUBSTEP.get(substep)
            if phase_name:
                phase, span_name = phase_name
                span_tracker.set_phase(phase, name=span_name)
        progress(
            step=1 + substep,
            total=REFRESH_WATCH_TOTAL,
            message=_format_subprogress_message(message, done, total),
        )

    return on_pipeline_progress


def _noop_progress(**_kwargs: Any) -> None:
    return None


def _capture_output(func: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        result = func(*args, **kwargs)
    return result, buffer.getvalue()


def _pipeline_log_lines(result: PipelineResult) -> list[str]:
    if result.pipeline_skipped:
        return [f"【流水线】{result.message}"]
    skip_detail = ""
    if result.skip_reasons:
        parts = [f"{reason} {count}" for reason, count in result.skip_reasons.items()]
        skip_detail = f"，跳过 {result.skipped_count} 条（{' / '.join(parts)}）"
    deferred_detail = f"，待重试 {len(result.deferred)} 条" if result.deferred else ""
    return [
        f"【新链接流水线】候选 {result.new_link_count} 条，"
        f"分类通过 {result.classified_count} 条，"
        f"详情 {result.enriched_count} 条，"
        f"入库 {result.persisted_count} 条{skip_detail}{deferred_detail}",
        *[f"【待重试】{item.dynamic_id} · {item.stage} · {item.reason}" for item in result.deferred],
    ]


def _run_ds_check(
    index: int,
    source_id: str,
    check_update: Callable[..., Any],
    save_result: Callable[[Any], Any],
) -> tuple[int, dict[str, Any], str, CheckResult]:
    with log_span(
        f"ds_check:{source_id}",
        logger=logger,
        component="ds",
        source_id=source_id,
        phase="ds_check",
    ):
        result, detail = _capture_output(check_update, force=False)
        out_path = save_result(result)
    status_text = "发现新专栏，已爬取" if result.updated else "同一专栏，已跳过"
    log_line = f"【{source_id}】{status_text}，共 {len(result.activity_links)} 条链接"
    payload = {
        "source_id": source_id,
        "updated": result.updated,
        "link_count": len(result.activity_links),
        "saved": bool(out_path),
        "log_line": log_line,
        "detail": detail.strip(),
        "status_text": status_text,
    }
    return index, payload, log_line, result


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError("任务已取消")


def run_action(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    params = params or {}
    progress = on_progress or _noop_progress
    log_lines: list[str] = []
    _raise_if_cancelled(cancel_event)
    if action == "refresh_publishers":
        from web.app import app

        result = app.state.cluster.refresh_publishers(progress, cancel_event)
        return {"ok": True, "message": result.pop("message"), "result": result}

    if action.startswith("control_"):
        from web.control_actions import run_control_action
        return run_control_action(action, params, on_progress=progress, cancel_event=cancel_event)

    if action == "login":
        qrcode_refresh_count = 0

        def on_qrcode_ready() -> None:
            nonlocal qrcode_refresh_count
            qrcode_refresh_count += 1
            refreshed_at = int(time.time())
            if qrcode_refresh_count == 1:
                progress(
                    step=1,
                    total=1,
                    message="请使用哔哩哔哩 App 扫码登录",
                    log_append="已生成登录二维码",
                    login_phase="waiting",
                    qrcode_refreshed_at=refreshed_at,
                )
            else:
                progress(
                    step=1,
                    total=1,
                    message="二维码已刷新，请重新扫码",
                    log_append="二维码已自动刷新",
                    login_phase="waiting",
                    qrcode_refreshed_at=refreshed_at,
                )

        def on_login_status(phase: str, message: str) -> None:
            log_append = {
                "scanned": "已在手机扫码，等待确认",
                "confirming": "用户已确认，正在写入登录信息",
                "refreshing": "二维码已过期，请重新扫码",
            }.get(phase)
            progress(
                step=1,
                total=1,
                message=message,
                log_append=log_append,
                login_phase=phase,
            )

        progress(step=1, total=1, message="正在生成登录二维码…", login_phase="waiting")
        _, log = _capture_output(
            login_with_qrcode,
            open_image=False,
            auto_refresh_on_expire=False,
            cancel_event=cancel_event,
            on_qrcode_ready=on_qrcode_ready,
            on_status_change=on_login_status,
        )
        progress(step=1, total=1, message="登录成功，账号已就绪", login_phase="success")
        return {
            "ok": True,
            "message": "登录成功，账号已就绪",
            "log": sanitize_log(log) or "登录成功",
            "result": {"login_phase": "success"},
        }

    if action in {"refresh_all", "refresh_source"}:
        source_id = str(params.get("source_id") or "").strip() if action == "refresh_source" else None
        if source_id is None:
            handlers = DS_HANDLERS
            label = "一键更新"
        else:
            handler = DS_HANDLER_BY_ID.get(source_id)
            if not handler:
                raise ValueError(f"未知数据源：{source_id}")
            handlers = [(source_id, *handler)]
            label = source_id

        ds_count = len(handlers)
        total = ds_count + REFRESH_ALL_PIPELINE_SUBSTEPS
        progress(
            step=0,
            total=total,
            message=f"正在检查 {source_id}…" if source_id else f"正在并行检查 {ds_count} 个数据源…",
        )
        _raise_if_cancelled(cancel_event)

        sources: dict[str, dict[str, Any]] = {}
        checks: dict[str, CheckResult] = {}
        source_errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=ds_count) as executor:
            # 每个 future 各自 copy_context：同一 Context 不能被多线程并发 enter
            futures = {
                executor.submit(
                    contextvars.copy_context().run, _run_ds_check, index, sid, check_update, save_result,
                ): sid
                for index, (sid, check_update, save_result) in enumerate(handlers, start=1)
            }
            for done, future in enumerate(as_completed(futures), start=1):
                _raise_if_cancelled(cancel_event)
                sid = futures[future]
                try:
                    _, payload, log_line, check = future.result()
                except Exception as exc:
                    reason = redact_text(str(exc)).replace("\n", " ").strip()[:300] or type(exc).__name__
                    source_errors[sid] = reason
                    source = {
                        "source_id": sid, "updated": False, "link_count": 0, "saved": False, "error": reason,
                    }
                    status_text = "检查失败，其余项目继续"
                    log_line = f"【{sid}】检查失败：{reason}"
                    logger.warning("%s 检查失败：%s", sid, reason)
                else:
                    checks[sid] = check
                    stage_source_links([sid], [check])
                    commit_source_checkpoint(check)
                    source = {key: payload[key] for key in ("source_id", "updated", "link_count", "saved")}
                    status_text = payload["status_text"]
                sources[sid] = source
                log_lines.append(log_line)
                progress(step=done, total=total, message=f"{sid}：{status_text}", log_append=log_line)

        source_ids = [sid for sid, _, _ in handlers]
        ds_checks = [checks[sid] for sid in source_ids if sid in checks]
        sources_updated = sum(check.updated for check in ds_checks)
        _raise_if_cancelled(cancel_event)
        pending = stage_source_links(source_ids, [])
        links = pending_activity_urls(pending)

        if source_errors and not ds_checks and not links:
            errors = "；".join(f"{sid}：{reason}" for sid, reason in source_errors.items())
            raise RuntimeError(f"数据源检查失败：{errors}")

        result: dict[str, Any] = {
            "sources": [sources[sid] for sid in source_ids],
            "sources_updated": sources_updated,
            "source_errors": source_errors,
        }
        if source_id is not None:
            result.update(source_id=source_id, source=sources[source_id])

        if links or sources_updated:
            progress(
                step=ds_count + 1,
                total=total,
                message="正在分类新链接…" if sources_updated else f"正在重试 {len(links)} 条待处理链接…",
            )
            spans = PhaseSpanTracker(logger=logger, component="pipeline")
            pipeline_error: str | None = None
            def acknowledge_batch(batch, deferred):
                clear_processed_source_links(pending, deferred, processed_ids=set(batch))
                invalidate_activity_cache()

            try:
                pipeline = run_refresh_all_pipeline(
                    ds_checks,
                    pending_links=links,
                    on_batch=acknowledge_batch,
                    on_progress=_make_refresh_all_pipeline_progress(
                        progress, ds_count=ds_count, span_tracker=spans, cancel_event=cancel_event,
                    ),
                )
            except Exception as exc:
                pipeline_error = type(exc).__name__
                raise
            finally:
                spans.close(error_kind=pipeline_error)
            _raise_if_cancelled(cancel_event)
            if not pipeline.ok:
                raise RuntimeError(pipeline.message or "活动链接流水线失败")

            invalidate_activity_cache()
            pipeline_logs = _pipeline_log_lines(pipeline)
            log_lines.extend(pipeline_logs)
            result["pipeline"] = pipeline.to_dict()
            persisted = pipeline.persisted_count
            summary = [f"{sources_updated} 个数据源有新专栏", f"新链接 {pipeline.new_link_count} 条", f"新入库 {persisted} 条"]
            if pipeline.skipped_count:
                summary.append(f"跳过 {pipeline.skipped_count} 条")
            if pipeline.deferred:
                summary.append(f"{len(pipeline.deferred)} 条待重试")
        else:
            persisted = 0
            result.update(pipeline_skipped=True, new_link_count=0, persisted_count=0)
            summary = ["无新专栏" if not source_errors else "已检查的来源无新专栏"]
            pipeline_logs = ["【跳过流水线】没有新链接或待重试链接"]
            log_lines.extend(pipeline_logs)

        if source_errors:
            summary.append(f"{len(source_errors)} 个数据源检查失败")
        message = f"{label}完成：" + "，".join(summary)
        logger.info("%s", message)
        set_last_pipeline_persisted(action=action, persisted_count=persisted)
        progress(step=total, total=total, message=message, log_append="\n".join(pipeline_logs))
        return {
            "ok": True,
            "message": message,
            "result": result,
            "log": sanitize_log("\n".join(log_lines).strip()),
        }

    if action == "refresh_watch":
        from web.control_service import get_control_service

        log_lines: list[str] = []
        store = get_control_service().store
        checkpoint_id = params.get("_queue_checkpoint_id", "direct")
        checkpoint = store.get("watch_checkpoint", checkpoint_id) or {}
        saved_scan = checkpoint.get("scan")
        processed = set(checkpoint.get("processed") or [])

        def save_watch_scan(scan):
            nonlocal saved_scan
            if saved_scan and (saved_scan["users"], saved_scan["window_end"]) != (scan["users"], scan["window_end"]):
                processed.clear()
            saved_scan = scan
            store.put("watch_checkpoint", checkpoint_id, {"scan": scan, "processed": sorted(processed)})

        progress(step=0, total=REFRESH_WATCH_TOTAL, message="准备扫描监控用户动态…")
        _raise_if_cancelled(cancel_event)

        def on_watch_progress(done: int, total: int, message: str) -> None:
            _raise_if_cancelled(cancel_event)
            progress(step=1, total=REFRESH_WATCH_TOTAL, message=message)

        with log_span("watch_scan", logger=logger, component="pipeline", phase="watch_scan"):
            watch_result = sync_watch_forwards(on_progress=on_watch_progress, checkpoint=saved_scan,
                                             on_checkpoint=save_watch_scan)
        _raise_if_cancelled(cancel_event)
        watch_line = (
            f"【监控扫描】窗口内 {watch_result.link_count} 条活动链接，"
            f"成功扫描 {watch_result.users_ok}/{watch_result.users_total} 人"
        )
        log_lines.append(watch_line)
        progress(
            step=1,
            total=REFRESH_WATCH_TOTAL,
            message=f"扫描完成：窗口 {watch_result.link_count} 条链接",
            log_append=watch_line,
        )

        pipeline_step = 2
        progress(step=pipeline_step, total=REFRESH_WATCH_TOTAL, message="正在分类新链接…")
        _raise_if_cancelled(cancel_event)

        pipeline_spans = PhaseSpanTracker(logger=logger, component="pipeline")
        pipeline_error: str | None = None
        def acknowledge_watch_batch(batch, deferred):
            processed.update(set(batch) - deferred)
            save_watch_scan(saved_scan)
            invalidate_activity_cache()

        try:
            pipeline_result = run_batched_new_links_pipeline(
                [url for url in watch_result.activity_links if normalize_activity_id(url) not in processed],
                on_batch=acknowledge_watch_batch,
                on_progress=_make_refresh_watch_pipeline_progress(
                    progress,
                    span_tracker=pipeline_spans,
                ),
            )
        except Exception as exc:
            pipeline_error = type(exc).__name__
            raise
        finally:
            pipeline_spans.close(error_kind=pipeline_error)
        _raise_if_cancelled(cancel_event)
        invalidate_activity_cache()
        for line in _pipeline_log_lines(pipeline_result):
            log_lines.append(line)
        progress(
            step=REFRESH_WATCH_TOTAL,
            total=REFRESH_WATCH_TOTAL,
            message=pipeline_result.message or "流水线完成",
            log_append=log_lines[-1] if log_lines else "",
        )

        save_watch_result(watch_result)
        set_watch_last_synced_at(max(get_watch_last_synced_at() or 0, watch_result.synced_at))
        store.put("watch_checkpoint", checkpoint_id, {})
        set_last_pipeline_persisted(
            action="refresh_watch",
            persisted_count=pipeline_result.persisted_count,
            synced_at=watch_result.synced_at,
        )

        summary_parts = [
            f"扫描 {watch_result.users_total} 人，提取 {watch_result.link_count} 条链接",
            f"新链接 {pipeline_result.new_link_count} 条",
            f"新入库 {pipeline_result.persisted_count} 条",
        ]
        logger.info(
            "监控用户更新完成：窗口 %s 条，入库 %s",
            watch_result.link_count,
            pipeline_result.persisted_count,
        )
        return {
            "ok": True,
            "message": "监控用户动态更新完成：" + "，".join(summary_parts),
            "result": {
                "watch": watch_result.to_dict(),
                "pipeline": pipeline_result.to_dict(),
            },
            "log": sanitize_log("\n".join(log_lines).strip()),
        }

    if action == "refresh_status":
        progress(step=0, total=1, message="正在刷新活动状态…")
        _raise_if_cancelled(cancel_event)
        with log_span(
            "status_refresh",
            logger=logger,
            component="pipeline",
            phase="status_refresh",
        ):
            result = refresh_local_activity_statuses()
        _raise_if_cancelled(cancel_event)
        invalidate_activity_cache()
        if result.get("skipped"):
            message = "没有需要刷新的进行中活动"
        else:
            message = (
                f"状态刷新完成：检查 {result.get('scanned', 0)} 条，"
                f"更新 {result.get('updated', 0)} 条，"
                f"新结束 {result.get('ended_marked', 0)} 条，"
                f"即将开奖 {result.get('soon_marked', 0)} 条"
            )
        progress(step=1, total=1, message=message, log_append=message)
        logger.info(
            "刷新任务状态完成：检查 %s，更新 %s",
            result.get("scanned", 0),
            result.get("updated", 0),
        )
        return {
            "ok": True,
            "message": message,
            "result": result,
            "log": sanitize_log(message),
        }

    if action == "participate":
        dynamic_id = str(params.get("dynamic_id") or "").strip()
        if not is_valid_dynamic_id(dynamic_id):
            raise ValueError("活动 ID 无效")
        _raise_if_cancelled(cancel_event)
        try:
            lottery_type = lookup_lottery_type(dynamic_id)
        except RuntimeError as exc:
            logger.warning("参与失败：未找到活动类型 %s — %s", dynamic_id, exc)
            raise ValueError(f"未找到活动 {dynamic_id} 的类型信息，请先一键更新") from exc
        total_steps = participate_step_budget(lottery_type, dynamic_id=dynamic_id)
        progress(step=0, total=total_steps, message="正在检查活动状态…", log_append="正在打开活动链接检查状态…")
        logger.info("开始参与活动 %s (%s)", dynamic_id, lottery_type)

        def on_step(step: int, total: int, message: str, _action_name: str) -> None:
            _raise_if_cancelled(cancel_event)
            progress(step=step, total=total, message=message, log_append=message)

        with BilibiliClient() as client:
            _raise_if_cancelled(cancel_event)
            ensure_activity_participatable(client, dynamic_id, lottery_type_hint=lottery_type)
            _raise_if_cancelled(cancel_event)
            progress(step=0, total=total_steps, message="检查通过，开始参与…", log_append="活动可参与，开始执行参与步骤")
            payload = _participate_dynamic_payload(
                dynamic_id,
                on_step,
                lottery_type_hint=lottery_type,
                client=client,
            )

        mark_enriched_joined(dynamic_id)
        refresh_local_activity_statuses()
        logger.info("参与活动成功 %s", dynamic_id)
        action_log = format_participation_log(payload)
        invalidate_activity_cache()
        return {
            "ok": True,
            "message": str(payload.get("message") or "参与成功"),
            "result": payload,
            "log": action_log,
        }

    if action in {"participate_triple", "participate_sequential"}:
        _raise_if_cancelled(cancel_event)

        account = params.get("_cluster_account")
        if account and get_login_uid() != account:
            raise ValueError("登录账号已变化，轮间顺序参与已停止")
        filters = _list_filter_params(params)
        sequential = action == "participate_sequential"
        targets = (pick_triple_participate_targets(**filters, limit=None) if sequential
                   else pick_triple_participate_targets(**filters))
        round_pause = params.get("_round_pause_event")
        previously_handled = set(params.get("_round_handled", ())) if account else set()
        if previously_handled:
            targets = [target for target in targets if str(target.get("dynamic_id") or "") not in previously_handled]
        label = "自动顺序参与" if sequential else "三连参与"
        from_auto = bool(params.get("from_auto"))
        if not targets:
            return {
                "ok": True,
                "message": "当前没有可参与的未参加活动，已跳过",
                "log": f"无可参与目标，本次{label}已跳过",
                "result": {
                    "skipped": True,
                    "from_auto": from_auto,
                    "joined": 0,
                    "failed": 0,
                    "items": [],
                    "targets": [],
                },
            }

        target_titles = [str(item.get("activity_title") or item.get("dynamic_id") or "") for item in targets]
        total_steps, progress_plan = build_triple_progress_plan(targets)
        if total_steps <= 0:
            return {
                "ok": True,
                "message": "当前没有可参与的未参加活动，已跳过",
                "log": f"进度计划为空，本次{label}已跳过",
                "result": {
                    "skipped": True,
                    "from_auto": from_auto,
                    "joined": 0,
                    "failed": 0,
                    "items": [],
                    "targets": [],
                },
            }

        serial = sequential or bool(params.get("_queued_serial"))
        execution_order = "依次" if serial else "并行"
        logger.info("%s开始：%s", label, ", ".join(target_titles))
        triple_target_items = build_triple_target_preview(targets)["items"]
        progress(
            step=0,
            total=total_steps,
            message=f"准备{execution_order}参与 {len(targets)} 个活动…",
            log_append="目标活动：" + "、".join(target_titles),
            result_patch={"targets": triple_target_items},
        )

        progress_lock = threading.Lock()
        task_states: dict[str, str] = {
            str(item.get("dynamic_id") or ""): "等待开始…" for item in targets
        }
        task_step_progress: dict[str, int] = {
            str(item.get("dynamic_id") or ""): 0 for item in targets
        }
        target_ids = [str(item.get("dynamic_id") or "") for item in targets]

        def _overall_progress_step() -> int:
            total_done = 0
            for dynamic_id in target_ids:
                plan_entry = progress_plan.get(dynamic_id)
                if not plan_entry:
                    continue
                _, budget = plan_entry
                total_done += min(task_step_progress.get(dynamic_id, 0), budget)
            return min(total_done, total_steps)

        def _emit_triple_progress(*, log_append: str | None = None) -> None:
            progress(
                step=_overall_progress_step(),
                total=total_steps,
                message=_progress_snapshot(),
                log_append=log_append,
            )

        def _progress_snapshot() -> str:
            parts: list[str] = []
            for target in targets:
                dynamic_id = str(target.get("dynamic_id") or "")
                title = str(target.get("activity_title") or dynamic_id[-6:])
                parts.append(f"{title}: {task_states.get(dynamic_id, '等待开始…')}")
            return " | ".join(parts)

        def _report_task_progress(dynamic_id: str, step: int, _total: int, message: str) -> None:
            if cancel_event and cancel_event.is_set():
                raise ValueError("任务已取消")
            if dynamic_id not in progress_plan:
                return
            with progress_lock:
                task_states[dynamic_id] = message
                task_step_progress[dynamic_id] = max(task_step_progress.get(dynamic_id, 0), step)
            title = next(
                (
                    str(item.get("activity_title") or dynamic_id)
                    for item in targets
                    if str(item.get("dynamic_id") or "") == dynamic_id
                ),
                dynamic_id,
            )
            _emit_triple_progress(log_append=f"{title}：{message}")

        def _mark_other_tasks_stopped(failed_id: str, *, reason: str) -> None:
            skip_markers = ("失败", "参与成功", "完成", "成功", "已停止", "已取消")
            with progress_lock:
                for other_id in target_ids:
                    if failed_id and other_id == failed_id:
                        continue
                    state = task_states.get(other_id, "")
                    if any(marker in state for marker in skip_markers):
                        continue
                    task_states[other_id] = reason

        def _participate_triple_target(target: dict[str, Any]) -> dict[str, Any]:
            if cancel_event and cancel_event.is_set():
                raise ValueError("任务已取消")
            if account and get_login_uid() != account:
                raise ValueError("登录账号已变化，轮间顺序参与已停止")

            dynamic_id = str(target.get("dynamic_id") or "")
            title = str(target.get("activity_title") or dynamic_id)
            target_lottery_type = str(target.get("lottery_type") or "").strip() or None

            with progress_lock:
                task_states[dynamic_id] = "正在检查活动状态…"
            _emit_triple_progress(log_append=f"{title}：正在检查活动状态…")

            with BilibiliClient() as client:
                ensure_activity_participatable(
                    client,
                    dynamic_id,
                    lottery_type_hint=target_lottery_type,
                )
                if cancel_event and cancel_event.is_set():
                    raise ValueError("任务已取消")
                if account and get_login_uid() != account:
                    raise ValueError("登录账号已变化，轮间顺序参与已停止")

                def on_step(step: int, total: int, message: str, _action_name: str) -> None:
                    _report_task_progress(dynamic_id, step, total, message)

                payload = _participate_dynamic_payload(
                    dynamic_id,
                    on_step,
                    lottery_type_hint=target_lottery_type,
                    client=client,
                )

            mark_enriched_joined(dynamic_id)
            with progress_lock:
                task_states[dynamic_id] = str(payload.get("message") or "参与成功")
            plan_entry = progress_plan.get(dynamic_id)
            if plan_entry:
                _, budget = plan_entry
                task_step_progress[dynamic_id] = budget
            _emit_triple_progress(log_append=f"{title}：参与成功")
            return {
                "dynamic_id": dynamic_id,
                "activity_title": title,
                "lottery_type": str(payload.get("lottery_type") or target.get("lottery_type") or ""),
                "payload": payload,
            }

        results: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        _emit_triple_progress()
        if serial:
            for index, target in enumerate(targets):
                if round_pause is not None and round_pause.is_set():
                    message = "轮间参与已暂停，等待本机发布UP主任务执行"
                    progress(step=_overall_progress_step(), total=total_steps, message=message, log_append=message)
                    return {"ok": True, "message": message,
                            "result": {"round_paused": True, "handled": target_ids[:index],
                                       "joined": len(results), "skipped": len(skipped), "remaining": len(targets) - index}}
                dynamic_id = str(target.get("dynamic_id") or "")
                try:
                    results.append(_participate_triple_target(target))
                except Exception as exc:
                    if sequential and account and isinstance(exc, ActivityPreflightRejected):
                        task_states[dynamic_id] = f"已跳过：{exc}"
                        if entry := progress_plan.get(dynamic_id):
                            task_step_progress[dynamic_id] = entry[1]
                        skipped.append({"dynamic_id": dynamic_id, "status": "skipped", "message": str(exc)})
                        _emit_triple_progress(log_append=f"{dynamic_id}：已跳过，{exc}")
                        continue
                    task_states[dynamic_id] = f"失败：{exc}"
                    _mark_other_tasks_stopped(dynamic_id, reason="已停止（其他活动失败）")
                    _emit_triple_progress(log_append=f"{dynamic_id}：{exc}")
                    if sequential:
                        failed = getattr(exc, "participation_result", None) or {}
                        remaining = len(targets) - index - 1
                        exc.participation_result = {"targets": triple_target_items,
                            "message": f"顺序参与停止：成功 {len(results)}，失败 1，尚余 {remaining} 个活动未执行",
                            "items": [*[item["payload"] for item in results], *skipped,
                                      {**failed, "dynamic_id": dynamic_id, "status": "failed", "message": str(exc)}],
                            "joined": len(results), "failed": 1, "skipped": len(skipped), "remaining": remaining}
                    raise
                progress(
                    step=_overall_progress_step(),
                    total=total_steps,
                    message=_progress_snapshot(),
                    result_patch={"items": [item["payload"] for item in results], "joined": len(results)},
                )
        else:
            with ThreadPoolExecutor(max_workers=PARTICIPATE_TRIPLE_WORKERS) as executor:
                # 每个 future 各自 copy_context：同一 Context 不能被多线程并发 enter
                future_to_id = {
                    executor.submit(
                        contextvars.copy_context().run,
                        _participate_triple_target,
                        target,
                    ): str(target.get("dynamic_id") or "")
                    for target in targets
                }
                try:
                    for future in as_completed(future_to_id):
                        dynamic_id = future_to_id[future]
                        if cancel_event and cancel_event.is_set():
                            raise ValueError("任务已取消")
                        try:
                            results.append(future.result())
                        except Exception as exc:
                            with progress_lock:
                                task_states[dynamic_id] = f"失败：{exc}"
                            _mark_other_tasks_stopped(dynamic_id, reason="已停止（其他活动失败）")
                            _emit_triple_progress(log_append=f"{dynamic_id}：失败")
                            for pending in future_to_id:
                                pending.cancel()
                            raise
                finally:
                    if cancel_event and cancel_event.is_set():
                        _mark_other_tasks_stopped("", reason="已取消")
                        _emit_triple_progress()

        refresh_local_activity_statuses()
        results.sort(key=lambda item: target_ids.index(item["dynamic_id"]))
        log_blocks = [format_participation_log(item["payload"]) for item in results]
        message = (f"{label}完成：成功 {len(results)} 个，跳过 {len(skipped)} 个" if skipped
                   else f"{label}完成：{len(results)} 个活动全部成功")
        progress(step=total_steps, total=total_steps, message=message, log_append=message)
        logger.info("%s成功 count=%s", label, len(results))
        invalidate_activity_cache()
        return {
            "ok": True,
            "message": message,
            "result": {
                "targets": [
                    {
                        "dynamic_id": item["dynamic_id"],
                        "activity_title": item["activity_title"],
                        "lottery_type": item["lottery_type"],
                    }
                    for item in results
                ],
                "items": [*[item["payload"] for item in results], *skipped],
                "joined": len(results),
                "skipped": len(skipped),
            },
            "log": "\n\n".join(log_blocks).strip(),
        }

    raise ValueError(f"未知操作: {action}")
