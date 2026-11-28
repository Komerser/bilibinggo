
"""Single-writer cluster coordinator. Workers interact through the peer API."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.discovery_coverage import ordered_page_times
from web.activity_service import collection_source_for_mid
from web import publisher_sources
from web.cluster_protocol import (Activity, Analysis, Change, DispatchSettings, Exchange, Feed, KEYWORDS, Lease,
                                  PAGE_SIZE, Reference, Register, RenameNode, Report, TARGET, Task, WATCH_LINK_DEPTH, WatchAssignment,
                                  WatchBatch, WatchFindings, WorkAssignments)

LEASE_SECONDS = 120
MAX_ATTEMPTS = 4
MAX_NODES = 32
MAX_CONCURRENT = 4
ONLINE_SECONDS = 30
PARTICIPATION_TIMEOUT = 120
WATCH_ROUND_INTERVAL = 60
SEARCH_PAGE_LIMIT = 60
EVENT_RETENTION = 2000
RECENT_EVENTS = 80
DETECTIONS_PER_PAGE = 10
TRACKED_USERS_PER_PAGE = 12
WATCH_VERIFIED_TTL = 7 * 86400
WATCH_FAILED_TTL = 6 * 3600
JOB_LABEL = {"search": "检索", "video": "视频检查", "watch_ref": "合集线索核验", "dynamic": "动态核验",
             "article": "专栏检查", "short": "短链展开", "author": "发布者检查", "watch": "追踪 UP 主",
             "publisher": "活动发布UP主检查"}
RETRY_LABEL = {"platform": "平台内容暂不可读", "model": "模型未返回有效结果",
               "network": "网络请求失败", "analysis_busy": "其它节点正在分析同一内容",
               "worker": "工作节点处理失败", "source_response": "B 站内容接口未返回可用数据",
               "dynamic_detail": "关联动态详情不可读取", "dynamic_body": "关联动态缺少可核验正文"}
DETECTION_EVENT = re.compile(
    r"视频检查 (BV[0-9A-Za-z]{10})完成：(核验 ([0-9]+) 项开放活动|未确认开放活动)(?:，调用模型 [0-9]+ 次)?"
)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ClusterError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ClusterStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "cluster.db"
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cluster_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, level TEXT NOT NULL,
                    node TEXT, node_name TEXT, message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cluster_events_node ON cluster_events(node,id);
                CREATE TABLE IF NOT EXISTS cluster_node_round(
                    round INTEGER NOT NULL, node TEXT NOT NULL,
                    pages INTEGER NOT NULL, found INTEGER NOT NULL,
                    videos INTEGER NOT NULL, activities INTEGER NOT NULL,
                    PRIMARY KEY(round,node)
                );
                CREATE TABLE IF NOT EXISTS cluster_video_results(
                    job INTEGER PRIMARY KEY, round INTEGER NOT NULL, node TEXT NOT NULL,
                    matched INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cluster_video_results_round ON cluster_video_results(round,node);
                CREATE TABLE IF NOT EXISTS node_detection_totals(
                    node TEXT PRIMARY KEY, videos INTEGER NOT NULL, counted INTEGER NOT NULL, matched INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL,
                    seen INTEGER NOT NULL, work INTEGER NOT NULL DEFAULT 0,
                    search_ready INTEGER NOT NULL DEFAULT 0,
                    watch_batch INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0, model_calls INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS catalog(
                    id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, payload TEXT NOT NULL,
                    rank INTEGER NOT NULL, expires INTEGER NOT NULL, origin TEXT NOT NULL, checked INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, round INTEGER NOT NULL, kind TEXT NOT NULL,
                    key TEXT NOT NULL, params TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    node TEXT, lease TEXT, until INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(round,kind,key)
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(round,state,due,id);
                CREATE INDEX IF NOT EXISTS watch_ref_identity ON jobs(
                    json_extract(params,'$.reference_kind'),json_extract(params,'$.target'),state
                ) WHERE round=0 AND kind='watch_ref';
                CREATE TABLE IF NOT EXISTS analyses(
                    key TEXT PRIMARY KEY, lease TEXT NOT NULL, until INTEGER NOT NULL, fields TEXT
                );
                CREATE TABLE IF NOT EXISTS checked_videos(id TEXT PRIMARY KEY, checked INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS local_checked_videos(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS node_history(node TEXT PRIMARY KEY, complete INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS node_video_history(node TEXT, bvid TEXT, PRIMARY KEY(node,bvid));
                CREATE TABLE IF NOT EXISTS node_local_work(node TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS node_participation(
                    node TEXT PRIMARY KEY, handled INTEGER NOT NULL DEFAULT 0,
                    offer INTEGER NOT NULL DEFAULT 0, deadline INTEGER NOT NULL DEFAULT 0,
                    pending INTEGER, enabled INTEGER
                );
                CREATE TABLE IF NOT EXISTS watch_sources(
                    node TEXT NOT NULL, mid TEXT NOT NULL, name TEXT NOT NULL, revision TEXT NOT NULL,
                    PRIMARY KEY(node,mid)
                );
                CREATE INDEX IF NOT EXISTS watch_sources_mid ON watch_sources(mid);
                CREATE TABLE IF NOT EXISTS watch_checks(mid TEXT PRIMARY KEY, checked INTEGER, hits INTEGER);
                CREATE TABLE IF NOT EXISTS watch_ref_results(
                    kind TEXT NOT NULL, target TEXT NOT NULL, expires INTEGER NOT NULL, payload TEXT,
                    PRIMARY KEY(kind,target)
                );
                CREATE TABLE IF NOT EXISTS sightings(
                    round INTEGER, job INTEGER, kind TEXT, key TEXT,
                    PRIMARY KEY(round,job,kind,key)
                );
            """)
            publisher_sources.install(db)
            db.execute("BEGIN IMMEDIATE")
            node_columns = {row["name"] for row in db.execute("PRAGMA table_info(nodes)")}
            if "search_ready" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN search_ready INTEGER NOT NULL DEFAULT 0")
            if "watch_batch" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN watch_batch INTEGER NOT NULL DEFAULT 0")
            if "publisher_capable" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN publisher_capable INTEGER NOT NULL DEFAULT 0")
            self._migrate_round(db)
            if (self.get(db, "session") or {}).get("role") == "host" and self.get(db, "collection_paused") is None:
                self.put(db, "collection_paused", self.get(db, "paused", False))
                self.put(db, "paused", False)
            self._restore_video_results(db)
            if not self.get(db, "detection_totals_initialized"):
                db.execute("""INSERT INTO node_detection_totals
                    SELECT node,max(videos,counted),counted,matched FROM (
                        SELECT node,sum(videos) AS videos,sum(counted) AS counted,sum(matched) AS matched FROM (
                            SELECT node,videos,0 AS counted,0 AS matched FROM cluster_node_round
                            UNION ALL SELECT node,0,1,matched FROM cluster_video_results
                        ) GROUP BY node
                    )""")
                self.put(db, "detection_totals_initialized", True)

    @contextmanager
    def tx(self, *, write=True):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def get(db, key, default=None):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, _dump(value)))

    def read(self, key, default=None):
        with self.tx(write=False) as db:
            return self.get(db, key, default)

    def write(self, key, value):
        with self.tx() as db:
            self.put(db, key, value)

    def record_publishers(self, findings):
        with self.tx() as db:
            publisher_sources.record(db, findings, int(time.time()))

    def publisher_listing(self, page, *, node=None, token=""):
        with self.tx(write=False) as db:
            db.execute("BEGIN")
            if node is not None:
                self._authenticate(db, node, token)
            count = db.execute("SELECT count(*) FROM publisher_sources WHERE removed=0").fetchone()[0]
            rows = db.execute("""SELECT mid,name,last_activity,checked FROM publisher_sources WHERE removed=0
                ORDER BY last_activity DESC,mid LIMIT ? OFFSET ?""", (PAGE_SIZE, (page - 1) * PAGE_SIZE))
            return {"items": [dict(row) for row in rows], "total": count, "page": page, "page_size": PAGE_SIZE}

    def complete_publisher(self, db, mid, coverage, now, node=None):
        removed = publisher_sources.complete(db, mid, coverage.lower, coverage.upper, now)
        if removed:
            self._event(db, now, f"活动发布UP主 {mid} 已从名单移除：完整检查覆盖 60 天未发布抽奖活动", node=node)
        return removed

    def _migrate_round(self, db):
        session = self.get(db, "session") or {}
        if session.get("role") != "host" or self.get(db, "two_stage"):
            return
        round_id = self.get(db, "round", 0)
        # Old video jobs only produced references. Reopen parents of unfinished
        # checks so the new video worker can complete that work within its lease.
        db.execute("""
            WITH RECURSIVE affected(kind,key) AS (
                SELECT kind,key FROM jobs WHERE round=? AND
                    kind NOT IN ('search','video') AND state IN ('pending','leased')
                UNION
                SELECT parent.kind,parent.key FROM affected a
                JOIN sightings s ON s.round=? AND s.kind=a.kind AND s.key=a.key
                JOIN jobs parent ON parent.id=s.job
            )
            UPDATE jobs SET state='pending',due=0,attempts=0
            WHERE round=? AND kind='video' AND state='done'
              AND key IN (SELECT key FROM affected WHERE kind='video')
        """, (round_id, round_id, round_id))
        # A worker that already held a video lease may still be running the old checker.
        db.execute("""UPDATE jobs SET state='pending',node=NULL,lease=NULL,until=0,due=0,
                       attempts=max(0,attempts-1)
                      WHERE round=? AND kind='video' AND state='leased'""", (round_id,))
        db.execute("""UPDATE jobs SET state='skipped',node=NULL,lease=NULL,until=0
                      WHERE round=? AND kind NOT IN ('search','video') AND state IN ('pending','leased')""",
                   (round_id,))
        self.put(db, "two_stage", True)
        if not self.get(db, "next_round", 0):
            self.put(db, "phase", "collecting")

    def _restore_video_results(self, db):
        session = self.get(db, "session") or {}
        if session.get("role") != "host" or self.get(db, "video_results_backfilled"):
            return
        round_id = self.get(db, "round", 0)
        start = db.execute("SELECT max(id) FROM cluster_events WHERE message=?",
                           (f"第 {round_id} 轮开始，目标 {TARGET} 个不同视频",)).fetchone()[0]
        # Some hosts enabled event logging after round one had already started.
        # Since a new cluster clears its event table, all its events belong to round one.
        if start or round_id == 1:
            for event in db.execute("""SELECT node,message FROM cluster_events
                                       WHERE id>? AND node IS NOT NULL AND level='info' ORDER BY id DESC""",
                                    (start or 0,)):
                match = DETECTION_EVENT.fullmatch(event["message"])
                if match:
                    db.execute("""INSERT OR IGNORE INTO cluster_video_results
                                  SELECT id,round,?,? FROM jobs
                                  WHERE round=? AND kind='video' AND key=? AND state='done'""",
                               (event["node"], int(int(match[3] or 0) > 0), round_id, match[1]))
        self.put(db, "video_results_backfilled", True)

    @staticmethod
    def _event(db, at, message, *, node=None, name=None, level="info"):
        if node and name is None:
            row = db.execute("SELECT name FROM nodes WHERE id=?", (node,)).fetchone()
            name = row["name"] if row else None
        insert = db.execute("INSERT INTO cluster_events(at,level,node,node_name,message) VALUES(?,?,?,?,?)",
                            (at, level, node, name, message))
        db.execute("DELETE FROM cluster_events WHERE id<=?", (insert.lastrowid - EVENT_RETENTION,))

    @staticmethod
    def _job_label(job):
        if job["kind"] == "search":
            params = json.loads(job["params"])
            order = "发布时间" if params["order"] == "pubdate" else "综合排序"
            return f"检索「{params['keyword']}」第 {params['page']} 页（{order}）"
        if job["kind"] == "watch_ref":
            return f"合集线索核验 {json.loads(job['params'])['target']}"
        return f"{JOB_LABEL[job['kind']]} {job['key']}"

    def start_host(self, name, address, *, video_history=False):
        with self.tx() as db:
            if self.get(db, "session"):
                raise ClusterError("请先退出当前集群")
            for table in ("nodes", "catalog", "jobs", "checked_videos", "sightings", "analyses",
                          "cluster_events", "cluster_node_round", "cluster_video_results", "node_history",
                          "node_video_history", "node_detection_totals", "node_local_work", "watch_sources", "watch_checks",
                          "watch_ref_results", "node_participation"):
                db.execute(f"DELETE FROM {table}")
            session = {"role": "host", "name": name, "address": address, "id": uuid.uuid4().hex,
                       "node": uuid.uuid4().hex, "token": secrets.token_hex(32)}
            db.execute("DELETE FROM settings")
            self.put(db, "session", session)
            self.put(db, "invite", secrets.token_hex(32))
            self._register(db, Register(name=name, node=session["node"], token=session["token"]), int(time.time()))
            if video_history:
                db.execute("INSERT INTO node_history VALUES(?,0)", (session["node"],))
            self.put(db, "round", 0)
            self.put(db, "collection_paused", False)
            self.put(db, "phase", "collecting")
            self.put(db, "two_stage", True)
            self.put(db, "video_results_backfilled", True)
            self.put(db, "detection_totals_initialized", True)
            self._event(db, int(time.time()), "主节点已发起工作集群", name=name)
            return session

    def register(self, body: Register, invite: str, *, video_history=False):
        now = int(time.time())
        with self.tx() as db:
            session = self.get(db, "session", {})
            if (session.get("role") != "host" or not secrets.compare_digest(invite, self.get(db, "invite", ""))):
                raise ClusterError("集群未开启或邀请码无效", 401)
            if body.node == session["node"]:
                raise ClusterError("不能加入本机发起的集群")
            if self._register(db, body, now):
                self._event(db, now, "节点已加入集群", node=body.node)
            if video_history:
                db.execute("INSERT OR IGNORE INTO node_history VALUES(?,0)", (body.node,))
            return {"cluster_id": session["id"], "protocol": 1, "video_history": True}

    @staticmethod
    def _register(db, body, now):
        existing = db.execute("SELECT token FROM nodes WHERE id=?", (body.node,)).fetchone()
        hashed = token_hash(body.token)
        if existing and not secrets.compare_digest(existing["token"], hashed):
            raise ClusterError("节点标识已被占用")
        if not existing and db.execute("SELECT count(*) FROM nodes").fetchone()[0] >= MAX_NODES:
            raise ClusterError("集群节点已满")
        db.execute("""INSERT INTO nodes(id,name,token,seen) VALUES(?,?,?,?)
                      ON CONFLICT(id) DO UPDATE SET name=excluded.name,seen=excluded.seen""",
                   (body.node, body.name, hashed, now))
        return existing is None

    def _authenticate(self, db, node, token):
        session = self.get(db, "session", {})
        row = db.execute("SELECT token FROM nodes WHERE id=?", (node,)).fetchone()
        if (session.get("role") != "host" or not row
                or not secrets.compare_digest(row["token"], token_hash(token))):
            raise ClusterError("节点授权失效，请重新加入集群", 401)
        return session

    def rename_node(self, body: RenameNode, token: str):
        with self.tx() as db:
            session = self._authenticate(db, body.node, token)
            old = db.execute("SELECT name FROM nodes WHERE id=?", (body.node,)).fetchone()[0]
            if old != body.name:
                db.execute("UPDATE nodes SET name=? WHERE id=?", (body.name, body.node))
                if body.node == session["node"]:
                    self.put(db, "session", {**session, "name": body.name})
                self._event(db, int(time.time()), f"节点名称从「{old}」改为「{body.name}」", node=body.node)
        return {"ok": True}

    def leave(self, node, token):
        with self.tx() as db:
            self._authenticate(db, node, token)
            self._event(db, int(time.time()), "节点已退出集群", node=node)
            db.execute("DELETE FROM nodes WHERE id=?", (node,))
            db.execute("DELETE FROM node_history WHERE node=?", (node,))
            db.execute("DELETE FROM node_video_history WHERE node=?", (node,))
            db.execute("DELETE FROM node_local_work WHERE node=?", (node,))
            db.execute("DELETE FROM node_participation WHERE node=?", (node,))
            db.execute("DELETE FROM watch_sources WHERE node=?", (node,))
            db.execute("UPDATE jobs SET until=0 WHERE node=? AND state='leased'", (node,))
            for setting, fields in (("dispatch", ("search_nodes", "video_nodes")), ("watch_batch", ("nodes",)),
                                    ("work_assignments", ("collection_nodes", "watch_nodes"))):
                current = self.get(db, setting)
                if current:
                    for field in fields:
                        if current.get(field) and node in current[field]:
                            remaining = [selected for selected in current[field] if selected != node]
                            current[field] = remaining if setting == "work_assignments" or current.get("strict") else remaining or None
                    self.put(db, setting, current)
            db.execute("""UPDATE jobs SET state='skipped' WHERE kind='watch' AND state='pending' AND
                          json_extract(params,'$.batch') IS NULL AND
                          (json_extract(params,'$.assigned')=? OR key NOT IN (SELECT mid FROM watch_sources))""", (node,))

    def analysis(self, body: Analysis, token: str):
        now = int(time.time())
        with self.tx() as db:
            self._authenticate(db, body.node, token)
            job = db.execute(
                "SELECT 1 FROM jobs WHERE id=? AND node=? AND lease=? AND state='leased' AND until>?",
                (body.task_id, body.node, body.lease, now),
            ).fetchone()
            if not job:
                raise ClusterError("任务已交回，请等待重新分配")
            row = db.execute("SELECT * FROM analyses WHERE key=?", (body.key,)).fetchone()
            if row and row["until"] > now and row["fields"]:
                return {"fields": json.loads(row["fields"])}
            if row and row["until"] > now and row["lease"] != body.lease:
                return {"busy": True}
            db.execute("""INSERT INTO analyses VALUES(?,?,?,?)
                          ON CONFLICT(key) DO UPDATE SET lease=excluded.lease,until=excluded.until,fields=excluded.fields""",
                       (body.key, body.lease, now + (30 * 86400 if body.fields else 330),
                        _dump(body.fields) if body.fields else None))
            return {"fields": body.fields}

    def set_paused(self, paused):
        with self.tx() as db:
            if self.get(db, "session", {}).get("role") != "host":
                raise ClusterError("只有发起人可以控制检索")
            if paused != self.get(db, "paused", False):
                self.put(db, "paused", paused)
                self._event(db, int(time.time()), "暂停领取新任务" if paused else "恢复领取任务")

    def set_collection_paused(self, paused):
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以控制主动搜集")
            if paused != self.get(db, "collection_paused", False):
                self.put(db, "collection_paused", paused)
                self._event(db, int(time.time()), "主动搜集已暂停" if paused else "主动搜集已恢复")

    def _queue(self, db, round_id, kind, key, **params):
        db.execute("INSERT OR IGNORE INTO jobs(round,kind,key,params) VALUES(?,?,?,?)",
                   (round_id, kind, key, _dump(params)))

    @staticmethod
    def _upcoming_keywords(current):
        if current not in KEYWORDS:
            return KEYWORDS
        index = KEYWORDS.index(current)
        return KEYWORDS[index + 1:] + KEYWORDS[:index]

    def _next_round(self, db, now):
        round_id = self.get(db, "round", 0) + 1
        keyword = self._upcoming_keywords(self.get(db, "round_keyword"))[0]
        self.put(db, "round", round_id)
        self.put(db, "round_keyword", keyword)
        self.put(db, "round_search", {"round": round_id, "node": None})
        self.put(db, "phase", "collecting")
        self.put(db, "next_round", 0)
        self._event(db, now, f"第 {round_id} 轮开始，目标 {TARGET} 个不同视频")
        self._queue(db, round_id, "search", f"pubdate:{keyword}:1", keyword=keyword, order="pubdate", page=1)
        db.execute("DELETE FROM cluster_node_round WHERE round<?", (round_id - 2,))
        db.execute("DELETE FROM cluster_video_results WHERE round<?", (round_id - 2,))
        db.execute("DELETE FROM sightings WHERE round>0 AND round<?", (round_id - 2,))
        # Keep a cumulative page count when old rounds are pruned. Older installations
        # can recover only the rounds whose job rows still exist at upgrade time.
        cutoff = round_id - 2
        archived = db.execute(
            "SELECT count(*) FROM jobs WHERE round<? AND kind='search' AND state='done'",
            (cutoff,),
        ).fetchone()[0]
        if archived:
            self.put(db, "search_pages_archived", self.get(db, "search_pages_archived", 0) + archived)
        first = db.execute("SELECT min(round) FROM jobs WHERE round>0").fetchone()[0]
        if first and not self.get(db, "search_history_start"):
            self.put(db, "search_history_start", first)
        db.execute("DELETE FROM jobs WHERE round>0 AND round<?", (cutoff,))
        db.execute("DELETE FROM catalog WHERE expires<?", (now - 7 * 86400,))
        db.execute("DELETE FROM analyses WHERE until<=?", (now,))

    def _catalog(self, db, activity: Activity, rank: int, now: int, origin: str):
        old = db.execute("SELECT payload,rank,origin FROM catalog WHERE id=?", (activity.dynamic_id,)).fetchone()
        if old:
            previous = Activity.model_validate_json(old["payload"])
            other_source = rank == 1 and old["origin"] != origin
            improves_time = previous.lottery_time is None and activity.lottery_time is not None and not previous.closed
            if old["rank"] > rank or (other_source and (activity.closed or not improves_time)):
                return
            if activity.lottery_time is None and previous.lottery_time is not None:
                if not activity.closed:
                    return
                activity = activity.model_copy(update={"lottery_time": previous.lottery_time})
        elif activity.closed:
            return
        expires = activity.expires_at
        if expires <= now and not activity.closed:
            return
        payload = _dump(activity.model_dump())
        if old:
            if old["payload"] == payload:
                db.execute("UPDATE catalog SET rank=?, checked=? WHERE id=?",
                           (rank, now, activity.dynamic_id))
                return
        seq = self.get(db, "sequence", 0) + 1
        self.put(db, "sequence", seq)
        db.execute("""INSERT INTO catalog VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                      seq=excluded.seq,payload=excluded.payload,rank=excluded.rank,expires=excluded.expires,
                      origin=excluded.origin,checked=excluded.checked""",
                   (activity.dynamic_id, seq, payload, rank, now if activity.closed else expires, origin, now))

    def _retry(self, db, job, now, *, delay, reason, remember_watch_failure=False):
        if job["kind"] == "video" and db.execute("SELECT 1 FROM checked_videos WHERE id=?", (job["key"],)).fetchone():
            db.execute("UPDATE jobs SET state='skipped',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
            return
        if job["kind"] == "watch":
            assigned = json.loads(job["params"]).get("assigned")
            if assigned and not db.execute("""SELECT 1 FROM watch_sources
                WHERE mid=? AND EXISTS (SELECT 1 FROM nodes WHERE id=?)""", (job["key"], assigned)).fetchone():
                db.execute("UPDATE jobs SET state='skipped',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
                return
        failed = job["attempts"] >= MAX_ATTEMPTS
        if failed and job["kind"] == "watch":
            params = json.loads(job["params"])
            if not params.get("legacy_batch_check") and self._watch_refs(db, job["id"]):
                params["collection_load_failed"] = True
                db.execute("UPDATE jobs SET state='checking',params=?,node=NULL,lease=NULL,until=0 WHERE id=?",
                           (_dump(params), job["id"]))
                self._event(db, now, "合集读取部分失败，继续核验已收录线索", node=job["node"], level="warning")
                return
        db.execute("UPDATE jobs SET state=?,node=NULL,lease=NULL,until=0,due=? WHERE id=?",
                   ("failed" if failed else "pending", now + delay, job["id"]))
        if failed and job["kind"] == "watch_ref" and remember_watch_failure:
            self._save_watch_ref(db, json.loads(job["params"]), now)
        result = "已达到重试上限" if failed else f"{delay} 秒后可重试" if delay else "已重新排队"
        self._event(db, now, f"{self._job_label(job)}：{reason}，{result}",
                    node=job["node"], level="error" if failed else "warning")

    def _publish_findings(self, db, job, node, activities, now):
        for item in activities:
            self._catalog(db, item, 2, now, node)
            if not item.closed and item.expires_at > now:
                db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,'open_activity',?)",
                           (job["round"], job["id"], item.dynamic_id))
            else:
                db.execute("DELETE FROM sightings WHERE round=? AND job=? AND kind='open_activity' AND key=?",
                           (job["round"], job["id"], item.dynamic_id))

    def _queue_watch_refs(self, db, parent, references, depth):
        if depth > WATCH_LINK_DEPTH:
            return
        for ref in references:
            if ref.kind not in {"dynamic", "article", "short"}:
                continue
            if ref.kind == "dynamic" and db.execute(
                "SELECT 1 FROM sightings WHERE round=0 AND job=? AND kind='open_activity' AND key=?",
                (parent["id"], ref.key),
            ).fetchone():
                continue
            key = f"{parent['id']}:{ref.kind}:{ref.key}"
            params = {"watch_id": parent["id"], "mid": parent["key"], "reference_kind": ref.kind,
                      "target": ref.key, "depth": depth}
            db.execute("""INSERT INTO jobs(round,kind,key,params) VALUES(0,'watch_ref',?,?)
                          ON CONFLICT(round,kind,key) DO UPDATE SET params=excluded.params,
                          state=CASE WHEN jobs.state='done' THEN 'pending' ELSE jobs.state END
                          WHERE jobs.state IN ('pending','leased','done')
                            AND json_extract(jobs.params,'$.depth')>json_extract(excluded.params,'$.depth')""",
                       (key, _dump(params)))

    @staticmethod
    def _watch_refs(db, job_id):
        return dict(db.execute("""SELECT state,count(*) FROM jobs WHERE round=0 AND kind='watch_ref'
                                 AND json_extract(params,'$.watch_id')=? GROUP BY state""", (job_id,)))

    @staticmethod
    def _save_watch_ref(db, params, now, report=None):
        payload = None if report is None else _dump({
            "references": [ref.model_dump() for ref in report.references],
            "activities": [activity.dynamic_id for activity in report.activities if not activity.closed],
        })
        expires = now + (WATCH_FAILED_TTL if report is None else WATCH_VERIFIED_TTL)
        db.execute("""INSERT INTO watch_ref_results VALUES(?,?,?,?) ON CONFLICT(kind,target) DO UPDATE SET
                      expires=excluded.expires,payload=excluded.payload""",
                   (params["reference_kind"], params["target"], expires, payload))

    def _reuse_watch_ref(self, db, job, now):
        params = json.loads(job["params"])
        cached = db.execute("SELECT payload FROM watch_ref_results WHERE kind=? AND target=? AND expires>?",
                            (params["reference_kind"], params["target"], now)).fetchone()
        if cached is None:
            return False
        parent = db.execute("SELECT * FROM jobs WHERE id=?", (params["watch_id"],)).fetchone()
        payload = json.loads(cached["payload"]) if cached["payload"] is not None else None
        if payload is not None:
            db.execute("""INSERT OR IGNORE INTO sightings(round,job,kind,key)
                          SELECT 0,?,'open_activity',id FROM catalog
                          WHERE expires>? AND id IN (SELECT value FROM json_each(?))""",
                       (parent["id"], now, _dump(payload["activities"])))
            self._queue_watch_refs(db, parent, [Reference.model_validate(ref) for ref in payload["references"]],
                                   params["depth"] + 1)
        db.execute("UPDATE jobs SET state=? WHERE id=?", ("failed" if payload is None else "done", job["id"]))
        return True

    def _complete_watch(self, db, job, now, *, node=None, failed=False):
        hits = db.execute("SELECT count(*) FROM sightings WHERE round=0 AND job=? AND kind='open_activity'",
                          (job["id"],)).fetchone()[0]
        db.execute("UPDATE jobs SET state=?,node=NULL,lease=NULL,until=0 WHERE id=?",
                   ("failed" if failed else "done", job["id"]))
        db.execute("""INSERT INTO watch_checks VALUES(?,?,?) ON CONFLICT(mid) DO UPDATE SET
                      checked=excluded.checked,hits=excluded.hits""", (job["key"], now, hits))
        if not failed and (collection := json.loads(job["params"]).get("collection_coverage")):
            key = f"watch_coverage:{job['key']}"
            coverage = self.get(db, key, {})
            if collection["published_at"] >= coverage.get("collection", {}).get("published_at", 0):
                self.put(db, key, {**coverage, "collection": collection})
        message = "部分线索核验失败" if failed else "完成"
        self._event(db, now, f"{self._job_label(job)}{message}，核验 {hits} 项开放活动",
                    node=node, level="warning" if failed else "info")

    def _settle_watch_collections(self, db, now):
        parents = db.execute("SELECT * FROM jobs WHERE round=0 AND kind='watch' AND state='checking'").fetchall()
        for parent in parents:
            counts = self._watch_refs(db, parent["id"])
            if not (counts.get("pending") or counts.get("leased")):
                failed = bool(counts.get("failed") or json.loads(parent["params"]).get("collection_load_failed"))
                self._complete_watch(db, parent, now, failed=failed)

    def publish_watch_findings(self, body: WatchFindings, token):
        now = int(time.time())
        with self.tx() as db:
            self._authenticate(db, body.node, token)
            job = db.execute("""SELECT * FROM jobs WHERE id=? AND node=? AND lease=?
                                AND state='leased' AND until>? AND kind IN ('watch','publisher')""",
                             (body.task_id, body.node, body.lease, now)).fetchone()
            if not job:
                raise ClusterError("追踪任务已交回")
            if body.references:
                if not json.loads(job["params"]).get("collection_mode"):
                    raise ClusterError("本次追踪无需拆分合集线索")
                self._queue_watch_refs(db, job, body.references, 1)
            self._publish_findings(db, job, body.node, body.activities, now)
        return {"ok": True}

    def _finish(self, db, node, report: Report, now):
        job = db.execute("SELECT * FROM jobs WHERE id=? AND node=? AND lease=? AND state='leased' AND until>?",
                         (report.task_id, node, report.lease, now)).fetchone()
        if not job:
            return
        db.execute("UPDATE nodes SET model_calls=model_calls+? WHERE id=?", (report.model_calls, node))
        db.execute("DELETE FROM analyses WHERE lease=? AND fields IS NULL", (report.lease,))
        kind = job["kind"]
        parent = None
        if kind == "watch_ref":
            params = json.loads(job["params"])
            parent = db.execute("SELECT * FROM jobs WHERE id=? AND kind='watch' AND state='checking'",
                                (params["watch_id"],)).fetchone()
            if parent:
                self._publish_findings(db, parent, node, report.activities, now)
        activities = {item.dynamic_id: item for item in report.activities} if kind in {"video", "watch", "publisher"} else {}
        self._publish_findings(db, job, node, activities.values(), now)
        if report.outcome == "cooldown":
            until = max(now + report.cooldown, self.get(db, "cooldown_until", 0))
            self.put(db, "cooldown_until", until)
            db.execute("UPDATE jobs SET attempts=attempts-1 WHERE id=?", (job["id"],))
            db.execute("""UPDATE jobs SET state=CASE WHEN kind='video' AND key IN (SELECT id FROM checked_videos)
                          THEN 'skipped' ELSE 'pending' END,node=NULL,lease=NULL,until=0,due=? WHERE id=?""",
                       (until, job["id"]))
            self._event(db, now, f"{self._job_label(job)}：平台限流，集群暂停领取任务 {until - now} 秒",
                        node=node, level="warning")
            return
        if report.outcome == "retry":
            self._retry(db, job, now, delay=30 * 2 ** (job["attempts"] - 1),
                        reason=RETRY_LABEL.get(report.reason, "任务未能完成"),
                        remember_watch_failure=report.reason in {"dynamic_detail", "dynamic_body", "source_response"})
            return
        if kind == "watch_ref":
            self._save_watch_ref(db, params, now, report)
            if parent:
                self._queue_watch_refs(db, parent, report.references, params["depth"] + 1)
            db.execute("UPDATE jobs SET state='done',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
            db.execute("UPDATE nodes SET completed=completed+1 WHERE id=?", (node,))
            return
        if kind == "publisher":
            if report.publisher_latest and report.publisher_latest.mid == job["key"]:
                publisher_sources.record(db, [report.publisher_latest], now)
            if report.publisher_coverage:
                self.complete_publisher(db, job["key"], report.publisher_coverage, now, node)
            db.execute("UPDATE jobs SET state='done',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
            db.execute("UPDATE nodes SET completed=completed+1 WHERE id=?", (node,))
            message = "检查完成" if report.publisher_coverage else "检查范围不完整，保留名单，等待下轮"
            self._event(db, now, f"{self._job_label(job)}：{message}", node=node)
            return
        if kind == "watch":
            params = json.loads(job["params"])
            coverage_key = f"watch_coverage:{job['key']}"
            coverage = self.get(db, coverage_key, {})
            if report.watch_through and coverage.get("dynamics", 0) < report.watch_through <= now:
                self.put(db, coverage_key, {**coverage, "dynamics": report.watch_through})
            if report.watch_collection and report.watch_collection.published_at <= now:
                params["collection_coverage"] = report.watch_collection.model_dump()
                db.execute("UPDATE jobs SET params=? WHERE id=?", (_dump(params), job["id"]))
            if params.get("legacy_batch_check") and collection_source_for_mid(job["key"]):
                params.update(legacy_batch_check=False, collection_pending=True)
                db.execute("""UPDATE jobs SET params=?,state='pending',node=NULL,lease=NULL,until=0,due=0,attempts=0
                              WHERE id=?""", (_dump(params), job["id"]))
                db.execute("UPDATE nodes SET completed=completed+1 WHERE id=?", (node,))
                self._event(db, now, "已完成动态检查，等待新版节点补查合集", node=node)
                return
            db.execute("UPDATE nodes SET completed=completed+1 WHERE id=?", (node,))
            refs = self._watch_refs(db, job["id"])
            if refs:
                db.execute("UPDATE jobs SET state='checking',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
                self._event(db, now, f"合集已读出 {sum(refs.values())} 条线索，交由追踪节点核验", node=node)
            else:
                completed = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
                self._complete_watch(db, completed, now, node=node)
            return
        round_id = job["round"]
        db.execute("UPDATE jobs SET state='done',node=NULL,lease=NULL,until=0 WHERE id=?", (job["id"],))
        db.execute("UPDATE nodes SET completed=completed+1 WHERE id=?", (node,))
        if kind == "video":
            db.execute("INSERT OR IGNORE INTO checked_videos VALUES(?,?)", (job["key"], now))
            matched = db.execute("SELECT EXISTS(SELECT 1 FROM sightings WHERE round=? AND job=? AND kind='open_activity')",
                                 (round_id, job["id"])).fetchone()[0]
            db.execute("INSERT INTO cluster_video_results VALUES(?,?,?,?)", (job["id"], round_id, node, matched))
            db.execute("""INSERT INTO node_detection_totals VALUES(?,1,1,?) ON CONFLICT(node) DO UPDATE SET
                          videos=videos+1,counted=counted+1,matched=node_detection_totals.matched+excluded.matched""",
                       (node, matched))
        open_activities = db.execute("SELECT count(*) FROM sightings WHERE round=? AND job=? AND kind='open_activity'",
                                     (round_id, job["id"])).fetchone()[0]
        summary = f"核验 {open_activities} 项开放活动" if open_activities else "未确认开放活动"
        new_videos = 0
        if kind == "search":
            params = json.loads(job["params"])
            before = self._video_count(db, round_id)
            cutoff = self.get(db, f"search_coverage:{params['keyword']}", 0) if params["order"] == "pubdate" else 0
            overflow = self._add_videos(db, job, report, cutoff)
            count = self._video_count(db, round_id)
            new_videos = count - before
            summary = f"返回 {len(report.references)} 个视频，去重后收录 {new_videos} 个"
            previous = db.execute(
                "SELECT id FROM jobs WHERE round=? AND kind='search' AND key=?",
                (round_id, f"{params['order']}:{params['keyword']}:{params['page'] - 1}"),
            ).fetchone()
            repeated = previous and not db.execute("""SELECT key FROM sightings WHERE job=? AND kind='video'
                EXCEPT SELECT key FROM sightings WHERE job=? AND kind='video'""", (job["id"], previous["id"])).fetchone()
            covered = params["order"] == "pubdate" and self._record_search_page(
                db, job, params, report, now, cutoff, overflow or bool(repeated),
            )
            if covered:
                summary += "，到达已覆盖时间边界"
            if report.more_pages and not repeated and not covered and params["page"] < SEARCH_PAGE_LIMIT:
                params["page"] += 1
                next_key = f"{params['order']}:{params['keyword']}:{params['page']}"
                self._queue(db, round_id, "search", next_key, **params)
        db.execute("""INSERT INTO cluster_node_round VALUES(?,?,?,?,?,?)
                      ON CONFLICT(round,node) DO UPDATE SET pages=pages+excluded.pages,
                      found=found+excluded.found,videos=videos+excluded.videos,
                      activities=activities+excluded.activities""",
                   (round_id, node, int(kind == "search"), new_videos, int(kind == "video"), open_activities))
        if report.model_calls:
            summary += f"，调用模型 {report.model_calls} 次"
        self._event(db, now, f"{self._job_label(job)}完成：{summary}", node=node)

    @staticmethod
    def _video_count(db, round_id):
        return db.execute("SELECT count(*) FROM jobs WHERE round=? AND kind='video' AND state<>'skipped'",
                          (round_id,)).fetchone()[0]

    def _add_videos(self, db, job, report, cutoff):
        round_id = job["round"]
        remaining = TARGET - self._video_count(db, round_id)
        overflow = False
        dates = report.search_dates or {}
        for ref in report.references:
            if ref.kind != "video":
                continue
            db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,?,?)", (round_id, job["id"], ref.kind, ref.key))
            if ref.key in dates and dates[ref.key] < cutoff:
                continue
            if db.execute("SELECT 1 FROM checked_videos WHERE id=?", (ref.key,)).fetchone():
                continue
            if remaining <= 0:
                if not db.execute("SELECT 1 FROM jobs WHERE round=? AND kind='video' AND key=?",
                                  (round_id, ref.key)).fetchone():
                    overflow = True
                continue
            if db.execute("INSERT OR IGNORE INTO jobs(round,kind,key,params) VALUES(?,'video',?,'{}')",
                          (round_id, ref.key)).rowcount:
                remaining -= 1
                db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,'collected',?)", (round_id, job["id"], ref.key))
        return overflow

    def _record_search_page(self, db, job, params, report, now, cutoff, interrupted):
        key = f"search_scan:{params['keyword']}"
        state = {"round": job["round"], "page": 0} if params["page"] == 1 else self.get(db, key, {})
        dates = report.search_dates
        if (interrupted or dates is None or state.get("round") != job["round"]
                or state.get("page") != params["page"] - 1):
            self.put(db, key, {})
            return False
        timestamps = [dates.get(ref.key) for ref in report.references if ref.kind == "video"]
        page_times = ordered_page_times(timestamps, now, state.get("oldest"))
        if timestamps and page_times is None:
            self.put(db, key, {})
            return False
        if page_times is not None:
            newest, state["oldest"] = page_times
            state.setdefault("newest", newest)
        covered = bool(page_times is not None and page_times[0] < cutoff)
        state.update(page=params["page"], complete=covered or (report.search_exhausted is True and not report.more_pages))
        self.put(db, key, state)
        return covered

    def _commit_search_coverage(self, db, round_id):
        for keyword in KEYWORDS:
            state = self.get(db, f"search_scan:{keyword}", {})
            if state.get("round") == round_id and state.get("complete") and (newest := state.get("newest")):
                key = f"search_coverage:{keyword}"
                if newest > self.get(db, key, 0):
                    self.put(db, key, newest)

    def _merge_history(self, db, node, batch, now):
        previous = db.execute("SELECT complete FROM node_history WHERE node=?", (node,)).fetchone()
        db.execute("""INSERT INTO node_history VALUES(?,?) ON CONFLICT(node) DO UPDATE SET
                      complete=max(complete,excluded.complete)""", (node, batch.complete))
        db.executemany("INSERT OR IGNORE INTO node_video_history VALUES(?,?)", ((node, key) for key in batch.ids))
        db.executemany("INSERT OR IGNORE INTO checked_videos VALUES(?,?)", ((key, now) for key in batch.ids))
        if batch.ids:
            round_id = self.get(db, "round", 0)
            skipped = db.execute("""UPDATE jobs SET state='skipped' WHERE round=? AND kind='video' AND state='pending'
                                    AND key IN (SELECT id FROM checked_videos)""", (round_id,)).rowcount
            if skipped and self.get(db, "phase") == "collecting" and self._video_count(db, round_id) < TARGET:
                db.execute("""UPDATE jobs SET state='pending'
                              WHERE round=? AND kind='search' AND state='skipped'""", (round_id,))
        if batch.complete and (not previous or not previous[0]):
            total = db.execute("SELECT count(*) FROM node_video_history WHERE node=?", (node,)).fetchone()[0]
            self._event(db, now, f"本机历史 BV 已同步，共 {total} 个", node=node)

    @staticmethod
    def _history_syncing(db, now):
        return db.execute("""SELECT count(*) FROM node_history h JOIN nodes n ON n.id=h.node
                             WHERE h.complete=0 AND n.seen>?""", (now - ONLINE_SECONDS,)).fetchone()[0]

    def _queue_participation(self, db):
        previous = self.get(db, "participation_checkpoint", {})
        epoch = max(previous.get("epoch", 0), self.get(db, "round", 0)) + 1
        cursor = db.execute("SELECT coalesce(max(seq),0) FROM catalog").fetchone()[0]
        self.put(db, "participation_checkpoint", {"epoch": epoch, "cursor": cursor})

    def _participation_turn(self, db, body, now, *, ready_to_claim):
        state = db.execute("SELECT * FROM node_participation WHERE node=?", (body.node,)).fetchone()
        outcome = body.round_participation if body.import_round == state["offer"] else None
        if state["offer"] and outcome == "running":
            db.execute("UPDATE node_participation SET deadline=? WHERE node=?", (now + PARTICIPATION_TIMEOUT, body.node))
        elif state["offer"] and outcome in {"done", "failed", "disabled"}:
            db.execute("UPDATE node_participation SET handled=offer,offer=0,deadline=0 WHERE node=?", (body.node,))
            if outcome != "disabled":
                message = "轮间顺序参与完成" if outcome == "done" else "轮间顺序参与未全部完成"
                if summary := body.participation_result:
                    message += (f"，成功 {summary.joined}，失败 {summary.failed}，"
                                f"跳过 {summary.skipped}，未执行 {summary.remaining}")
                self._event(db, now, message, node=body.node, level="warning" if outcome == "failed" else "info")
            # Give the returning node a chance to work before a newer checkpoint.
            return None, False
        expired = db.execute("SELECT node FROM node_participation WHERE offer>0 AND deadline<=?", (now,)).fetchall()
        for node, in expired:
            db.execute("UPDATE node_participation SET handled=offer,offer=0,deadline=0 WHERE node=?", (node,))
            self._event(db, now, "轮间参与未收到节点确认", node=node, level="warning")
        state = db.execute("SELECT * FROM node_participation WHERE node=?", (body.node,)).fetchone()
        if state["offer"]:
            return {"epoch": state["offer"], "granted": True}, True
        checkpoint = self.get(db, "participation_checkpoint")
        if not checkpoint or state["handled"] >= checkpoint["epoch"]:
            return None, False
        epoch = checkpoint["epoch"]
        latest = db.execute("SELECT coalesce(max(seq),0) FROM catalog").fetchone()[0]
        if body.after < min(checkpoint["cursor"], latest):
            return None, False
        modern = body.participation_enabled is not None
        legacy_ack = not modern and state["handled"] < (body.import_round or 0) <= epoch
        if (modern and (not body.participation_enabled or body.participation_pending == 0)
                or legacy_ack and body.round_participation == "disabled"):
            db.execute("UPDATE node_participation SET handled=? WHERE node=?", (epoch, body.node))
            return None, False
        if self.get(db, "paused", False) or self.get(db, "cooldown_until", 0) > now:
            return None, False
        preview = ({"epoch": epoch, "granted": False} if not modern else None)
        if not modern and not (legacy_ack and body.round_participation == "ready"):
            # Advertise a turn to older workers without holding a lease slot for
            # clients that never implemented the readiness acknowledgement.
            return preview, False
        if not ready_to_claim or db.execute("SELECT 1 FROM jobs WHERE node=? AND state='leased'", (body.node,)).fetchone():
            return preview, False
        plan = self.get(db, "work_assignments") or {}
        pools = (plan.get("collection_nodes"), plan.get("watch_nodes"))
        for other, in db.execute("""SELECT node FROM node_participation WHERE offer>0
            UNION SELECT w.node FROM node_local_work w JOIN nodes n ON n.id=w.node
            WHERE w.state='participating' AND n.seen>?""", (now - ONLINE_SECONDS,)):
            if any(pool is None or (body.node in pool and other in pool) for pool in pools):
                return preview, False
        db.execute("UPDATE node_participation SET offer=?,deadline=? WHERE node=?",
                   (epoch, now + PARTICIPATION_TIMEOUT, body.node))
        db.execute("UPDATE nodes SET search_ready=0 WHERE id=?", (body.node,))
        self._event(db, now, "已安排轮间顺序参与", node=body.node)
        return {"epoch": epoch, "granted": True}, True

    def _tick(self, db, now):
        for job in db.execute("SELECT * FROM jobs WHERE state='leased' AND until<=?", (now,)).fetchall():
            self._retry(db, job, now, delay=0, reason="节点未在租约内交回任务")
        self._settle_watch_collections(db, now)
        if self.get(db, "paused", False) or self.get(db, "cooldown_until", 0) > now or self._history_syncing(db, now):
            return
        self._tick_watch(db, now)
        if self.get(db, "collection_paused", False):
            return
        round_id = self.get(db, "round", 0)
        if not round_id:
            self._next_round(db, now)
            return
        if self.get(db, "phase") == "importing" or self.get(db, "next_round", 0):
            # Preserve the granted job ID when upgrading a host mid-import.
            previous = self.get(db, "round_import") or {}
            if node := previous.get("participating"):
                db.execute("""INSERT INTO node_participation(node,offer,deadline) VALUES(?,?,?)
                              ON CONFLICT(node) DO UPDATE SET offer=excluded.offer,deadline=excluded.deadline""",
                           (node, round_id, now + PARTICIPATION_TIMEOUT))
            self._queue_participation(db)
            self.put(db, "round_import", None)
            self._next_round(db, now)
            return
        found = self._video_count(db, round_id)
        if self.get(db, "phase") == "collecting":
            if found >= TARGET:
                db.execute("""UPDATE jobs SET state='skipped'
                              WHERE round=? AND kind='search' AND state='pending'""", (round_id,))
            if self._has_work(db, round_id, "search"):
                return
            self.put(db, "phase", "detecting")
            if found:
                reason = "达到收录目标" if found >= TARGET else "本轮检索结束"
                self._event(db, now, f"{reason}，开始分配 {found} 个已收录视频的检测任务")
        if self._has_work(db, round_id, "video"):
            return
        failed = db.execute("""SELECT count(*) FROM jobs WHERE round=? AND state='failed'
                               AND kind IN ('search','video')""", (round_id,)).fetchone()[0]
        if not failed:
            self._commit_search_coverage(db, round_id)
        self._event(db, now, f"第 {round_id} 轮结束，收录 {found}/{TARGET} 个视频，失败 {failed} 项",
                    level="warning" if failed else "info")
        self._queue_participation(db)
        self._next_round(db, now)

    @staticmethod
    def _has_work(db, round_id, kind):
        return db.execute("""SELECT 1 FROM jobs WHERE round=? AND kind=?
                             AND state IN ('pending','leased','checking') LIMIT 1""", (round_id, kind)).fetchone() is not None

    def _effective_pool(self, db, nodes, fallback, now, *, video=False):
        if nodes is None or not fallback:
            return nodes
        available = db.execute("""SELECT id FROM nodes WHERE seen>? AND (?=0 OR work=2)""",
                               (now - ONLINE_SECONDS, video))
        return nodes if any(row[0] in nodes for row in available) else None

    def _search_turn(self, db, job, node, now, pool):
        if pool is not None and node not in pool:
            return False
        search = self.get(db, "round_search", {})
        if search.get("round") != job["round"]:  # Allow an in-flight round from the previous scheduler to finish.
            return True
        owner = search["node"]
        if owner and (pool is None or owner in pool) and db.execute("""
                SELECT 1 FROM nodes n LEFT JOIN node_local_work w ON w.node=n.id
                LEFT JOIN node_participation p ON p.node=n.id
                WHERE n.id=? AND n.seen>? AND coalesce(w.state,'idle')<>'participating'
                  AND coalesce(p.offer,0)=0""", (owner, now - ONLINE_SECONDS)).fetchone():
            return node == owner
        # Rotate in registration order and wrap, skipping nodes that cannot take work.
        cursor = self.get(db, "search_rotation_cursor", 0)
        candidate = db.execute("""SELECT n.id,n.rowid FROM nodes n
            WHERE n.seen>? AND n.search_ready=1
              AND (? OR n.id IN (SELECT value FROM json_each(?))) AND NOT EXISTS (
                SELECT 1 FROM jobs WHERE node=n.id AND state='leased'
            ) ORDER BY n.rowid<=?,n.rowid LIMIT 1""", (now - ONLINE_SECONDS, pool is None, _dump(pool), cursor)).fetchone()
        if not candidate or candidate["id"] != node:
            return False
        self.put(db, "round_search", {"round": job["round"], "node": node})
        self.put(db, "search_rotation_cursor", candidate["rowid"])
        if owner:
            self._event(db, now, "接续本轮关键词检索", node=node)
        return True

    def _watch_job(self, db, node, now):
        batch = self.get(db, "watch_batch") or {}
        pool = batch.get("nodes")
        modern = db.execute("SELECT watch_batch FROM nodes WHERE id=?", (node,)).fetchone()[0]
        in_pool = pool is None or node in pool
        # A legacy worker can check dynamics. Collection follow-ups prefer selected
        # modern nodes, and otherwise use any online modern node with a ready model.
        preferred_modern = modern and (in_pool or (not batch.get("strict") and not db.execute("""SELECT 1 FROM nodes
            WHERE watch_batch>0 AND work=2 AND seen>? AND id IN (SELECT value FROM json_each(?))""",
            (now - ONLINE_SECONDS, _dump(pool))).fetchone()))
        job = db.execute("""SELECT * FROM jobs WHERE round=0 AND kind='watch' AND state='pending' AND due<=?
            AND ((json_extract(params,'$.assigned')=? AND key IN (SELECT mid FROM watch_sources))
                 OR (json_extract(params,'$.batch')=? AND
                     CASE WHEN json_extract(params,'$.collection_pending') THEN ? ELSE ? END))
            ORDER BY coalesce(json_extract(params,'$.collection_pending'),0),id LIMIT 1""",
            (now, node, batch.get("id"), preferred_modern, in_pool)).fetchone()
        if job is not None or not in_pool or not batch:
            return job, modern
        # Drain cached results in batches so a large collection does not monopolize the write lock.
        for _ in range(PAGE_SIZE):
            job = db.execute("""SELECT j.* FROM jobs j JOIN jobs parent ON parent.id=json_extract(j.params,'$.watch_id')
                WHERE j.round=0 AND j.kind='watch_ref' AND j.state='pending' AND j.due<=?
                  AND parent.state='checking' AND json_extract(parent.params,'$.batch')=?
                  AND NOT EXISTS (
                    SELECT 1 FROM jobs other JOIN jobs owner ON owner.id=json_extract(other.params,'$.watch_id')
                    WHERE other.round=0 AND other.kind='watch_ref' AND owner.state='checking'
                      AND (other.state='leased' OR (other.state='pending' AND other.id<j.id))
                      AND json_extract(other.params,'$.reference_kind')=json_extract(j.params,'$.reference_kind')
                      AND json_extract(other.params,'$.target')=json_extract(j.params,'$.target')
                  )
                ORDER BY j.id LIMIT 1""", (now, batch["id"])).fetchone()
            if job is None or not self._reuse_watch_ref(db, job, now):
                return job, modern
        return None, modern

    def _claim(self, db, node, now, *, video_detection, watch_ready=False):
        phase = self.get(db, "phase")
        kind = {"collecting": "search", "detecting": "video"}.get(phase)
        dispatch = self.get(db, "dispatch", {})
        assignments = self.get(db, "work_assignments")
        collection_paused = self.get(db, "collection_paused", False)
        if (self.get(db, "paused", False) or self.get(db, "cooldown_until", 0) > now or self._history_syncing(db, now)
                or db.execute("SELECT 1 FROM jobs WHERE node=? AND state='leased'", (node,)).fetchone()
                or db.execute("SELECT count(*) FROM jobs WHERE state='leased'").fetchone()[0] >= dispatch.get("concurrency", MAX_CONCURRENT)):
            return None
        job = None
        if watch_ready and video_detection:
            job, modern = self._watch_job(db, node, now)
            batch = self.get(db, "watch_batch") or {}
            if (job is None and (batch.get("nodes") is None or node in batch["nodes"])
                    and db.execute("SELECT publisher_capable FROM nodes WHERE id=?", (node,)).fetchone()[0]):
                job = db.execute("""SELECT * FROM jobs WHERE round=0 AND kind='publisher'
                    AND state='pending' AND due<=? ORDER BY id LIMIT 1""", (now,)).fetchone()
        if job is None and kind and not collection_paused and (kind != "video" or video_detection):
            pool = assignments["collection_nodes"] if assignments is not None else self._effective_pool(
                db, dispatch.get(f"{kind}_nodes"), dispatch.get("fallback"), now, video=kind == "video")
            if pool is not None and node not in pool:
                return None
            job = db.execute("""SELECT * FROM jobs WHERE round=? AND kind=? AND state='pending' AND due<=?
                ORDER BY id LIMIT 1""", (self.get(db, "round", 0), kind, now)).fetchone()
            if job and kind == "search" and not self._search_turn(db, job, node, now, pool):
                return None
        if job is None:
            return None
        params = json.loads(job["params"])
        if job["kind"] == "watch" and params.get("batch"):
            params["legacy_batch_check"] = not modern
            params.pop("collection_mode", None)
            if modern > 1 and collection_source_for_mid(job["key"]):
                params["collection_mode"] = "collection_only" if params.get("collection_pending") else "collection_and_dynamics"
            db.execute("UPDATE jobs SET params=? WHERE id=?", (_dump(params), job["id"]))
        lease = uuid.uuid4().hex
        db.execute("UPDATE jobs SET state='leased',node=?,lease=?,until=?,attempts=attempts+1 WHERE id=?",
                   (node, lease, now + LEASE_SECONDS, job["id"]))
        self._event(db, now, f"开始{self._job_label(job)}（第 {job['attempts'] + 1} 次）", node=node)
        if job["kind"] == "watch_ref":
            return Task(task_id=job["id"], lease=lease, kind=params["reference_kind"], key=params["target"],
                        depth=params["depth"])
        params = params if job["kind"] == "search" else {}
        return Task(task_id=job["id"], lease=lease, kind=job["kind"], key=job["key"], **params)

    def _require_dispatch_nodes(self, db, nodes):
        known = {row[0] for row in db.execute("SELECT id FROM nodes")}
        if not set(nodes) <= known:
            raise ClusterError("有节点已退出集群，请重新选择")

    def set_dispatch(self, body: DispatchSettings):
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以设置任务指派")
            self._require_dispatch_nodes(db, (body.search_nodes or []) + (body.video_nodes or []))
            self.put(db, "dispatch", body.model_dump())
            self._event(db, int(time.time()), f"已更新检索和视频指派，并行上限 {body.concurrency}")

    def set_work_assignments(self, body: WorkAssignments):
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以设置节点分工")
            plan = {key: list(dict.fromkeys(nodes)) for key, nodes in body.model_dump().items()}
            self._require_dispatch_nodes(db, plan["collection_nodes"] + plan["watch_nodes"])
            self.put(db, "work_assignments", plan)
            batch = self.get(db, "watch_batch")
            if batch:
                self.put(db, "watch_batch", {**batch, "nodes": plan["watch_nodes"], "strict": True})
            self._event(db, int(time.time()),
                        f"已保存节点分工，{len(plan['collection_nodes'])} 台主动搜集，{len(plan['watch_nodes'])} 台追踪")

    def assign_watch(self, body: WatchAssignment):
        now = int(time.time())
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以指派追踪检查")
            if not db.execute("SELECT 1 FROM watch_sources WHERE mid=?", (body.mid,)).fetchone():
                raise ClusterError("这个 UP 主已不在集群追踪列表")
            target = db.execute("""SELECT n.name FROM nodes n JOIN node_local_work w ON w.node=n.id
                                 WHERE n.id=? AND n.seen>? AND n.work=2""", (body.node, now - ONLINE_SECONDS)).fetchone()
            if not target:
                raise ClusterError("选择在线、支持追踪检查且模型已就绪的节点")
            existing = db.execute("SELECT state,params FROM jobs WHERE round=0 AND kind='watch' AND key=?", (body.mid,)).fetchone()
            if existing and "batch" in json.loads(existing["params"]):
                raise ClusterError("请在整批追踪中指派这个 UP 主")
            if existing and existing["state"] in {"pending", "leased"}:
                if json.loads(existing["params"])["assigned"] == body.node:
                    return
                if existing["state"] == "leased":
                    raise ClusterError("该 UP 主正在检查中")
            db.execute("""INSERT INTO jobs(round,kind,key,params) VALUES(0,'watch',?,?)
                ON CONFLICT(round,kind,key) DO UPDATE SET params=excluded.params,state='pending',
                node=NULL,lease=NULL,until=0,due=0,attempts=0""", (body.mid, _dump({"assigned": body.node})))
            self._event(db, now, f"追踪 UP 主 {body.mid} 已指派给 {target['name']}")

    def assign_watch_batch(self, body: WatchBatch):
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以指派追踪检查")
            nodes = list(dict.fromkeys(body.nodes)) if body.nodes is not None else None
            plan = self.get(db, "work_assignments")
            if plan is not None:
                nodes = plan["watch_nodes"]
            self._assign_watch_batch(db, nodes, strict=plan is not None)

    def start_watch(self):
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以开始追踪")
            plan = self.get(db, "work_assignments")
            nodes = plan["watch_nodes"] if plan is not None else (self.get(db, "watch_batch") or {}).get("nodes")
            self._assign_watch_batch(db, nodes, strict=plan is not None, start_only=True)
            self.put(db, "watch_repeating", True)
            self.put(db, "watch_next", 0)

    def stop_watch(self):
        with self.tx() as db:
            if (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("只有主节点可以停止追踪轮转")
            self.put(db, "watch_repeating", False)
            self.put(db, "watch_next", 0)

    def _tick_watch(self, db, now):
        batch = self.get(db, "watch_batch")
        if not batch or self._has_work(db, 0, "watch"):
            return
        if self.get(db, "publisher_batch") != batch["id"]:
            self.put(db, "publisher_batch", batch["id"])
            candidates = db.execute("SELECT id FROM nodes WHERE publisher_capable=1 AND work=2 AND seen>?",
                                    (now - ONLINE_SECONDS,))
            if any(batch["nodes"] is None or row["id"] in batch["nodes"] for row in candidates):
                sources = publisher_sources.active(db, due=now)
                db.executemany("INSERT INTO jobs(round,kind,key,params) VALUES(0,'publisher',?,?)",
                               ((source["mid"], _dump({"batch": batch["id"]})) for source in sources))
                if sources:
                    self.put(db, "publisher_batch_started", now)
                    self._event(db, now, f"活动整理UP主与监控检查结束，开始检查 {len(sources)} 位活动发布UP主")
        if self._has_work(db, 0, "publisher"):
            if now - self.get(db, "publisher_batch_started", now) >= 3600:
                skipped = db.execute("UPDATE jobs SET state='skipped' WHERE round=0 AND kind='publisher' AND state='pending'")
                if skipped.rowcount:
                    self._event(db, now, f"{skipped.rowcount} 项发布者检查未在本批开始，将在后续轮次重试", level="warning")
            if self._has_work(db, 0, "publisher"):
                return
        if self.get(db, "watch_finished") != batch["id"]:
            self.put(db, "watch_finished", batch["id"])
            self._event(db, now, "本轮追踪结束")
            self._queue_participation(db)
            if self.get(db, "watch_repeating", False):
                self.put(db, "watch_next", max(now, batch.get("started", now) + WATCH_ROUND_INTERVAL))
        if not self.get(db, "watch_repeating", False) or self.get(db, "watch_next", 0) > now:
            return
        if batch["nodes"] == [] or not db.execute("""SELECT 1 FROM watch_sources UNION ALL
            SELECT 1 FROM publisher_sources WHERE removed=0 LIMIT 1""").fetchone():
            return
        self._assign_watch_batch(db, batch["nodes"], strict=bool(batch.get("strict")), start_only=True)
        self.put(db, "watch_next", 0)

    def _assign_watch_batch(self, db, nodes, *, strict, start_only=False):
        if nodes == []:
            raise ClusterError("请先勾选要追踪的节点")
        self._require_dispatch_nodes(db, nodes or [])
        current = self.get(db, "watch_batch") or {}
        now = int(time.time())
        if self._has_work(db, 0, "publisher"):
            return
        if db.execute("""SELECT 1 FROM jobs WHERE kind='watch' AND state IN ('pending','leased','checking')
                         AND json_extract(params,'$.batch')=?""", (current.get("id"),)).fetchone():
            if not start_only:
                self.put(db, "watch_batch", {**current, "nodes": nodes, "strict": strict})
                self._event(db, now, "已更新本批追踪的执行节点")
            return
        if self._has_work(db, 0, "watch"):
            raise ClusterError("请等待已指派的单项追踪检查结束")
        mids = [row[0] for row in db.execute("SELECT DISTINCT mid FROM watch_sources ORDER BY mid")]
        if not mids and not db.execute("SELECT 1 FROM publisher_sources WHERE removed=0 LIMIT 1").fetchone():
            raise ClusterError("集群尚无可追踪的 UP 主")
        batch = uuid.uuid4().hex
        unresolved = db.execute("""SELECT owner.key AS mid,
                json_extract(ref.params,'$.reference_kind') AS kind,
                json_extract(ref.params,'$.target') AS target,
                json_extract(ref.params,'$.depth') AS depth
            FROM jobs ref JOIN jobs owner ON owner.id=json_extract(ref.params,'$.watch_id')
            WHERE ref.round=0 AND ref.kind='watch_ref' AND ref.state='failed'""").fetchall()
        db.execute("DELETE FROM watch_ref_results WHERE expires<=?", (now,))
        db.execute("DELETE FROM sightings WHERE round=0")
        db.execute("DELETE FROM jobs WHERE round=0 AND kind IN ('watch','watch_ref','publisher')")
        db.executemany("INSERT INTO jobs(round,kind,key,params) VALUES(0,'watch',?,?)",
                       ((mid, _dump({"batch": batch})) for mid in mids))
        parents = {row["key"]: row for row in db.execute("SELECT * FROM jobs WHERE round=0 AND kind='watch'")}
        for ref in unresolved:
            if parent := parents.get(ref["mid"]):
                self._queue_watch_refs(db, parent, [Reference(kind=ref["kind"], key=ref["target"])], ref["depth"])
        self.put(db, "watch_batch", {"id": batch, "nodes": nodes, "strict": strict, "started": now})
        self._event(db, now, f"开始追踪全部 {len(mids)} 位 UP 主")

    @staticmethod
    def _sync_watch_list(db, node, watch_list):
        db.executemany("""INSERT INTO watch_sources VALUES(?,?,?,?) ON CONFLICT(node,mid) DO UPDATE SET
                          name=excluded.name,revision=excluded.revision""",
                       ((node, user.mid, user.name, watch_list.revision) for user in watch_list.users))
        if watch_list.complete:
            db.execute("DELETE FROM watch_sources WHERE node=? AND revision<>?", (node, watch_list.revision))
            db.execute("""UPDATE jobs SET state='skipped' WHERE kind='watch' AND state='pending'
                          AND json_extract(params,'$.batch') IS NULL
                          AND key NOT IN (SELECT mid FROM watch_sources)""")

    def exchange(self, body: Exchange, token: str) -> Feed:
        now = int(time.time())
        with self.tx() as db:
            session = self._authenticate(db, body.node, token)
            readiness = 2 if body.video_detection else int(body.work)
            ready_to_claim = body.work and not body.working and body.local_work in {None, "idle"}
            watch_capability = 2 if body.watch_link_workers else int(bool(body.watch_batch_capable))
            db.execute("UPDATE nodes SET seen=?,work=?,search_ready=?,watch_batch=? WHERE id=?",
                       (now, readiness, ready_to_claim, watch_capability, body.node))
            if body.local_work is None:
                db.execute("DELETE FROM node_local_work WHERE node=?", (body.node,))
            else:
                db.execute("INSERT OR REPLACE INTO node_local_work VALUES(?,?)", (body.node, body.local_work))
            db.execute("""INSERT INTO node_participation(node,pending,enabled) VALUES(?,?,?)
                ON CONFLICT(node) DO UPDATE SET pending=excluded.pending,enabled=excluded.enabled""",
                       (body.node, body.participation_pending, body.participation_enabled))
            if body.watch_list is not None:
                self._sync_watch_list(db, body.node, body.watch_list)
            db.execute("UPDATE nodes SET publisher_capable=? WHERE id=?",
                       (body.publisher_findings is not None, body.node))
            if body.publisher_findings:
                publisher_sources.record(db, body.publisher_findings, now)
            sequence = self.get(db, "sequence", 0)
            for item in body.activities:
                if body.unknown_activity_time or not item.closed:
                    self._catalog(db, item, 1, now, body.node)
            updated = self.get(db, "sequence", 0) - sequence
            if updated:
                self._event(db, now, f"上传活动后，公共目录新增或更新 {updated} 条", node=body.node)
            if body.video_history is not None:
                self._merge_history(db, body.node, body.video_history, now)
            if body.working:
                self._renew(db, body.node, body.working, now)
            if body.report:
                self._finish(db, body.node, body.report, now)
            self._tick(db, now)
            turn, reserved = self._participation_turn(db, body, now, ready_to_claim=ready_to_claim)
            task = None
            if ready_to_claim and not reserved:
                task = self._claim(db, body.node, now, video_detection=body.video_detection,
                                   watch_ready=body.local_work == "idle")
            rows = db.execute("""SELECT seq,payload FROM catalog WHERE seq>?
                                  AND (? OR json_extract(payload,'$.lottery_time') IS NOT NULL)
                                  ORDER BY seq LIMIT ?""", (body.after, body.unknown_activity_time, PAGE_SIZE))
            changes = [Change(seq=row["seq"], activity=Activity.model_validate_json(row["payload"])) for row in rows]
            status = self._status(db, session, now, work_node=body.node)
            if turn and body.participation_enabled is not None:
                status["participation_turn"] = turn
            elif turn:
                status["round_import"] = {"round": turn["epoch"], "node": body.node,
                                          "pending": 1, "done": 0, "skipped": 0,
                                          **({"participating": True} if turn["granted"] else {})}
            if task and task.kind == "watch":
                params = json.loads(db.execute("SELECT params FROM jobs WHERE id=?", (task.task_id,)).fetchone()[0])
                if mode := params.get("collection_mode"):
                    status["watch_collection_mode"] = mode
                if coverage := self.get(db, f"watch_coverage:{task.key}"):
                    status["watch_coverage"] = coverage
            if task and task.kind == "publisher":
                row = db.execute("SELECT checked FROM publisher_sources WHERE mid=?", (task.key,)).fetchone()
                status["watch_coverage"] = {"dynamics": row["checked"] if row else 0}
            status["publisher_sources_supported"] = True
            return Feed(task=task, changes=changes, status=status)

    @staticmethod
    def _renew(db, node, lease: Lease, now):
        db.execute("""UPDATE jobs SET until=? WHERE id=? AND node=? AND lease=?
                      AND state='leased' AND until>?""",
                   (now + LEASE_SECONDS, lease.task_id, node, lease.lease, now))

    def status(self):
        with self.tx(write=False) as db:
            db.execute("BEGIN")
            session = self.get(db, "session") or {}
            return self._status(db, session, int(time.time()), work_node=session.get("node"))

    def detected_activities(self, page: int, *, node: str | None = None, token: str = ""):
        now = int(time.time())
        with self.tx(write=False) as db:
            db.execute("BEGIN")
            if node is not None:
                self._authenticate(db, node, token)
            elif (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("主节点尚未开启集群")
            total = db.execute("SELECT count(*) FROM catalog WHERE rank=2 AND expires>?", (now,)).fetchone()[0]
            pages = max(1, (total + DETECTIONS_PER_PAGE - 1) // DETECTIONS_PER_PAGE)
            page = min(page, pages)
            rows = db.execute("""SELECT payload FROM catalog WHERE rank=2 AND expires>?
                                 ORDER BY expires,id LIMIT ? OFFSET ?""",
                              (now, DETECTIONS_PER_PAGE, (page - 1) * DETECTIONS_PER_PAGE))
            return {"total": total, "page": page, "pages": pages, "items": [json.loads(row[0]) for row in rows]}

    def tracked_users(self, page: int, *, node: str | None = None, token: str = ""):
        with self.tx(write=False) as db:
            db.execute("BEGIN")
            if node is not None:
                self._authenticate(db, node, token)
            elif (self.get(db, "session") or {}).get("role") != "host":
                raise ClusterError("主节点尚未开启集群")
            total = db.execute("SELECT count(DISTINCT mid) FROM watch_sources").fetchone()[0]
            pages = max(1, (total + TRACKED_USERS_PER_PAGE - 1) // TRACKED_USERS_PER_PAGE)
            page = min(page, pages)
            rows = db.execute("""WITH users AS (
                SELECT s.mid,min(s.name) AS name,json_group_array(n.name) AS sources
                FROM watch_sources s JOIN nodes n ON n.id=s.node GROUP BY s.mid
                ORDER BY name,s.mid LIMIT ? OFFSET ?
            ) SELECT u.*, j.state, n.name AS assigned, w.checked, w.hits,
                     (SELECT count(*) FROM jobs ref
                      WHERE ref.round=0 AND ref.kind='watch_ref' AND ref.state='failed'
                        AND json_extract(ref.params,'$.watch_id')=j.id) AS failed_refs
              FROM users u
              LEFT JOIN jobs j ON j.round=0 AND j.kind='watch' AND j.key=u.mid
              LEFT JOIN nodes n ON n.id=coalesce(j.node,json_extract(j.params,'$.assigned'))
              LEFT JOIN watch_checks w ON w.mid=u.mid ORDER BY u.name,u.mid""",
                              (TRACKED_USERS_PER_PAGE, (page - 1) * TRACKED_USERS_PER_PAGE))
            return {"total": total, "pages": pages, "page": page,
                    "items": [{**dict(row), "sources": json.loads(row["sources"])} for row in rows]}

    @staticmethod
    def _search_tasks(db, round_id):
        rows = db.execute("""SELECT j.params,j.state,n.name,coalesce(v.found,0) AS found
            FROM jobs j LEFT JOIN nodes n ON n.id=j.node
            LEFT JOIN (SELECT s.job,count(*) AS found FROM sightings s
                       JOIN jobs video ON video.round=s.round AND video.kind='video' AND video.key=s.key
                       WHERE s.round=? AND s.kind='collected' AND video.state<>'skipped' GROUP BY s.job) v ON v.job=j.id
            WHERE j.round=? AND j.kind='search' ORDER BY j.id""", (round_id, round_id))
        queries = {}
        for row in rows:
            params = json.loads(row["params"])
            key = params["keyword"], params["order"]
            query = queries.setdefault(key, {"keyword": key[0], "order": key[1], "pages": 0, "found": 0,
                                             "failed": 0, "skipped": 0, "queued": []})
            query["pages"] += row["state"] == "done"
            query["failed"] += row["state"] == "failed"
            query["skipped"] += row["state"] == "skipped"
            query["found"] += row["found"]
            if row["state"] in {"pending", "leased"}:
                query["queued"].append({"page": params["page"], "state": row["state"], "node": row["name"]})
        return list(queries.values())

    def _status(self, db, session, now, *, work_node):
        round_id = self.get(db, "round", 0)
        keyword = self.get(db, "round_keyword")
        counts = {}
        for row in db.execute("""SELECT kind,state,count(*) AS n FROM jobs WHERE round=?
                                 AND kind IN ('search','video') GROUP BY kind,state""", (round_id,)):
            counts.setdefault(row["kind"], {})[row["state"]] = row["n"]
        search = db.execute("""SELECT min(round),count(CASE WHEN kind='search' AND state='done' THEN 1 END)
                               FROM jobs WHERE round>=?""", (max(1, round_id - 2),)).fetchone()
        nodes = [dict(row) for row in db.execute("""
            SELECT n.id,n.name,n.seen,n.work,n.watch_batch,n.completed,n.model_calls,j.kind,j.key,j.params,
                   h.complete AS history_complete,l.state AS local_work,
                   p.pending AS participation_pending,p.enabled AS participation_enabled,
                   CASE WHEN j.kind IN ('watch','watch_ref') THEN
                       (SELECT min(name) FROM watch_sources WHERE mid=coalesce(json_extract(j.params,'$.mid'),j.key))
                   END AS watch_name,
                   CASE WHEN h.node IS NOT NULL THEN (SELECT count(*) FROM node_video_history WHERE node=n.id) END AS history_count,
                   coalesce(r.pages,0) AS search_pages,coalesce(r.videos,0) AS videos_checked,
                   coalesce(t.videos,0) AS videos_total,coalesce(t.counted,0) AS videos_counted,
                   coalesce(t.matched,0) AS videos_matched
            FROM nodes n LEFT JOIN jobs j ON n.id=j.node AND j.state='leased' AND j.until>?
            LEFT JOIN node_history h ON h.node=n.id
            LEFT JOIN node_local_work l ON l.node=n.id
            LEFT JOIN node_participation p ON p.node=n.id
            LEFT JOIN cluster_node_round r ON r.round=? AND r.node=n.id
            LEFT JOIN node_detection_totals t ON t.node=n.id
            ORDER BY n.seen DESC,n.name""", (now, round_id))]
        for node in nodes:
            node["online"] = node["seen"] > now - ONLINE_SECONDS
            node["host"] = node["id"] == session.get("node")
            if node["participation_enabled"] is not None:
                node["participation_enabled"] = bool(node["participation_enabled"])
            node["task_label"] = self._job_label(node) if node["kind"] else None
            if node["kind"] == "watch_ref":
                node["kind"] = "watch"
                if node["watch_name"]:
                    node["task_label"] = f"核验「{node['watch_name']}」的合集线索"
            elif node["watch_name"]:
                mode = json.loads(node["params"]).get("collection_mode")
                node["task_label"] = f"读取合集「{node['watch_name']}」" if mode else f"追踪检查「{node['watch_name']}」"
            del node["watch_name"]
            del node["params"]
        local_round = db.execute("SELECT pages,found,videos,activities FROM cluster_node_round WHERE round=? AND node=?",
                                 (round_id, work_node)).fetchone()
        local_events = [dict(row) for row in db.execute(
            "SELECT id,at,level,node,node_name,message FROM cluster_events WHERE node=? ORDER BY id DESC LIMIT 6",
            (work_node,))]
        events = [dict(row) for row in db.execute(
            "SELECT id,at,level,node,node_name,message FROM cluster_events ORDER BY id DESC LIMIT ?",
            (RECENT_EVENTS,))]
        results = db.execute("""SELECT count(*),coalesce(sum(matched),0),
                                       coalesce(sum(node=?),0),coalesce(sum(CASE WHEN node=? THEN matched END),0)
                                FROM cluster_video_results WHERE round=?""", (work_node, work_node, round_id)).fetchone()
        catalog = db.execute("SELECT count(*),coalesce(sum(rank=2),0) FROM catalog WHERE expires>?", (now,)).fetchone()
        phase = self.get(db, "phase", "collecting")
        batch = self.get(db, "watch_batch")
        if batch:
            batch = {**batch, "counts": dict(db.execute("""SELECT
                CASE WHEN state='checking' THEN 'leased' ELSE state END AS display_state,count(*)
                FROM jobs WHERE round=0 AND kind IN ('watch','publisher') AND json_extract(params,'$.batch')=?
                GROUP BY display_state""", (batch["id"],)))}
            batch["references"] = dict(db.execute("""SELECT state,count(*) FROM jobs
                WHERE round=0 AND kind='watch_ref' GROUP BY state"""))
            batch["collection_pending"] = db.execute("""SELECT count(*) FROM jobs WHERE kind='watch'
                AND state='pending' AND json_extract(params,'$.batch')=?
                AND json_extract(params,'$.collection_pending')""", (batch["id"],)).fetchone()[0]
        dispatch = self.get(db, "dispatch", DispatchSettings().model_dump())
        plan = self.get(db, "work_assignments")
        if plan is None:
            search_nodes = dispatch.get("search_nodes")
            video_nodes = dispatch.get("video_nodes")
            plan = {"collection_nodes": None if search_nodes is None or video_nodes is None
                    else sorted(set(search_nodes + video_nodes)),
                    "watch_nodes": batch.get("nodes") if batch else None}
        video_queue = [dict(row) for row in db.execute("""SELECT j.key,j.state,n.name AS node FROM jobs j
                       LEFT JOIN nodes n ON n.id=j.node
                       WHERE j.round=? AND j.kind='video'
                       AND j.state IN ('pending','leased') ORDER BY j.state,j.id LIMIT 12""", (round_id,))]
        return {"round": round_id, "target": TARGET, "counts": counts, "nodes": nodes, "events": events,
                "round_keyword": keyword, "upcoming_keywords": self._upcoming_keywords(keyword),
                "concurrency_limit": dispatch["concurrency"], "two_stage": True, "dispatch": dispatch,
                "work_assignments": plan, "collection_paused": self.get(db, "collection_paused", False),
                "node_rename_supported": True, "discovery_coverage": True,
                "watch_tracking": True, "watch_batch_supported": True, "watch_link_workers": True, "watch_batch": batch,
                "round_participation_supported": True, "independent_participation": True,
                "watch_repeating": self.get(db, "watch_repeating", False),
                "watch_next": self.get(db, "watch_next", 0),
                "video_history": True,
                "known_videos": db.execute("SELECT count(*) FROM checked_videos").fetchone()[0],
                "history_syncing": self._history_syncing(db, now),
                "search_tasks": self._search_tasks(db, round_id), "video_queue": video_queue,
                "round_import": None,
                "search_pages_total": self.get(db, "search_pages_archived", 0) + search[1],
                "search_history_start": self.get(db, "search_history_start", search[0] or round_id),
                "local_work": dict(local_round) if local_round else {"pages": 0, "found": 0, "videos": 0, "activities": 0},
                "local_events": local_events,
                "conversion": {"checked": results[0], "matched": results[1]},
                "local_conversion": {"checked": results[2], "matched": results[3]},
                "detected_open": catalog[1], "retry_detail": True, "unknown_activity_time": True,
                "now": now, "phase": phase, "next_round": self.get(db, "next_round", 0),
                "paused": self.get(db, "paused", False), "cooldown_until": self.get(db, "cooldown_until", 0),
                "sequence": self.get(db, "sequence", 0),
                "active": catalog[0]}
