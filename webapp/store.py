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
    created_at  TEXT NOT NULL,
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT
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
CREATE TABLE IF NOT EXISTS clip_edits (
    job_id        TEXT NOT NULL REFERENCES jobs(id),
    clip_n        INTEGER NOT NULL,
    start         REAL NOT NULL,
    end           REAL NOT NULL,
    caption_style TEXT NOT NULL DEFAULT 'highlight',
    word_edits    TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (job_id, clip_n)
);
CREATE TABLE IF NOT EXISTS renders (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    clip_n      INTEGER NOT NULL,
    aspect      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS renders_queue ON renders (status, created_at);
CREATE TABLE IF NOT EXISTS schedules (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    job_id       TEXT NOT NULL REFERENCES jobs(id),
    clip_n       INTEGER NOT NULL,
    platform     TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'scheduled',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_user ON schedules (user_id, scheduled_at);
CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    key_hash     TEXT UNIQUE NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE TABLE IF NOT EXISTS login_links (
    token_hash TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
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
        self._migrate()

    def _migrate(self) -> None:
        # Columns added after the first release are added in place to older databases
        additions = {
            "users": {"stripe_customer_id": "TEXT", "stripe_subscription_id": "TEXT"},
            "jobs": {"purged_at": "TEXT"},
            "schedules": {"error": "TEXT"},
        }
        for table, cols_wanted in additions.items():
            have = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, kind in cols_wanted.items():
                if col not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")

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

    def user_by_customer(self, customer_id: str | None) -> dict | None:
        if not customer_id:
            return None
        rows = self._run("SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,))
        return rows[0] if rows else None

    def set_billing(self, user_id: str, plan: str, customer_id: str | None = None,
                    subscription_id: str | None = None) -> None:
        self._write(
            "UPDATE users SET plan = ?, "
            "stripe_customer_id = COALESCE(?, stripe_customer_id), "
            "stripe_subscription_id = ? WHERE id = ?",
            (plan, customer_id, subscription_id, user_id),
        )

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

    # clip edits (trim, caption text and style); the engine's own values are the defaults
    def get_clip_edit(self, job_id: str, clip_n: int) -> dict | None:
        rows = self._run("SELECT * FROM clip_edits WHERE job_id = ? AND clip_n = ?", (job_id, clip_n))
        return rows[0] if rows else None

    def save_clip_edit(self, edit: dict[str, Any]) -> None:
        self._write(
            "INSERT INTO clip_edits (job_id, clip_n, start, end, caption_style, word_edits, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id, clip_n) DO UPDATE SET "
            "start = excluded.start, end = excluded.end, caption_style = excluded.caption_style, "
            "word_edits = excluded.word_edits, updated_at = excluded.updated_at",
            (edit["job_id"], edit["clip_n"], edit["start"], edit["end"], edit["caption_style"],
             edit["word_edits"], edit["updated_at"]),
        )

    # render requests
    def insert_render(self, render: dict[str, Any]) -> None:
        cols = ", ".join(render)
        marks = ", ".join("?" for _ in render)
        self._write(f"INSERT INTO renders ({cols}) VALUES ({marks})", tuple(render.values()))

    def latest_renders(self, job_id: str) -> list[dict]:
        return self._run(
            "SELECT r.* FROM renders r JOIN (SELECT clip_n, aspect, MAX(created_at) AS m FROM renders "
            "WHERE job_id = ? GROUP BY clip_n, aspect) x ON r.clip_n = x.clip_n AND r.aspect = x.aspect "
            "AND r.created_at = x.m WHERE r.job_id = ?",
            (job_id, job_id),
        )

    def claim_next_render(self, now_iso: str) -> dict | None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT id FROM renders WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchall()
                if not rows:
                    self._conn.execute("COMMIT")
                    return None
                render_id = rows[0]["id"]
                self._conn.execute("UPDATE renders SET status = 'running' WHERE id = ? AND status = 'queued'",
                                   (render_id,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        rows = self._run("SELECT * FROM renders WHERE id = ?", (render_id,))
        return rows[0] if rows else None

    def finish_render(self, render_id: str, error: str | None, now_iso: str) -> None:
        self._write(
            "UPDATE renders SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            ("failed" if error else "done", error[:500] if error else None, now_iso, render_id),
        )

    # schedules
    def insert_schedule(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._write(f"INSERT INTO schedules ({cols}) VALUES ({marks})", tuple(row.values()))

    def list_schedules(self, user_id: str) -> list[dict]:
        return self._run("SELECT * FROM schedules WHERE user_id = ? ORDER BY scheduled_at", (user_id,))

    def delete_schedule(self, user_id: str, schedule_id: str) -> int:
        return self._write("DELETE FROM schedules WHERE user_id = ? AND id = ?", (user_id, schedule_id))

    # api keys: each sign-in gets its own key, which can be revoked on its own
    def insert_api_key(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._write(f"INSERT INTO api_keys ({cols}) VALUES ({marks})", tuple(row.values()))

    def user_for_active_key(self, key_hash: str) -> dict | None:
        rows = self._run(
            "SELECT u.*, k.id AS key_id FROM api_keys k JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = ? AND k.revoked_at IS NULL",
            (key_hash,),
        )
        return rows[0] if rows else None

    def touch_api_key(self, key_hash: str, now_iso: str) -> None:
        self._write("UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?", (now_iso, key_hash))

    def revoke_api_key(self, key_hash: str, now_iso: str) -> int:
        return self._write(
            "UPDATE api_keys SET revoked_at = ? WHERE key_hash = ? AND revoked_at IS NULL", (now_iso, key_hash))

    # login links: single use, short lived. Only the hash of the token is stored.
    def insert_login_link(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._write(f"INSERT INTO login_links ({cols}) VALUES ({marks})", tuple(row.values()))

    def get_login_link(self, token_hash: str) -> dict | None:
        rows = self._run("SELECT * FROM login_links WHERE token_hash = ?", (token_hash,))
        return rows[0] if rows else None

    def claim_login_link(self, token_hash: str, now_iso: str) -> bool:
        """Mark a link used. Returns False if someone else already used it (race-safe)."""
        return self._write(
            "UPDATE login_links SET used_at = ? WHERE token_hash = ? AND used_at IS NULL", (now_iso, token_hash)) == 1

    # retention and scheduling maintenance
    def list_finished_jobs(self) -> list[dict]:
        return self._run("SELECT * FROM jobs WHERE status = 'done' AND purged_at IS NULL")

    def mark_job_purged(self, job_id: str, now_iso: str) -> None:
        self._write("UPDATE jobs SET purged_at = ? WHERE id = ?", (now_iso, job_id))

    def due_schedules(self, now_iso: str) -> list[dict]:
        return self._run(
            "SELECT * FROM schedules WHERE status IN ('scheduled', 'waiting_approval') AND scheduled_at <= ? "
            "ORDER BY scheduled_at",
            (now_iso,),
        )

    def update_schedule(self, schedule_id: str, status: str, error: str | None) -> None:
        self._write("UPDATE schedules SET status = ?, error = ? WHERE id = ?", (status, error, schedule_id))

