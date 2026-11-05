"""SQLite is operational state; users access it through CLI/API reports.

A unique scene ID deduplicates all plans. BEGIN IMMEDIATE makes claims atomic.
Only the coordinator publishes outputs; workers may record their own timings.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import time
from pathlib import Path
from .common import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenes (
 id TEXT PRIMARY KEY, scene_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, stage TEXT, bytes_downloaded INTEGER DEFAULT 0,
 download_seconds REAL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL,
 archive TEXT, archive_sha256 TEXT, rows INTEGER DEFAULT 0,
 reference_id TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, path TEXT NOT NULL, created TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_scenes (plan_id TEXT NOT NULL, scene_id TEXT NOT NULL,
 PRIMARY KEY(plan_id,scene_id), FOREIGN KEY(plan_id) REFERENCES plans(id), FOREIGN KEY(scene_id) REFERENCES scenes(id));
CREATE TABLE IF NOT EXISTS scene_regions (scene_id TEXT NOT NULL, country TEXT NOT NULL, state_id TEXT NOT NULL,
 state_name TEXT NOT NULL, PRIMARY KEY(scene_id,country,state_id), FOREIGN KEY(scene_id) REFERENCES scenes(id));
CREATE TABLE IF NOT EXISTS timings (scene_id TEXT NOT NULL, attempt INTEGER NOT NULL, stage TEXT NOT NULL,
 started TEXT NOT NULL, seconds REAL NOT NULL, ok INTEGER NOT NULL, details TEXT NOT NULL,
 FOREIGN KEY(scene_id) REFERENCES scenes(id));
CREATE TABLE IF NOT EXISTS workspace_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS scene_queue ON scenes(status,next_attempt,created);
CREATE INDEX IF NOT EXISTS timing_scene ON timings(scene_id);
"""


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            c.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in c.execute("PRAGMA table_info(scenes)")}
            if "attempt_limit" not in columns:
                c.execute("ALTER TABLE scenes ADD COLUMN attempt_limit INTEGER NOT NULL DEFAULT 3")

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def enqueue(self, plan, path):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            ref = c.execute(
                "SELECT value FROM workspace_meta WHERE key='reference_id'"
            ).fetchone()
            if ref and ref[0] != plan["reference_id"]:
                raise ValueError(
                    "Reference data changed: use a separate output workspace"
                )
            c.execute(
                "INSERT OR IGNORE INTO workspace_meta VALUES ('reference_id',?)",
                (plan["reference_id"],),
            )
            c.execute(
                "INSERT OR IGNORE INTO plans VALUES (?,?,?,?)",
                (plan["id"], str(path), plan["created"], json.dumps(plan["selection"])),
            )
            lake_region = plan.get("selection", {}).get("lake_region", "WORLD")
            existing_region = c.execute("SELECT value FROM workspace_meta WHERE key='lake_region'").fetchone()
            if existing_region and existing_region[0] != lake_region:
                raise ValueError("HydroLAKES universe differs; use a separate output directory for this notebook configuration")
            c.execute("INSERT OR IGNORE INTO workspace_meta VALUES ('lake_region',?)", (lake_region,))
            fingerprint = plan.get("science_fingerprint", "test-unversioned")
            old = c.execute(
                "SELECT value FROM workspace_meta WHERE key='science_fingerprint'"
            ).fetchone()
            if old and old[0] != fingerprint:
                raise ValueError(
                    "Scientific implementation changed: use a separate output workspace"
                )
            c.execute(
                "INSERT OR IGNORE INTO workspace_meta VALUES ('science_fingerprint',?)",
                (fingerprint,),
            )
            for scene in plan["scenes"]:
                sid = scene["id"]
                c.execute(
                    "INSERT OR IGNORE INTO scenes (id,scene_json,created,updated,reference_id) VALUES (?,?,?,?,?)",
                    (sid, json.dumps(scene), utcnow(), utcnow(), plan["reference_id"]),
                )
                c.execute(
                    "INSERT OR IGNORE INTO plan_scenes VALUES (?,?)", (plan["id"], sid)
                )
                for region in scene.get("regions", []):
                    c.execute(
                        "INSERT OR IGNORE INTO scene_regions VALUES (?,?,?,?)",
                        (
                            sid,
                            region["country"],
                            region["state_id"],
                            region["state_name"],
                        ),
                    )

    def claim(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM scenes WHERE status IN ('queued','retry') AND attempts < attempt_limit AND next_attempt<=? ORDER BY created,id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE scenes SET status='running',attempts=attempts+1,error=NULL,stage='starting',bytes_downloaded=0,download_seconds=0,updated=? WHERE id=?",
                (utcnow(), row["id"]),
            )
            return {
                **dict(row),
                "attempts": row["attempts"] + 1,
                "scene": json.loads(row["scene_json"]),
            }

    def update(self, sid, **fields):
        allowed = {
            "status",
            "error",
            "stage",
            "archive",
            "archive_sha256",
            "rows",
            "bytes_downloaded",
            "download_seconds",
            "next_attempt",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError("Unsupported scene update")
        fields["updated"] = utcnow()
        with self.connect() as c:
            c.execute(
                "UPDATE scenes SET "
                + ",".join(f"{k}=?" for k in fields)
                + " WHERE id=?",
                (*fields.values(), sid),
            )

    def fail(self, sid, message):
        with self.connect() as c:
            n, limit = c.execute("SELECT attempts,attempt_limit FROM scenes WHERE id=?", (sid,)).fetchone()
        cycle_attempt = max(1, n - (limit - 3))
        self.update(
            sid,
            status="failed" if n >= limit else "retry",
            error=message[-4000:],
            stage="failed",
            next_attempt=time.time() + min(60 * 2 ** min(cycle_attempt - 1, 3), 300),
        )

    def retry_failed(self):
        """Grant failed scenes three more attempts without replacing scene metadata."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT * FROM scenes WHERE status='failed'").fetchall()
            stamp = utcnow()
            for row in rows:
                c.execute("INSERT INTO timings VALUES (?,?,?,?,?,?,?)", (
                    row["id"], row["attempts"], "manual_requeue", stamp, 0, 1,
                    json.dumps({"previous_error": row["error"], "previous_stage": row["stage"],
                                "previous_attempt_limit": row["attempt_limit"],
                                "new_attempt_limit": row["attempts"] + 3}),
                ))
            c.execute("UPDATE scenes SET status='queued', stage='requeued', next_attempt=0, "
                      "attempt_limit=attempts+3, updated=? WHERE status='failed'", (stamp,))
        return {"requeued": len(rows), "additional_attempts_per_scene": 3,
                "destination": "Original saved archive destination preserved"}

    def timing(self, sid, attempt, stage, started, seconds, ok, details=None):
        with self.connect() as c:
            c.execute(
                "INSERT INTO timings VALUES (?,?,?,?,?,?,?)",
                (
                    sid,
                    attempt,
                    stage,
                    started,
                    seconds,
                    int(ok),
                    json.dumps(details or {}),
                ),
            )

    def rows(self, status=None):
        with self.connect() as c:
            sql = "SELECT * FROM scenes"
            args = ()
            if status:
                sql += " WHERE status=?"
                args = (status,)
            return [dict(r) for r in c.execute(sql + " ORDER BY created,id", args)]

    def scene(self, sid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM scenes WHERE id=?", (sid,)).fetchone()
            if not row:
                raise ValueError(f"Unknown scene: {sid}")
            result = dict(row)
            result["timings"] = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM timings WHERE scene_id=? ORDER BY attempt,started",
                    (sid,),
                )
            ]
            result["regions"] = [
                dict(r)
                for r in c.execute(
                    "SELECT country,state_id,state_name FROM scene_regions WHERE scene_id=?",
                    (sid,),
                )
            ]
            return result

    def summary(self):
        with self.connect() as c:
            return {
                "scope": "Scenes discovered in saved plans; not an estimate of the entire satellite archive",
                "counts": {
                    r[0]: r[1]
                    for r in c.execute(
                        "SELECT status,count(*) FROM scenes GROUP BY status"
                    )
                },
                "plans": c.execute("SELECT count(*) FROM plans").fetchone()[0],
            }

    def regions(self):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    """SELECT r.country,r.state_id,r.state_name,
              count(DISTINCT s.id) AS discovered,
              count(DISTINCT CASE WHEN s.status='done' THEN s.id END) AS processed,
              count(DISTINCT CASE WHEN s.status='failed' THEN s.id END) AS failed
              FROM scene_regions r JOIN scenes s ON r.scene_id=s.id
              GROUP BY r.country,r.state_id,r.state_name ORDER BY r.country,r.state_name"""
                )
            ]
