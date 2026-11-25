"""Durable control handles, idempotency and artifacts; no application imports."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class ControlError(Exception):
    def __init__(self, code: str, message: str, *, job_id: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code, self.job_id, self.retryable = code, job_id, retryable


class ControlStore:
    def __init__(self, directory: str | Path):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "control.db"
        self.lock = threading.RLock()
        with self.transaction() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records(kind TEXT, key TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY(kind,key));
                CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    result TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS control_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL, backend_id INTEGER, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS control_jobs_backend ON control_jobs(owner,backend_id);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS control_events_job ON events(job_id,seq);
                CREATE TABLE IF NOT EXISTS discovery_videos (
                    bvid TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS discovery_videos_recent ON discovery_videos(last_seen DESC,bvid);
                CREATE TABLE IF NOT EXISTS discovery_video_metadata (
                    bvid TEXT PRIMARY KEY, title TEXT NOT NULL, sightings INTEGER NOT NULL, legacy_history INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_video_sightings (
                    bvid TEXT NOT NULL, page_id TEXT NOT NULL, PRIMARY KEY(bvid,page_id)
                );
                CREATE TABLE IF NOT EXISTS discovery_video_checks (
                    job_id INTEGER NOT NULL, bvid TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job_id,bvid)
                );
                CREATE INDEX IF NOT EXISTS discovery_video_checks_latest ON discovery_video_checks(bvid,job_id DESC);
                CREATE TABLE IF NOT EXISTS discovery_checkpoints (
                    id TEXT PRIMARY KEY, saved_at INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS discovery_checkpoints_age ON discovery_checkpoints(saved_at);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind: str, key: str, value: dict, *, _db=None) -> None:
        with (self.transaction() if _db is None else nullcontext(_db)) as db:
            db.execute("INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload",
                       (kind, key, json.dumps(value, ensure_ascii=False)))

    def get(self, kind: str, key: str) -> dict | None:
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def records(self, kind: str) -> list[dict]:
        with self.transaction() as db:
            rows = db.execute("SELECT payload FROM records WHERE kind=? ORDER BY key", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def remember_discovery_videos(self, videos, *, page_id=None, backfill=False, _db=None) -> None:
        now = utc_now()
        rows = {}
        for video in videos:
            bvid, title = (video, "") if isinstance(video, str) else (video["bvid"], video.get("title", ""))
            rows[bvid] = title or rows.get(bvid, "")
        with (self.transaction() if _db is None else nullcontext(_db)) as db:
            for bvid, title in rows.items():
                created = db.execute("INSERT OR IGNORE INTO discovery_videos VALUES(?,?,?)", (bvid, now, now)).rowcount
                counted = bool(page_id and db.execute(
                    "INSERT OR IGNORE INTO discovery_video_sightings VALUES(?,?)", (bvid, page_id),
                ).rowcount)
                if counted or (page_id is None and not backfill):
                    db.execute("UPDATE discovery_videos SET last_seen=? WHERE bvid=?", (now, bvid))
                db.execute("""
                    INSERT INTO discovery_video_metadata VALUES(?,?,?,?) ON CONFLICT(bvid) DO UPDATE SET
                    title=CASE WHEN excluded.title<>'' AND (discovery_video_metadata.title='' OR NOT ?)
                               THEN excluded.title ELSE discovery_video_metadata.title END,
                    sightings=discovery_video_metadata.sightings+excluded.sightings
                """, (bvid, title, int(counted), int(not created or page_id is None), backfill))

    def save_discovery_video_checks(self, job_id: int, checks: dict, *, _db=None) -> None:
        with (self.transaction() if _db is None else nullcontext(_db)) as db:
            db.executemany("""
                INSERT INTO discovery_video_checks VALUES(?,?,?)
                ON CONFLICT(job_id,bvid) DO UPDATE SET payload=excluded.payload
            """, [(job_id, bvid, json.dumps(check, ensure_ascii=False)) for bvid, check in checks.items()])

    def discovery_checkpoint(self, checkpoint_id: str, *, max_age: int) -> dict | None:
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM discovery_checkpoints WHERE id=? AND saved_at>=?",
                             (checkpoint_id, int(time.time()) - max_age)).fetchone()
        return json.loads(row[0]) if row else None

    def save_discovery_checkpoint(self, checkpoint_id, state, *, job_id, report, checks, video_page=None):
        now = int(time.time())
        with self.transaction() as db:
            if video_page:
                self.remember_discovery_videos(video_page["videos"], page_id=video_page["id"], _db=db)
            if checks:
                self.save_discovery_video_checks(job_id, checks, _db=db)
            self.put("discovery", str(job_id), report, _db=db)
            db.execute("INSERT INTO discovery_checkpoints VALUES(?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET saved_at=excluded.saved_at,payload=excluded.payload",
                       (checkpoint_id, now, json.dumps(state, ensure_ascii=False)))
            db.execute("DELETE FROM discovery_checkpoints WHERE saved_at<?", (now - 7 * 86400,))

    def discovery_video_count(self) -> int:
        with self.transaction() as db:
            return db.execute("SELECT count(*) FROM discovery_videos").fetchone()[0]

    def discovery_video_ids(self) -> set[str]:
        with self.transaction() as db:
            return {row[0] for row in db.execute("SELECT bvid FROM discovery_videos")}

    def list_discovery_videos(self, *, limit: int, offset: int, job_id=None) -> tuple[int, list[dict]]:
        fields = """v.*, c.job_id, c.payload, coalesce(m.title,'') AS title,
                    coalesce(m.sightings,0) AS sightings, coalesce(m.legacy_history,1) AS legacy_history"""
        with self.transaction() as db:
            db.execute("BEGIN")
            if job_id is None:
                total = db.execute("SELECT count(*) FROM discovery_videos").fetchone()[0]
                rows = db.execute(f"""
                    SELECT {fields} FROM discovery_videos v LEFT JOIN discovery_video_metadata m ON m.bvid=v.bvid
                    LEFT JOIN discovery_video_checks c
                    ON c.bvid=v.bvid AND c.job_id=(SELECT max(job_id) FROM discovery_video_checks WHERE bvid=v.bvid)
                    ORDER BY v.last_seen DESC,v.bvid LIMIT ? OFFSET ?
                """, (limit, offset)).fetchall()
            else:
                total = db.execute("SELECT count(*) FROM discovery_video_checks WHERE job_id=?", (job_id,)).fetchone()[0]
                rows = db.execute(f"""
                    SELECT {fields} FROM discovery_video_checks c JOIN discovery_videos v ON v.bvid=c.bvid
                    LEFT JOIN discovery_video_metadata m ON m.bvid=v.bvid
                    WHERE c.job_id=? ORDER BY c.rowid LIMIT ? OFFSET ?
                """, (job_id, limit, offset)).fetchall()
        return total, [{**dict(row), "payload": json.loads(row["payload"] or "{}")} for row in rows]

    def claim(self, key: str, parameters: dict) -> dict | None:
        fingerprint = digest(parameters)
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,result FROM operations WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ControlError("REVISION_CONFLICT", "幂等键已用于不同参数")
                if row[1] is None:
                    raise ControlError("OUTCOME_UNKNOWN", "原请求尚在执行或被中断，请查询任务，不要重复平台写入")
                return json.loads(row[1])
            db.execute("INSERT INTO operations VALUES(?,?,NULL,?)", (key, fingerprint, utc_now()))
        return None

    def complete(self, key: str, result: dict) -> None:
        with self.transaction() as db:
            db.execute("UPDATE operations SET result=? WHERE key=?", (json.dumps(result, ensure_ascii=False), key))

    def new_job(self, action: str, boot_id: str, *, owner="app", metadata=None) -> dict:
        payload = {"job_id": 0, "state": "running", "action": action, "boot_id": boot_id,
                   "result": None, "can_cancel": False, "cancel_requested": False,
                   "created_at": utc_now(), "finished_at": None,
                   "progress": {"step": 0, "total": 0, "message": "任务已接收"}, **(metadata or {})}
        with self.transaction() as db:
            cursor = db.execute("INSERT INTO control_jobs(owner,payload) VALUES(?,?)", (owner, json.dumps(payload)))
            payload["job_id"] = cursor.lastrowid
            db.execute("UPDATE control_jobs SET payload=? WHERE id=?", (json.dumps(payload), cursor.lastrowid))
        self.event(payload["job_id"], {"kind": "created", "job": payload})
        return payload

    def bind_job(self, job_id: int, backend_id: int) -> None:
        with self.transaction() as db:
            db.execute("UPDATE control_jobs SET backend_id=? WHERE id=?", (backend_id, job_id))

    def job_record(self, job_id: int) -> tuple[str, int | None, dict]:
        with self.transaction() as db:
            row = db.execute("SELECT owner,backend_id,payload FROM control_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ControlError("NOT_FOUND", "未找到该控制任务")
        return row[0], row[1], json.loads(row[2])

    def update_job(self, job_id: int, **changes) -> dict:
        with self.lock:
            _, _, payload = self.job_record(job_id)
            updated = {**payload, **changes}
            if updated == payload:
                return payload
            if updated["state"] != "running" and not updated.get("finished_at"):
                updated["finished_at"] = utc_now()
            with self.transaction() as db:
                db.execute("UPDATE control_jobs SET payload=? WHERE id=?", (json.dumps(updated, ensure_ascii=False), job_id))
            self.event(job_id, {"kind": "updated", "job": updated})
            return updated

    def list_jobs(self, *, before=2147483647, limit=50, state=None):
        with self.transaction() as db:
            if state is None:
                rows = db.execute("SELECT payload FROM control_jobs WHERE id<? ORDER BY id DESC LIMIT ?",
                                  (before, limit)).fetchall()
            else:
                rows = db.execute("SELECT payload FROM control_jobs "
                                  "WHERE id<? AND json_extract(payload,'$.state')=? ORDER BY id DESC LIMIT ?",
                                  (before, state, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def event(self, job_id: int, value: dict):
        with self.transaction() as db:
            db.execute("INSERT INTO events(job_id,payload) VALUES(?,?)", (job_id, json.dumps({"ts": utc_now(), **value}, ensure_ascii=False)))

    def events(self, job_id: int, after=0, limit=100):
        self.job_record(job_id)
        with self.transaction() as db:
            rows = db.execute("SELECT seq,payload FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (job_id, after, limit)).fetchall()
        return {"events": [{"seq": r[0], **json.loads(r[1])} for r in rows],
                "next_seq": rows[-1][0] if rows else after, "cursor_expired": False}

    def artifact(self, name: str, content: str | bytes, mime="text/plain", *, redacted=True) -> dict:
        identifier = uuid.uuid4().hex
        folder = self.root / "artifacts"
        folder.mkdir(exist_ok=True)
        raw = content.encode("utf-8") if isinstance(content, str) else content
        (folder / identifier).write_bytes(raw)
        value = {"artifact_id": identifier, "name": name, "mime_type": mime,
                 "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                 "resource_uri": f"binggo://artifacts/{identifier}", "redacted": redacted}
        self.put("artifact", identifier, value)
        return value

    def read_artifact(self, identifier: str, offset=0, size=65536):
        value = self.get("artifact", identifier)
        if not value or not identifier.isalnum():
            raise ControlError("NOT_FOUND", "未登记的产物")
        path = (self.root / "artifacts" / identifier).resolve()
        if path.parent != (self.root / "artifacts").resolve():
            raise ControlError("VALIDATION_ERROR", "产物路径越界")
        with path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(size)
        return {"artifact": value, "text": raw.decode("utf-8", errors="replace") if value["mime_type"].startswith(("text/", "application/json")) else None,
                "next_offset": offset + len(raw) if offset + len(raw) < value["size_bytes"] else None}
