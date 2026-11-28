"""Evidence and lifecycle of the activity publisher source."""
from __future__ import annotations

INACTIVE_SECONDS = 60 * 86400
MIN_ACTIVITIES = 3
CHECK_INTERVAL = 6 * 3600
MAX_BATCH = 100


def install(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS publisher_evidence(
            activity TEXT PRIMARY KEY, mid TEXT NOT NULL, name TEXT NOT NULL,
            published INTEGER NOT NULL, forwarded INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS publisher_evidence_mid ON publisher_evidence(mid,published);
        CREATE TABLE IF NOT EXISTS publisher_sources(
            mid TEXT PRIMARY KEY, name TEXT NOT NULL,
            last_activity INTEGER NOT NULL, checked INTEGER NOT NULL DEFAULT 0,
            covered_from INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0
        );
    """)


def record(db, findings, now):
    for item in findings:
        if not now - INACTIVE_SECONDS <= item.published <= now:
            continue
        db.execute("INSERT OR IGNORE INTO publisher_evidence(activity,mid,name,published) VALUES(?,?,?,?)",
                   (item.activity, item.mid, item.name, item.published))
        db.execute("""UPDATE publisher_sources SET name=?,last_activity=?,removed=0
            WHERE mid=? AND last_activity<? AND (removed=0 OR removed<?)""",
                   (item.name, item.published, item.mid, item.published, item.published))
        db.execute("""INSERT OR IGNORE INTO publisher_sources(mid,name,last_activity)
            SELECT ?,?,max(published) FROM publisher_evidence WHERE mid=? AND published>=?
            HAVING count(*)>=?""",
                   (item.mid, item.name, item.mid, now - INACTIVE_SECONDS, MIN_ACTIVITIES))


def pending(db):
    return db.execute("""SELECT rowid,activity,mid,name,published FROM publisher_evidence
        WHERE forwarded=0 ORDER BY rowid LIMIT ?""", (MAX_BATCH,)).fetchall()


def active(db, *, due=None):
    query = "SELECT mid,name,last_activity,checked FROM publisher_sources WHERE removed=0"
    params = ()
    if due is not None:
        query += " AND checked<=?"
        params = (due - CHECK_INTERVAL,)
    return [dict(row) for row in db.execute(query + " ORDER BY checked,mid", params)]


def complete(db, mid, lower, upper, now):
    source = db.execute("SELECT * FROM publisher_sources WHERE mid=? AND removed=0", (mid,)).fetchone()
    if not source or not 0 < lower < upper <= now or upper <= source["checked"]:
        return False
    previous = source["covered_from"]
    covered = min(previous, lower) if previous and lower <= source["checked"] else lower
    removed = (upper - max(covered, source["last_activity"]) >= INACTIVE_SECONDS)
    db.execute("UPDATE publisher_sources SET checked=?,covered_from=?,removed=? WHERE mid=?",
               (upper, covered, upper if removed else 0, mid))
    return removed
