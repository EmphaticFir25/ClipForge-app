"""Business rules: accounts, plan limits, upload checks and job ownership."""
from __future__ import annotations

import hashlib
import secrets
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

from .store import Store

GB = 1024 ** 3
MB = 1024 ** 2

# Limits are placeholders until pricing is decided. See docs/LAUNCH_BLOCKERS.md.
PLANS = {
    "free": {"jobs_per_month": 3, "max_upload_bytes": 1 * GB, "max_clips": 5},
    "pro": {"jobs_per_month": 60, "max_upload_bytes": 8 * GB, "max_clips": 20},
}
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
COPY_CHUNK = 4 * MB


class ServiceError(ValueError):
    pass


class QuotaExceeded(ServiceError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _month_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Service:
    def __init__(self, store: Store, data_dir: Path, plans: dict | None = None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.plans = plans or PLANS

    # ----- accounts -----

    def create_user(self, email: str, plan: str = "free") -> str:
        """Returns a new API key. It's shown once and only its hash is stored."""
        email = email.strip().lower()
        if "@" not in email:
            raise ServiceError("enter a valid email address")
        if plan not in self.plans:
            raise ServiceError(f"unknown plan: {plan}")
        if self.store.user_by_email(email):
            raise ServiceError("that email already has an account")
        key = "cf_" + secrets.token_urlsafe(32)
        self.store.insert_user({
            "id": uuid.uuid4().hex,
            "email": email,
            "key_hash": hash_key(key),
            "plan": plan,
            "created_at": _now(),
        })
        return key

    def authenticate(self, key: str | None) -> dict | None:
        if not key:
            return None
        return self.store.user_by_key_hash(hash_key(key))

    def set_plan(self, email: str, plan: str) -> None:
        if plan not in self.plans:
            raise ServiceError(f"unknown plan: {plan}")
        if self.store.set_plan(email.strip().lower(), plan) == 0:
            raise ServiceError("no account with that email")

    # ----- jobs -----

    def create_job(self, user: dict, filename: str, fileobj: BinaryIO,
                   top: int | None = None, rights_confirmed: bool = False) -> dict:
        if not rights_confirmed:
            raise ServiceError("confirm that you have the rights to this video before uploading")
        ext = Path(filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ServiceError(f"unsupported file type; use one of {', '.join(sorted(ALLOWED_EXTENSIONS))}")

        plan = self.plans[user["plan"]]
        used = self.store.count_jobs_since(user["id"], _month_start())
        if used >= plan["jobs_per_month"]:
            raise QuotaExceeded(f"monthly limit reached ({plan['jobs_per_month']} jobs on your plan)")

        top = min(max(int(top or 10), 1), plan["max_clips"])
        job_id = uuid.uuid4().hex
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        source = job_dir / f"source{ext}"

        size = 0
        try:
            with source.open("wb") as out:
                while True:
                    chunk = fileobj.read(COPY_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > plan["max_upload_bytes"]:
                        raise ServiceError("file is larger than your plan allows")
                    out.write(chunk)
            if size == 0:
                raise ServiceError("the uploaded file is empty")
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

        now = _now()
        self.store.insert_job({
            "id": job_id,
            "user_id": user["id"],
            "status": "queued",
            "source_path": str(source),
            "source_bytes": size,
            "top": top,
            "clip_count": 0,
            "error": None,
            "rights_confirmed_at": now,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        })
        return self.public_job(self.store.get_job(job_id))

    def get_job(self, user: dict, job_id: str) -> dict | None:
        job = self.store.get_job(job_id)
        if job is None or job["user_id"] != user["id"]:
            return None  # same answer for "missing" and "not yours"
        return self.public_job(job)

    def list_jobs(self, user: dict) -> list[dict]:
        return [self.public_job(j) for j in self.store.list_jobs(user["id"])]

    def clip_file(self, user: dict, job_id: str, n: int) -> Path | None:
        job = self.get_job(user, job_id)
        if job is None or not 1 <= n <= job["clip_count"]:
            return None
        path = self.job_dir(job_id) / "clips" / f"clip_{n:02d}.mp4"
        return path if path.exists() else None

    def job_dir(self, job_id: str) -> Path:
        return self.data_dir / "jobs" / job_id

    def public_job(self, job: dict) -> dict:
        """Only the fields the API exposes. No file paths, no internal user ids."""
        return {
            "id": job["id"],
            "status": job["status"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "clip_count": job["clip_count"],
            "clips": list(range(1, job["clip_count"] + 1)),
            "error": job["error"],
        }


@dataclass
class Worker:
    """Takes one queued job and runs the pipeline on it. `run_pipeline` is injected so tests can fake it."""
    service: Service
    run_pipeline: Callable[[Path, Path, int], int]  # (source, job_dir, top) -> clips rendered
    now: Callable[[], str] = _now

    def run_once(self) -> bool:
        job = self.service.store.claim_next_job(self.now())
        if job is None:
            return False
        try:
            clips = self.run_pipeline(Path(job["source_path"]), self.service.job_dir(job["id"]), job["top"])
            self.service.store.finish_job(job["id"], clips, self.now())
        except Exception as e:  # record the failure; the worker keeps running
            self.service.store.fail_job(job["id"], f"{type(e).__name__}: {e}", self.now())
        return True
