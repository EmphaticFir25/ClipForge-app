"""SQLite storage for users and jobs.

SQLite is enough for one worker and a small user base. Move to Postgres before
running several workers or taking real traffic.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    key_hash    TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id),
    status              TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    source_path         TEXT NOT NULL,
    source_bytes        INTEGER NOT NULL,
    source_minutes      REAL NOT NULL DEFAULT 0,
    top                 INTEGER NOT NULL,
    clip_count          INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    rights_confirmed_at TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs (user_id, created_at);
"""


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def _run(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    # users
    def insert_user(self, user: dict[str, Any]) -> None:
        self._write(
            "INSERT INTO users (id, email, key_hash, plan, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], user["email"], user["key_hash"], user["plan"], user["created_at"]),
        )

    def user_by_key_hash(self, key_hash: str) -> dict | None:
        rows = self._run("SELECT * FROM users WHERE key_hash = ?", (key_hash,))
        return rows[0] if rows else None

    def user_by_email(self, email: str) -> dict | None:
        rows = self._run("SELECT * FROM users WHERE email = ?", (email,))
        return rows[0] if rows else None

    def get_user_by_id(self, user_id: str) -> dict | None:
        rows = self._run("SELECT * FROM users WHERE id = ?", (user_id,))
        return rows[0] if rows else None

    def set_plan(self, email: str, plan: str) -> int:
        return self._write("UPDATE users SET plan = ? WHERE email = ?", (plan, email))

    # jobs
    def insert_job(self, job: dict[str, Any]) -> None:
        cols = ", ".join(job)
        marks = ", ".join("?" for _ in job)
        self._write(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(job.values()))

    def get_job(self, job_id: str) -> dict | None:
        rows = self._run("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return rows[0] if rows else None

    def list_jobs(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._run(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )

    def usage_since(self, user_id: str, since_iso: str) -> dict:
        rows = self._run(
            "SELECT COUNT(*) AS jobs, COALESCE(SUM(source_minutes), 0) AS minutes "
            "FROM jobs WHERE user_id = ? AND created_at >= ?",
            (user_id, since_iso),
        )
        return {"jobs": rows[0]["jobs"], "minutes": float(rows[0]["minutes"])}

    def claim_next_job(self, now_iso: str) -> dict | None:
        """Atomically move the oldest queued job to 'running'. Safe with several workers."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
                ).fetchall()
                if not rows:
                    self._conn.execute("COMMIT")
                    return None
                job_id = rows[0]["id"]
                self._conn.execute(
                    "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                    (now_iso, job_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_job(job_id)

    def finish_job(self, job_id: str, clip_count: int, now_iso: str) -> None:
        self._write(
            "UPDATE jobs SET status = 'done', clip_count = ?, finished_at = ? WHERE id = ?",
            (clip_count, now_iso, job_id),
        )

    def fail_job(self, job_id: str, error: str, now_iso: str) -> None:
        self._write(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (error[:500], now_iso, job_id),
        )
