
"""Persistent Binggo participant and coordinator lifecycle."""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from src.activity_store import activity_file_lock, append_activities, load_activities, update_activity
from src.bilibili_auth import get_login_uid
from src.llm_settings import is_llm_ready
from src.lottery_classifier import PARTICIPATABLE_TYPES
from src.lottery_time import confirmed_lottery_time_unix
from src.pipeline.status_step import apply_initial_status
from src.sources.common import is_valid_dynamic_id, opus_link
from src.watch_users import list_watch_users
from web.account_service import has_login_cookie
from web.activity_service import SOURCE_LABELS, SOURCE_SPACE_URLS, count_pending_participation, invalidate_activity_cache
from web.cluster_protocol import (Activity, Analysis, DetectionPageRequest, Exchange, Feed, HISTORY_PAGE_SIZE, Lease,
                                  LINK_PATTERNS, MAX_LOTTERY_TIME, PAGE_SIZE, PEER_PATH, POLL_SECONDS, Register, Report,
                                  RenameNode, VideoHistory, WATCH_PAGE_SIZE, WatchFindings, WatchList, WatchUser,
                                  ParticipationResult, normalize_address)
from web.cluster_protocol import Change, PublisherFinding, Task
from web import publisher_sources
from web.cluster_store import ClusterError, ClusterStore
from web.cluster_work import RetryCheck, execute

logger = logging.getLogger(__name__)
SYNC_SECONDS = 30


def _tracked_creators(*, include_collections):
    creators = {}
    if include_collections:
        for source, url in SOURCE_SPACE_URLS.items():
            match = re.match(r"https://space\.bilibili\.com/([1-9][0-9]*)/", url)
            if match:
                creators[match[1]] = SOURCE_LABELS[source]
    creators.update((str(user.mid), user.name) for user in list_watch_users(ensure_seeded=False))
    return [WatchUser(mid=mid, name=name[:64]) for mid, name in sorted(creators.items())]


def _export(now, published_unknown=()):
    rows = []
    for item in load_activities():
        key = str(item.get("dynamic_id") or "")
        kind = item.get("lottery_type")
        if (item.get("cluster_id") or kind not in PARTICIPATABLE_TYPES or item.get("skipped")
                or not key.isascii() or not is_valid_dynamic_id(key)):
            continue
        timestamp = confirmed_lottery_time_unix(item)
        if timestamp is not None and not 0 < timestamp <= MAX_LOTTERY_TIME:
            timestamp = None
        closed = item.get("draw_status") == "ended" or (timestamp is not None and timestamp <= now)
        if (closed and key not in published_unknown) or (not closed and item.get("draw_status") != "active"):
            continue
        prizes = item.get("prizes") or []
        prize = "、".join(str(entry.get("description") or "") for entry in prizes if isinstance(entry, dict))
        rows.append(Activity(dynamic_id=key, source_url=opus_link(key), lottery_type=kind,
                             lottery_time=timestamp, prize=prize[:300], closed=closed))
    return rows


def _import(changes, cluster_id):
    if not changes:
        return
    now = int(time.time())
    with activity_file_lock():
        existing = {str(item["dynamic_id"]): item for item in load_activities()}
        new = []
        changed = False
        for change in changes:
            activity = change.activity
            key = activity.dynamic_id
            current = existing.get(key)
            if current and (not current.get("cluster_id") or (
                current["cluster_id"] == cluster_id and current.get("cluster_seq", 0) >= change.seq
            )):
                continue
            closed = activity.closed or activity.expires_at <= now
            if closed and not current:
                continue
            row = dict(current or {})
            lottery_time = activity.lottery_time
            if lottery_time is None and current:
                lottery_time = confirmed_lottery_time_unix(current)
            conditions = dict(row.get("conditions") or {})
            conditions.pop("lottery_time_text", None)
            conditions.pop("lottery_time_inferred", None)
            row.update(dynamic_id=key, source_url=opus_link(key), lottery_type=activity.lottery_type,
                       lottery_time=lottery_time, conditions=conditions, lottery_time_inferred=lottery_time is None,
                       prizes=[{"description": activity.prize}] if activity.prize else [],
                       cluster_id=cluster_id, cluster_seq=change.seq, cluster_deadline=activity.deadline,
                       cluster_closed=closed)
            if current:
                if closed:
                    row.update(draw_status="ended", activity_status="已结束", draw_tag="")
                elif current.get("cluster_closed") or (
                    type(current.get("cluster_deadline")) is int and current["cluster_deadline"] <= now
                ):
                    row.update(draw_status="active", status_classified=False)
                update_activity(key, row)
                changed = True
            else:
                row = apply_initial_status(row, now=now)
                row["status_classified"] = False
                new.append(row)
        if append_activities(new) or changed:
            invalidate_activity_cache()


class ClusterService:
    def __init__(self, directory: Path):
        self.store = ClusterStore(directory)
        self.directory = directory
        self.http = httpx.Client(timeout=8, trust_env=False, follow_redirects=False)
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.status_remote = {}
        self.error = ""
        self.last_sync = 0
        self._thread = None
        self._job = None
        self._task = None
        self._exchange_in_flight = False

    def start(self):
        self._thread = threading.Thread(target=self._run, name="binggo-cluster", daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()

    def drain_for_update(self):
        with self.lock:
            if self._job is not None:
                self.wake.set()
                return False
            self.stop()
        return self._thread is None or not self._thread.is_alive()

    def update_busy(self):
        with self.lock:
            return self._job is not None

    def _remote(self, session, path, token, payload):
        headers = {"Authorization": "Bearer " + token}
        if path == "/join":
            headers["X-Binggo-Video-History"] = "1"
        if path == "/exchange":
            # Old v1 hosts reject extra JSON fields but ignore the capability header.
            payload = dict(payload)
            if payload.pop("video_detection", None):
                headers["X-Binggo-Video-Detection"] = "1"
            if payload.pop("unknown_activity_time", False):
                headers["X-Binggo-Unknown-Activity-Time"] = "1"
        try:
            response = self.http.post(session["address"] + PEER_PATH + path,
                                      headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise ClusterError("无法连接主服务器，请检查地址、网络和 TLS 证书", 502) from exc
        if response.status_code == 401:
            raise ClusterError("集群授权失败，请检查邀请码或重新加入", 401)
        if response.status_code != 200:
            status = response.status_code if response.status_code in (400, 422) else 502
            raise ClusterError(f"主服务器返回 HTTP {response.status_code}，请检查集群入口", status)
        try:
            return response.json()
        except ValueError as exc:
            raise ClusterError("主服务器未返回集群协议数据", 502) from exc

    def _set_session(self, session):
        self.store.write("session", session)
        self.status_remote = {}
        self.error = ""
        self.last_sync = 0
        self.wake.set()

    def host(self, body):
        address = normalize_address(body.address)
        with self.lock:
            self.store.start_host(body.name.strip() or "主节点", address, video_history=True)
            self.status_remote = {}
            self.error = ""
            self.last_sync = 0
            self.wake.set()

    def join(self, body):
        address = normalize_address(body.address)
        with self.lock:
            if self.store.read("session"):
                raise ClusterError("请先退出当前集群")
            registration = Register(name=body.name.strip() or "工作节点", node=uuid.uuid4().hex,
                                    token=secrets.token_hex(32))
            response = self._remote({"address": address}, "/join", body.invite, registration.model_dump())
            cluster_id = response.get("cluster_id") if isinstance(response, dict) else None
            if not isinstance(cluster_id, str) or len(cluster_id) != 32 or response.get("protocol") != 1:
                raise ClusterError("主服务器的集群协议版本不兼容", 502)
            self._set_session({**registration.model_dump(), "role": "member", "id": cluster_id, "address": address})
            if response.get("video_history"):
                self.status_remote = {"video_history": True}

    def leave(self):
        with self.lock:
            session = self.store.read("session") or {}
            self._set_session(None)
        if session.get("role") == "member":
            try:
                self._remote(session, f"/leave/{session['node']}", session["token"], {})
            except ClusterError:
                pass

    def rename(self, name):
        with self.lock:
            session = self.store.read("session")
            if not session:
                raise ClusterError("请先加入工作集群")
            body = RenameNode(node=session["node"], name=name)
            if session["role"] == "host":
                self.store.rename_node(body, session["token"])
            else:
                if not self.status_remote.get("node_rename_supported"):
                    raise ClusterError("等待主节点更新或重新连接后再修改名称")
                result = self._remote(session, "/rename", session["token"], body.model_dump())
                if result.get("ok") is not True:
                    raise ClusterError("主节点未确认名称修改")
                self.store.write("session", {**session, "name": body.name})
        self.wake.set()

    def snapshot(self):
        config = self.store.read("session") or {}
        role = config.get("role")
        connected = bool(self.last_sync and self.last_sync > time.time() - 30)
        status = self.store.status() if role == "host" else self.status_remote if role else {}
        return {"role": role, "address": config.get("address"), "node": config.get("node"), "node_name": config.get("name"),
                "connected": connected if role else False, "error": self.error if role else "",
                "last_sync": self.last_sync if role else 0,
                "local_cursor": self.store.read(self._cursor_key(config), 0) if role else 0,
                "worker_ready": has_login_cookie() and is_llm_ready(),
                "round_participation_enabled": self._participation_uid(config) is not None if role else False,
                "status": status}

    def _participation_uid(self, session):
        uid = get_login_uid()
        return uid if uid and uid == self.store.read(f"round_opt_in:{session['id']}") else None

    def set_round_participation(self, enabled):
        with self.lock:
            session = self.store.read("session")
            uid = get_login_uid() if enabled else None
            if not session or (enabled and not uid):
                raise ClusterError("请先登录 B 站并加入工作集群")
            self.store.write(f"round_opt_in:{session['id']}", uid)
        self.wake.set()

    def _round_participation(self, session, round_id, runner):
        key = f"round_participation:{session['id']}"
        progress = self.store.read(key) or {}
        if progress.get("round", 0) > round_id:
            return "failed", None
        if progress.get("round") == round_id:
            if not progress.get("job"):
                return "failed", None  # An interrupted submission may already have posted actions.
            job = runner.resolve_job_status(progress["job"])
            if job.state == "running":
                return "running", None
            result = job.result or {}
            if result.get("round_paused"):
                from web.task_queue import get_task_queue

                if get_task_queue().publisher_waiting() or runner.is_running():
                    return "running", None
                uid = self._participation_uid(session)
                if not uid or progress.get("uid") != uid:
                    return "disabled", None
                handled = list(dict.fromkeys([*progress.get("handled", []), *result.get("handled", [])]))
                with self.lock:
                    if self.store.read("session") != session:
                        return "disabled", None
                    self.store.write(key, {"round": round_id, "job": None})
                    resumed = runner.try_start("participate_sequential",
                        {"from_auto": True, "_cluster_account": uid, "_round_handled": handled}, source="auto")
                    self.store.write(key, {"round": round_id, "job": resumed, "uid": uid, "handled": handled,
                                           "joined": progress.get("joined", 0) + result.get("joined", 0),
                                           "skipped": progress.get("skipped", 0) + result.get("skipped", 0)}
                                     if resumed is not None else progress)
                return "running", None
            outcome = "done" if job.state == "success" and not result.get("failed") else "failed"
            summary = ParticipationResult(
                joined=progress.get("joined", 0) + result.get("joined", 0), failed=result.get("failed", int(outcome == "failed")),
                skipped=progress.get("skipped", 0) + len([item for item in result.get("items", []) if item.get("status") == "skipped"]),
                remaining=result.get("remaining", 0),
            )
            return outcome, summary
        uid = self._participation_uid(session)
        if not uid:
            return "disabled", None
        granted = (self.status_remote.get("participation_turn") or {}).get("granted") or (
            self.status_remote.get("round_import") or {}).get("participating")
        from web.task_queue import get_task_queue

        if (not granted or self.status_remote.get("paused") or self._job
                or not self.last_sync or time.time() - self.last_sync > 20
                or runner.is_running() or get_task_queue().publisher_waiting()):
            return "ready", None
        if not is_llm_ready():
            return "failed", None
        with self.lock:
            if self.store.read("session") != session or self._participation_uid(session) != uid:
                return "disabled", None
            self.store.write(key, {"round": round_id, "job": None})
            job = runner.try_start("participate_sequential", {"from_auto": True, "_cluster_account": uid},
                                   source="auto")
            self.store.write(key, {"round": round_id, "job": job, "uid": uid} if job is not None else progress)
        return ("running" if job is not None else "ready"), None

    def _supports_unknown_time(self, session):
        return session["role"] == "host" or bool(self.status_remote.get("unknown_activity_time"))

    def _cursor_key(self, session):
        prefix = "full_cursor" if self._supports_unknown_time(session) else "cursor"
        return f"{prefix}:{session['node']}"

    def invite(self):
        with self.lock:
            if (self.store.read("session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以查看邀请码")
            return {"invite": self.store.read("invite")}

    def detected_activities(self, page):
        session = self.store.read("session") or {}
        if session.get("role") == "host":
            return self.store.detected_activities(page)
        if session.get("role") != "member":
            raise ClusterError("请先加入工作集群")
        if "detected_open" not in self.status_remote:
            raise ClusterError("主节点尚未提供检出活动列表")
        request = DetectionPageRequest(node=session["node"], page=page)
        return self._remote(session, "/detected-activities", session["token"], request.model_dump())

    def tracked_users(self, page):
        session = self.store.read("session") or {}
        if session.get("role") == "host":
            return self.store.tracked_users(page)
        if session.get("role") != "member" or not self.status_remote.get("watch_tracking"):
            raise ClusterError("主节点尚未提供集群追踪列表")
        request = DetectionPageRequest(node=session["node"], page=page)
        return self._remote(session, "/tracked-users", session["token"], request.model_dump())

    def _analysis(self, session, task, key, fields=None):
        payload = {"node": session["node"], "task_id": task.task_id, "lease": task.lease, "key": key, "fields": fields}
        if session["role"] == "host":
            response = self.store.analysis(Analysis.model_validate(payload), session["token"])
        else:
            response = self._remote(session, "/analysis", session["token"], payload)
        if response.get("busy"):
            raise RetryCheck("相同内容正在由其他节点分析", reason="analysis_busy")
        return response.get("fields")

    def _publish_watch(self, session, task, **findings):
        body = WatchFindings(node=session["node"], task_id=task.task_id, lease=task.lease, **findings)
        try:
            if session["role"] == "host":
                self.store.publish_watch_findings(body, session["token"])
            else:
                self._remote(session, "/watch-findings", session["token"], body.model_dump(exclude_unset=True))
        except ClusterError as exc:
            raise RetryCheck("追踪结果尚未同步", reason="network") from exc

    def _execute(self, session, task, *, two_stage, watch_batch=False, collection_mode=None, watch_coverage=None):
        def publish(activities):
            self._publish_watch(session, task, activities=activities)

        def publish_references(references):
            self._publish_watch(session, task, references=references)

        return execute(task, self.directory / "worker-cache.json",
                       lambda key: self._analysis(session, task, key),
                       lambda key, result: self._analysis(session, task, key, result),
                       two_stage=two_stage,
                       publish=publish if task.kind == "publisher" or (watch_batch and task.kind == "watch") else None,
                       collection_mode=collection_mode, publish_references=publish_references, watch_coverage=watch_coverage,
                       record_publishers=self.store.record_publishers)

    def publisher_listing(self, page):
        session = self.store.read("session") or {}
        if session.get("role") == "member" and self.status_remote.get("publisher_sources_supported"):
            return self._remote(session, "/publisher-sources", session["token"], {"page": page, "node": session["node"]})
        return self.store.publisher_listing(page)

    def publisher_work_busy(self):
        return self._exchange_in_flight or self._job is not None

    def refresh_publishers(self, progress, cancel_event):
        from web.actions import _raise_if_cancelled

        with self.store.tx(write=False) as db:
            sources = {row["mid"]: row for row in publisher_sources.active(db)}
        page = 1
        while True:
            listing = self.publisher_listing(page)
            for row in listing["items"]:
                sources.setdefault(row["mid"], row)
            if page * listing["page_size"] >= listing["total"]:
                break
            page += 1
        progress(step=0, total=max(1, len(sources)), message="已更新活动发布UP主名单", log_append=f"本次检查 {len(sources)} 位")
        hits = set()
        failures = 0
        for index, source in enumerate(sources.values(), 1):
            _raise_if_cancelled(cancel_event)
            mid = source["mid"]

            def publish(activities):
                hits.update(item.dynamic_id for item in activities if not item.closed)
                _import([Change(seq=0, activity=item) for item in activities], "")

            result = execute(Task(task_id=1, lease=uuid.uuid4().hex, kind="publisher", key=mid),
                             self.directory / "publisher-cache.json", lambda key: None, lambda key, fields: fields,
                             publish=publish, watch_coverage={"dynamics": source["checked"]},
                             record_publishers=self.store.record_publishers)
            message = "检查未完成，保留名单"
            if result.outcome == "done" and result.publisher_coverage:
                with self.store.tx() as db:
                    removed = self.store.complete_publisher(db, mid, result.publisher_coverage, int(time.time()))
                message = "已检查" if not removed else "完整检查覆盖 60 天未发布抽奖活动，已移出名单"
            else:
                failures += 1
            progress(step=index, total=len(sources), message=f"{source['name']}（{mid}）：{message}",
                     log_append=f"{source['name']}（{mid}）：{message}")
            if result.outcome == "cooldown":
                failures += len(sources) - index
                break
        if not sources:
            progress(step=1, total=1, message="暂无达到入选条件的发布者")
        return {"message": f"确认 {len(hits)} 项开放活动，{failures} 位检查未完成",
                "source_errors": failures, "activities": len(hits)}

    def _history(self):
        from web.control_service import get_control_service

        ids = get_control_service().store.discovery_video_ids()
        with self.store.tx(write=False) as db:
            ids.update(row[0] for row in db.execute("SELECT id FROM local_checked_videos"))
        return {key for key in ids if re.fullmatch(LINK_PATTERNS["video"], key)}

    def _import_turn(self, node):
        if turn := self.status_remote.get("participation_turn"):
            return turn["epoch"]
        state = self.status_remote.get("round_import") or {}
        return state.get("round") if state.get("node") == node else None

    def _run(self):
        session_node = ""
        export_queue = []
        exported = set()
        reload_at = 0
        history_queue, shared_history = [], set()
        history_sent, history_reload_at = False, 0
        import_turn = None
        watch_queue, watch_uploaded, watch_reload_at = [], None, 0
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cluster-worker") as pool:
                while not self.stop_event.is_set():
                    self.wake.clear()
                    session = self.store.read("session") or {}
                    if session.get("node") != session_node:
                        session_node = session.get("node", "")
                        export_queue, exported, reload_at = [], set(), 0
                        history_queue, shared_history = [], set()
                        history_sent, history_reload_at, import_turn = False, 0, None
                        watch_queue, watch_uploaded, watch_reload_at = [], None, 0
                    if self._job and self._job.done():
                        try:
                            report = self._job.result()
                        except Exception:
                            logger.warning("集群任务未完成", exc_info=True)
                            report = Report(task_id=self._task.task_id, lease=self._task.lease, outcome="retry", reason="worker")
                        report_node = self._task_node
                        task = self._task
                        self._job, self._task = None, None
                        with self.store.tx() as db:
                            if task.kind == "video" and report.outcome == "done":
                                db.execute("INSERT OR IGNORE INTO local_checked_videos VALUES(?)", (task.key,))
                            if report_node == session_node:
                                self.store.put(db, f"report:{session_node}", report.model_dump())
                    if session_node and has_login_cookie():
                        try:
                            now = int(time.time())
                            unknown_time = self._supports_unknown_time(session)
                            turn = self._import_turn(session_node)
                            independent = self.status_remote.get("independent_participation")
                            if not independent and turn and turn != import_turn:
                                export_queue, exported, reload_at = [], set(), 0
                            import_turn = turn
                            waiting_turn = bool(self.status_remote.get("round_import")) and not turn
                            if not waiting_turn and not export_queue and now >= reload_at:
                                published = set(self.store.read(f"published_unknown:{session_node}", []))
                                offered = [(row.model_dump_json(), row) for row in _export(now, published)]
                                exported.intersection_update(mark for mark, _ in offered)
                                export_queue = [(mark, row) for mark, row in offered if mark not in exported]
                                reload_at = now + SYNC_SECONDS
                            batch = export_queue[:PAGE_SIZE] if not waiting_turn else []
                            history = None
                            if session["role"] == "host" or self.status_remote.get("video_history"):
                                if not history_queue and now >= history_reload_at:
                                    history_queue = sorted(self._history() - shared_history)
                                    history_reload_at = now + SYNC_SECONDS
                                if history_queue or not history_sent:
                                    history = VideoHistory(ids=history_queue[:HISTORY_PAGE_SIZE],
                                                           complete=len(history_queue) <= HISTORY_PAGE_SIZE)
                            watch = None
                            if session["role"] == "host" or self.status_remote.get("watch_tracking"):
                                if not watch_queue and now >= watch_reload_at:
                                    users = _tracked_creators(include_collections=session["role"] == "host"
                                                              or self.status_remote.get("watch_batch_supported"))
                                    watch_reload_at = now + SYNC_SECONDS
                                    if users != watch_uploaded:
                                        revision = uuid.uuid4().hex
                                        watch_queue = [WatchList(revision=revision, users=users[start:start + WATCH_PAGE_SIZE],
                                                                complete=start + WATCH_PAGE_SIZE >= len(users))
                                                       for start in range(0, max(1, len(users)), WATCH_PAGE_SIZE)]
                                        watch_uploaded = users
                                if watch_queue:
                                    watch = watch_queue[0]
                            self._exchange_in_flight = True
                            try:
                                feed = self._sync(session, [row for _, row in batch], pool, history=history, watch=watch,
                                                  import_round=turn if independent
                                                  or len(export_queue) <= len(batch) else None)
                            finally:
                                self._exchange_in_flight = False
                            if feed:
                                if watch is not None:
                                    watch_queue.pop(0)
                                    if watch_queue:
                                        self.wake.set()
                                if history is not None:
                                    shared_history.update(history.ids)
                                    history_queue = history_queue[len(history.ids):]
                                    history_sent = True
                                if unknown_time != self._supports_unknown_time(session):
                                    export_queue, exported, reload_at = [], set(), 0
                                else:
                                    exported.update(mark for mark, _ in batch)
                                    export_queue = export_queue[len(batch):]
                                next_turn = self._import_turn(session_node)
                                if (history_queue and feed.status.get("video_history")) or (
                                    next_turn and (next_turn != turn or batch or feed.changes)
                                ):
                                    self.wake.set()
                        except ClusterError as exc:
                            self.error = str(exc)
                        except Exception:
                            logger.warning("集群同步失败", exc_info=True)
                            self.error = "同步失败，将自动重试"
                    elif session_node:
                        self.error = "请重新登录 B 站以恢复集群同步"
                    self.wake.wait(POLL_SECONDS)
        finally:
            self.http.close()

    def _sync(self, session, batch, pool, *, history=None, import_round=None, watch=None):
        from web.job_runner import runner
        from web.task_queue import get_task_queue
        from src.update_state import maintenance

        node = session["node"]
        pending = self.store.read(f"report:{node}")
        active = self._task if self._task_node_if_working() == node else None
        two_stage = session["role"] == "host" or bool(self.status_remote.get("two_stage"))
        unknown_time = self._supports_unknown_time(session)
        cursor_key = self._cursor_key(session)
        batch = batch if unknown_time else [row for row in batch if row.lottery_time is not None and not row.closed]
        video_detection = is_llm_ready()
        watch_batch = session["role"] == "host" or bool(self.status_remote.get("watch_batch_supported"))
        watch_link_workers = session["role"] == "host" or bool(self.status_remote.get("watch_link_workers"))
        participation_support = session["role"] == "host" or self.status_remote.get("round_participation_supported")
        publisher_waiting = get_task_queue().publisher_waiting()
        if publisher_waiting:
            runner.pause_round_participation()
        round_participation, participation_result = self._round_participation(session, import_round, runner) if (
            participation_support and import_round
        ) else (None, None)
        independent = session["role"] == "host" or self.status_remote.get("independent_participation")
        local = runner.get_status()
        local_work = "busy" if maintenance.is_set() or publisher_waiting else "idle"
        if local.state == "running":
            local_work = ("participating" if local.action.startswith("participate")
                          else "watch" if local.action == "refresh_watch" else "busy")
        tracking = session["role"] == "host" or self.status_remote.get("watch_tracking")
        supports_publishers = session["role"] == "host" or self.status_remote.get("publisher_sources_supported")
        publisher_batch = []
        if supports_publishers:
            with self.store.tx(write=False) as db:
                publisher_batch = publisher_sources.pending(db)
        message = Exchange(node=node, after=self.store.read(cursor_key, 0), activities=batch,
                           work=not maintenance.is_set() and not self._job and local_work == "idle" and (two_stage or video_detection),
                           video_detection=video_detection if two_stage else None,
                           unknown_activity_time=True,
                           working=Lease(task_id=active.task_id, lease=active.lease) if active else None,
                           report=Report.model_validate(pending) if pending else None,
                           video_history=history, import_round=import_round,
                           watch_list=watch, local_work=local_work if tracking else None,
                           watch_batch_capable=True if watch_batch else None,
                           watch_link_workers=True if watch_link_workers else None,
                           round_participation=round_participation,
                           participation_pending=count_pending_participation() if independent else None,
                           participation_enabled=self._participation_uid(session) is not None if independent else None,
                           participation_result=participation_result if independent else None,
                           publisher_findings=[PublisherFinding(**{k: row[k] for k in ("activity", "mid", "name", "published")})
                                               for row in publisher_batch] if supports_publishers else None)
        if session["role"] == "host":
            feed = self.store.exchange(message, session["token"])
        else:
            payload = message.model_dump()
            for field in ("video_history", "import_round", "watch_list", "local_work",
                          "watch_batch_capable", "watch_link_workers", "round_participation",
                          "participation_pending", "participation_enabled", "participation_result", "publisher_findings"):
                if payload[field] is None:
                    del payload[field]
            if pending_report := payload["report"]:
                if not supports_publishers:
                    pending_report.pop("publisher_coverage", None)
                    pending_report.pop("publisher_latest", None)
                if not self.status_remote.get("discovery_coverage"):
                    for field in ("search_dates", "search_exhausted", "watch_through", "watch_collection"):
                        pending_report.pop(field, None)
                if "events" not in self.status_remote:
                    pending_report.pop("reason", None)
                elif (not self.status_remote.get("retry_detail") and
                      pending_report["reason"] in {"source_response", "dynamic_detail", "dynamic_body"}):
                    pending_report["reason"] = "platform"
            try:
                feed = Feed.model_validate(self._remote(session, "/exchange", session["token"], payload))
            except ClusterError as exc:
                if unknown_time and exc.status in (400, 422):
                    self.status_remote = {}
                raise
        with self.lock:
            if self.store.read("session") != session:
                return None
            if pending:
                self.store.write(f"report:{node}", None)
            if publisher_batch:
                with self.store.tx() as db:
                    db.executemany("UPDATE publisher_evidence SET forwarded=1 WHERE rowid=?",
                                   ((row["rowid"],) for row in publisher_batch))
            _import(feed.changes, session["id"])
            # After upgrading, replay the full stream once. The original v1 cursor
            # may have advanced past records it could not represent.
            if feed.changes and unknown_time == bool(feed.status.get("unknown_activity_time")):
                self.store.write(cursor_key, feed.changes[-1].seq)
            if unknown_time and batch and feed.status.get("unknown_activity_time"):
                key = f"published_unknown:{node}"
                previous = set(self.store.read(key, []))
                published = previous.copy()
                for activity in batch:
                    if activity.lottery_time is None and not activity.closed:
                        published.add(activity.dynamic_id)
                    else:
                        published.discard(activity.dynamic_id)
                if published != previous:
                    self.store.write(key, sorted(published))
            self.status_remote, self.last_sync, self.error = feed.status, int(time.time()), ""
            if feed.task and not self.stop_event.is_set():
                self._task, self._task_node = feed.task, node
                options = {"collection_mode": feed.status["watch_collection_mode"]} if feed.status.get("watch_collection_mode") else {}
                if feed.task.kind in {"watch", "publisher"} and feed.status.get("watch_coverage"):
                    options["watch_coverage"] = feed.status["watch_coverage"]
                self._job = pool.submit(self._execute, session, feed.task, two_stage=bool(feed.status.get("two_stage")),
                                        watch_batch=watch_batch, **options)
            return feed

    def _task_node_if_working(self):
        return self._task_node if self._job else None
