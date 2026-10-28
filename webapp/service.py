"""Business rules: accounts, plans, uploads, ownership and worker logic."""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import smtplib
import uuid
import zipfile
from email.message import EmailMessage
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import BinaryIO, Callable

from .store import Store

GB = 1024 ** 3
MB = 1024 ** 2

# Plan details are proposals, not tested with customers. See docs/PRICING.md.
PLANS = {
    "free": {
        "name": "Free", "price_usd": 0,
        "source_minutes_month": 60, "max_upload_bytes": 2 * GB, "max_clips": 5,
        "watermark": True, "retention_days": 7, "publishing": False, "brand_kit": False, "seats": 1,
    },
    "creator": {
        "name": "Creator", "price_usd": 15,
        "source_minutes_month": 300, "max_upload_bytes": 4 * GB, "max_clips": 15,
        "watermark": False, "retention_days": 30, "publishing": False, "brand_kit": False, "seats": 1,
    },
    "pro": {
        "name": "Pro", "price_usd": 29,
        "source_minutes_month": 600, "max_upload_bytes": 8 * GB, "max_clips": 20,
        "watermark": False, "retention_days": 90, "publishing": True, "brand_kit": True, "seats": 1,
    },
    "business": {
        "name": "Business", "price_usd": 79,
        "source_minutes_month": 1500, "max_upload_bytes": 8 * GB, "max_clips": 30,
        "watermark": False, "retention_days": 90, "publishing": True, "brand_kit": True, "seats": 3,
    },
}
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
ASPECTS = ("9:16", "1:1", "16:9")
FREE_ASPECTS = ("9:16",)
CAPTION_STYLES = ("highlight", "plain", "signal")
PLATFORMS = ("youtube", "tiktok")
MIN_CLIP_S = 3.0
MAX_CLIP_S = 90.0
MAX_WORD_EDIT_CHARS = 40
COPY_CHUNK = 4 * MB


# Platforms that have approved ClipForge's app. Nothing is posted to a platform until it's listed here.
APPROVED_PLATFORMS = {p.strip() for p in os.environ.get("CLIPFORGE_APPROVED_PLATFORMS", "").split(",") if p.strip()}
SCHEDULE_NOT_CONNECTED = "This account isn't connected. Connect it, then schedule the post again."

SIGN_IN_MESSAGE = "If that address can receive email, a sign-in link is on its way. The link works once and expires in 20 minutes."
LOGIN_TTL = timedelta(minutes=20)
LOGIN_REQUESTS_PER_HOUR = 5


class OutboxMailer:
    """Development mailer: writes each message to a folder instead of sending it."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def send(self, to: str, subject: str, body: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}_{uuid.uuid4().hex[:6]}.json"
        (self.folder / name).write_text(json.dumps({"to": to, "subject": subject, "body": body}, indent=2),
                                        encoding="utf-8")


class SMTPMailer:
    """Sends through an SMTP server. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM."""

    def __init__(self, host: str, port: int, user: str, password: str, sender: str):
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)


def mailer_from_env(data_dir: Path):
    host = os.environ.get("SMTP_HOST")
    if host:
        return SMTPMailer(host, int(os.environ.get("SMTP_PORT", "587")), os.environ.get("SMTP_USER", ""),
                          os.environ.get("SMTP_PASSWORD", ""), os.environ.get("MAIL_FROM", "ClipForge <no-reply@localhost>"))
    return OutboxMailer(Path(data_dir) / "outbox")

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


def _default_probe(path: Path) -> float:
    """Minutes of video. Refuses files that aren't a real video (see media.inspect)."""
    from .media import MediaRejected, inspect
    try:
        return inspect(path)["duration_s"] / 60.0
    except MediaRejected as e:
        raise ServiceError(str(e))


class Service:
    def __init__(self, store: Store, data_dir: Path, plans: dict | None = None,
                 probe_minutes: Callable[[Path], float] | None = None, mailer=None,
                 cancel_subscription: Callable[[str], None] | None = None):
        self.store = store
        self.cancel_subscription = cancel_subscription
        self.data_dir = Path(data_dir)
        self.plans = plans or PLANS
        self.probe_minutes = probe_minutes or _default_probe
        self.mailer = mailer or OutboxMailer(self.data_dir / "outbox")

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
        user_id = uuid.uuid4().hex
        self.store.insert_user({
            "id": user_id,
            "email": email,
            "key_hash": hash_key(key),
            "plan": plan,
            "created_at": _now(),
        })
        self._store_key(user_id, key)
        return key

    def _store_key(self, user_id: str, key: str) -> None:
        self.store.insert_api_key({
            "id": uuid.uuid4().hex, "user_id": user_id, "key_hash": hash_key(key),
            "created_at": _now(), "last_used_at": None, "revoked_at": None,
        })

    def authenticate(self, key: str | None) -> dict | None:
        if not key:
            return None
        user = self.store.user_for_active_key(hash_key(key))
        if user is not None:
            self.store.touch_api_key(hash_key(key), _now())
        return user

    def sign_out(self, key: str | None) -> None:
        if key:
            self.store.revoke_api_key(hash_key(key), _now())

    # ----- sign in by email: one flow for new and returning people -----

    def request_sign_in(self, email: str, site_url: str) -> str:
        """Creates the account if needed and emails a single-use link. Always returns the same message."""
        email = email.strip().lower()
        if "@" not in email or len(email) > 254:
            return SIGN_IN_MESSAGE
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        requests = self.store.record_auth_request(email, now.isoformat(timespec="seconds"), since)
        if requests > LOGIN_REQUESTS_PER_HOUR:
            return SIGN_IN_MESSAGE  # silently refuse; the message doesn't change

        if self.store.user_by_email(email) is None:
            self.store.insert_user({
                "id": uuid.uuid4().hex, "email": email, "key_hash": "", "plan": "free", "created_at": _now(),
            })
        token = secrets.token_urlsafe(32)
        self.store.insert_login_link({
            "token_hash": hash_key(token), "email": email, "created_at": _now(),
            "expires_at": (now + LOGIN_TTL).isoformat(timespec="seconds"), "used_at": None,
        })
        link = f"{site_url.rstrip('/')}/app#login={token}"
        self.mailer.send(
            email, "Your ClipForge sign-in link",
            "Use this link to sign in to ClipForge. It works once and expires in 20 minutes.\n\n"
            f"{link}\n\nIf you didn't ask for this, you can ignore this email.",
        )
        return SIGN_IN_MESSAGE

    def redeem_sign_in(self, token: str) -> dict:
        """Trades a single-use link for an API key. The key is returned once."""
        invalid = ServiceError("this link has expired or was already used. Request a new one.")
        if not token:
            raise invalid
        token_hash = hash_key(token)
        link = self.store.get_login_link(token_hash)
        if link is None or link["used_at"] is not None:
            raise invalid
        if datetime.fromisoformat(link["expires_at"]) < datetime.now(timezone.utc):
            raise invalid
        if not self.store.claim_login_link(token_hash, _now()):
            raise invalid
        user = self.store.user_by_email(link["email"])
        if user is None:
            raise invalid
        key = "cf_" + secrets.token_urlsafe(32)
        self._store_key(user["id"], key)
        return {"api_key": key, "email": user["email"], "plan": user["plan"]}

    def delete_account(self, user: dict, confirm_email: str) -> None:
        """Deletes the account, its footage, clips, keys and schedules. Irreversible.

        A paid subscription is cancelled first through `cancel_subscription`. If that fails,
        the account is kept so the user is still billed for nothing they can't reach.
        """
        if (confirm_email or "").strip().lower() != user["email"]:
            raise ServiceError("type your email address to confirm")
        if user.get("stripe_subscription_id"):
            if self.cancel_subscription is None:
                raise ServiceError("cancel your paid plan before deleting your account")
            try:
                self.cancel_subscription(user["stripe_subscription_id"])
            except Exception:
                raise ServiceError("we couldn't cancel your paid plan, so your account is still open. Try again shortly.")
        for job_id in self.store.job_ids_for_user(user["id"]):
            shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
        self.store.delete_user_data(user["id"], user["email"])

    def set_plan(self, email: str, plan: str) -> None:
        if plan not in self.plans:
            raise ServiceError(f"unknown plan: {plan}")
        if self.store.set_plan(email.strip().lower(), plan) == 0:
            raise ServiceError("no account with that email")

    def usage(self, user: dict) -> dict:
        plan = self.plans[user["plan"]]
        used = self.store.usage_since(user["id"], _month_start())
        return {
            "plan": user["plan"],
            "plan_name": plan["name"],
            "minutes_used": round(used["minutes"], 1),
            "minutes_limit": plan["source_minutes_month"],
            "jobs_this_month": used["jobs"],
            "max_upload_mb": plan["max_upload_bytes"] // MB,
            "max_clips": plan["max_clips"],
            "watermark": plan["watermark"],
            "retention_days": plan["retention_days"],
            "publishing": plan["publishing"],
            "aspects": list(ASPECTS if user["plan"] != "free" else FREE_ASPECTS),
        }

    # ----- jobs -----

    def create_job(self, user: dict, filename: str, fileobj: BinaryIO,
                   top: int | None = None, rights_confirmed: bool = False) -> dict:
        if not rights_confirmed:
            raise ServiceError("confirm that you have the rights to this video before uploading")
        ext = Path(filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ServiceError(f"unsupported file type; use one of {', '.join(sorted(ALLOWED_EXTENSIONS))}")

        plan = self.plans[user["plan"]]
        top = min(max(int(top or 10), 1), plan["max_clips"])
        job_id = uuid.uuid4().hex
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        source = job_dir / f"source{ext}"

        try:
            size = self._copy_upload(fileobj, source, plan["max_upload_bytes"])
            minutes = self._measure(source)
            used = self.store.usage_since(user["id"], _month_start())
            if used["minutes"] + minutes > plan["source_minutes_month"]:
                remaining = max(plan["source_minutes_month"] - used["minutes"], 0)
                raise QuotaExceeded(
                    f"this video is {minutes:.0f} minutes; your plan has {remaining:.0f} minutes left this month"
                )
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
            "source_minutes": round(minutes, 2),
            "top": top,
            "clip_count": 0,
            "error": None,
            "rights_confirmed_at": now,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        })
        return self.public_job(self.store.get_job(job_id))

    def _copy_upload(self, fileobj: BinaryIO, dest: Path, limit: int) -> int:
        size = 0
        with dest.open("wb") as out:
            while True:
                chunk = fileobj.read(COPY_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ServiceError("file is larger than your plan allows")
                out.write(chunk)
        if size == 0:
            raise ServiceError("the uploaded file is empty")
        return size

    def _measure(self, source: Path) -> float:
        try:
            minutes = self.probe_minutes(source)
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("could not read this video; try exporting it again as an mp4")
        if minutes <= 0:
            raise ServiceError("this video has no length")
        return minutes

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
            "source_minutes": job["source_minutes"],
            "clip_count": job["clip_count"],
            "clips": list(range(1, job["clip_count"] + 1)),
            "error": job["error"],
            "purged_at": job["purged_at"],
        }


    # ----- clip review and editing -----

    def _allowed_aspects(self, user: dict) -> tuple[str, ...]:
        return ASPECTS if user["plan"] != "free" else FREE_ASPECTS

    def _engine_moments(self, job_id: str) -> list[dict]:
        path = self.job_dir(job_id) / "moments.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _effective(self, job_id: str, n: int, moment: dict) -> dict:
        edit = self.store.get_clip_edit(job_id, n)
        if edit is None:
            return {"start": moment["start"], "end": moment["end"], "caption_style": "highlight",
                    "word_edits": {}, "edited": False}
        return {"start": edit["start"], "end": edit["end"], "caption_style": edit["caption_style"],
                "word_edits": json.loads(edit["word_edits"]), "edited": True}

    def _variant_path(self, job_id: str, n: int, aspect: str) -> Path:
        tag = aspect.replace(":", "x")
        return self.job_dir(job_id) / "clips" / f"clip_{n:02d}__{tag}.mp4"

    def clip_list(self, user: dict, job_id: str) -> list[dict] | None:
        job = self.get_job(user, job_id)
        if job is None:
            return None
        if job["purged_at"]:
            return []  # clips expired under the plan's retention period
        moments = self._engine_moments(job_id)
        renders = self.store.latest_renders(job_id)
        out = []
        for n in range(1, job["clip_count"] + 1):
            if n > len(moments):
                break
            m = moments[n - 1]
            eff = self._effective(job_id, n, m)
            variants = {}
            for aspect in self._allowed_aspects(user):
                latest = next((r for r in renders if r["clip_n"] == n and r["aspect"] == aspect), None)
                if latest is not None:
                    status = latest["status"]
                elif aspect == "9:16" and not eff["edited"]:
                    status = "done"  # the engine's original render
                else:
                    status = "none"
                variants[aspect] = {"status": status, "error": latest["error"] if latest else None}
            out.append({
                "n": n,
                "score": m["score"],
                "title": m["title"],
                "hook": m.get("hook", ""),
                "caption": m["caption"],
                "hashtags": m["hashtags"],
                "reason": m.get("reason", ""),
                "start": eff["start"],
                "end": eff["end"],
                "duration": round(eff["end"] - eff["start"], 2),
                "original_start": m["start"],
                "original_end": m["end"],
                "caption_style": eff["caption_style"],
                "word_edits": eff["word_edits"],
                "edited": eff["edited"],
                "variants": variants,
            })
        return out

    def clip_words(self, user: dict, job_id: str, n: int) -> list[dict] | None:
        clips = self.clip_list(user, job_id)
        if clips is None or not 1 <= n <= len(clips):
            return None
        clip = clips[n - 1]
        path = self.job_dir(job_id) / "transcript.json"
        if not path.exists():
            return []
        words = json.loads(path.read_text(encoding="utf-8"))["words"]
        selected = []
        for i, w in enumerate(words):
            if w["end"] > clip["start"] and w["start"] < clip["end"]:
                text = clip["word_edits"].get(str(i), w["word"])
                selected.append({"i": i, "word": text, "original": w["word"], "start": w["start"], "end": w["end"]})
        return selected

    def update_clip(self, user: dict, job_id: str, n: int, changes: dict) -> dict:
        clips = self.clip_list(user, job_id)
        if clips is None:
            raise ServiceError("job not found")
        if not 1 <= n <= len(clips):
            raise ServiceError("clip not found")
        clip = clips[n - 1]

        start = float(changes.get("start", clip["start"]))
        end = float(changes.get("end", clip["end"]))
        if start < 0 or end <= start:
            raise ServiceError("the clip must end after it starts")
        if not MIN_CLIP_S <= end - start <= MAX_CLIP_S:
            raise ServiceError(f"clips must be between {MIN_CLIP_S:.0f} and {MAX_CLIP_S:.0f} seconds")

        style = changes.get("caption_style", clip["caption_style"])
        if style not in CAPTION_STYLES:
            raise ServiceError(f"caption style must be one of {', '.join(CAPTION_STYLES)}")

        edits = dict(clip["word_edits"])
        for key, text in (changes.get("word_edits") or {}).items():
            if not str(key).isdigit():
                raise ServiceError("word edits must use word numbers")
            text = str(text).strip()
            if not text or len(text) > MAX_WORD_EDIT_CHARS or any(c in text for c in "{}\\"):
                raise ServiceError("a caption word must be 1 to 40 characters without braces or backslashes")
            edits[str(key)] = text

        aspect = changes.get("aspect", "9:16")
        if aspect not in ASPECTS:
            raise ServiceError(f"aspect must be one of {', '.join(ASPECTS)}")
        if aspect not in self._allowed_aspects(user):
            raise ServiceError("square and landscape exports are included on paid plans")

        now = _now()
        self.store.save_clip_edit({
            "job_id": job_id, "clip_n": n, "start": start, "end": end,
            "caption_style": style, "word_edits": json.dumps(edits, ensure_ascii=False), "updated_at": now,
        })
        self.store.insert_render({
            "id": uuid.uuid4().hex, "job_id": job_id, "clip_n": n, "aspect": aspect,
            "status": "queued", "error": None, "created_at": now, "finished_at": None,
        })
        return next(c for c in self.clip_list(user, job_id) if c["n"] == n)

    def render_spec(self, job_id: str, n: int) -> dict:
        """What the worker needs to render one clip: the effective trim, style and word edits."""
        moment = self._engine_moments(job_id)[n - 1]
        eff = self._effective(job_id, n, moment)
        return {"start": eff["start"], "end": eff["end"], "caption_style": eff["caption_style"],
                "word_edits": {int(k): v for k, v in eff["word_edits"].items()}}

    def variant_file(self, user: dict, job_id: str, n: int, aspect: str = "9:16") -> Path | None:
        job = self.get_job(user, job_id)
        if job is None or job["purged_at"] or not 1 <= n <= job["clip_count"] or aspect not in ASPECTS:
            return None
        if aspect == "9:16" and self.store.get_clip_edit(job_id, n) is None:
            original = self.job_dir(job_id) / "clips" / f"clip_{n:02d}.mp4"
            return original if original.exists() else None
        path = self._variant_path(job_id, n, aspect)
        return path if path.exists() else None

    def export_zip(self, user: dict, job_id: str, aspect: str = "9:16") -> Path | None:
        """All of a job's clips in one aspect ratio, as a zip in the job folder."""
        clips = self.clip_list(user, job_id)
        if clips is None:
            return None
        files = []
        for clip in clips:
            path = self.variant_file(user, job_id, clip["n"], aspect)
            if path is not None:
                files.append((path, f"{job_id[:8]}_clip_{clip['n']:02d}_{aspect.replace(':', 'x')}.mp4"))
        if not files:
            return None
        out = self.job_dir(job_id) / "exports" / f"clips_{aspect.replace(':', 'x')}.zip"
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as z:
            for path, name in files:
                z.write(path, arcname=name)
        return out

    # ----- scheduling (publishing needs platform approval; see docs/LAUNCH_BLOCKERS.md) -----

    def create_schedule(self, user: dict, job_id: str, n: int, platform: str, scheduled_at: str) -> dict:
        if not self.plans[user["plan"]]["publishing"]:
            raise QuotaExceeded("scheduling posts is included on Pro and Business")
        if platform not in PLATFORMS:
            raise ServiceError(f"platform must be one of {', '.join(PLATFORMS)}")
        if self.get_job(user, job_id) is None:
            raise ServiceError("job not found")
        try:
            when = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        except ValueError:
            raise ServiceError("scheduled_at must be an ISO 8601 time")
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= datetime.now(timezone.utc) + timedelta(minutes=1):
            raise ServiceError("pick a time at least a minute from now")
        row = {
            "id": uuid.uuid4().hex, "user_id": user["id"], "job_id": job_id, "clip_n": n,
            "platform": platform, "scheduled_at": when.isoformat(timespec="minutes"),
            "status": "scheduled", "created_at": _now(),
        }
        self.store.insert_schedule(row)
        return row

    def list_schedules(self, user: dict) -> list[dict]:
        return self.store.list_schedules(user["id"])

    def delete_schedule(self, user: dict, schedule_id: str) -> bool:
        return self.store.delete_schedule(user["id"], schedule_id) > 0

    # ----- maintenance: run from the worker loop or `python -m webapp.maintenance` -----

    def purge_expired(self, now: datetime | None = None) -> int:
        """Deletes clips and sources for finished jobs older than the owner's plan allows."""
        now = now or datetime.now(timezone.utc)
        purged = 0
        for job in self.store.list_finished_jobs():
            owner = self.store.get_user_by_id(job["user_id"]) or {}
            days = self.plans.get(owner.get("plan", "free"), PLANS["free"])["retention_days"]
            if not job["finished_at"]:
                continue
            if now - datetime.fromisoformat(job["finished_at"]) < timedelta(days=days):
                continue
            shutil.rmtree(self.job_dir(job["id"]), ignore_errors=True)
            self.store.mark_job_purged(job["id"], now.isoformat(timespec="seconds"))
            purged += 1
        return purged

    def dispatch_due_schedules(self, now: datetime | None = None, publish: Callable[[dict], None] | None = None) -> dict:
        """Handles posts whose time has come.

        A platform that hasn't approved ClipForge gets 'waiting_approval' and nothing is sent.
        An approved platform needs a connected account; without one the post is marked failed
        with a message the user can act on. `publish` is the hook for the real upload code.
        """
        now = now or datetime.now(timezone.utc)
        counts = {"waiting_approval": 0, "posted": 0, "failed": 0}
        for row in self.store.due_schedules(now.isoformat(timespec="minutes")):
            if row["platform"] not in APPROVED_PLATFORMS:
                self.store.update_schedule(row["id"], "waiting_approval", None)
                counts["waiting_approval"] += 1
                continue
            if publish is None:
                self.store.update_schedule(row["id"], "failed", SCHEDULE_NOT_CONNECTED)
                counts["failed"] += 1
                continue
            try:
                publish(row)
                self.store.update_schedule(row["id"], "posted", None)
                counts["posted"] += 1
            except Exception as e:
                self.store.update_schedule(row["id"], "failed", f"{type(e).__name__}: {e}"[:300])
                counts["failed"] += 1
        return counts

@dataclass
class Worker:
    """Takes one queued job and runs the pipeline on it.

    run_pipeline(source, job_dir, top, watermark) -> clips rendered. It's injected
    so tests can fake the engine.
    """
    service: Service
    run_pipeline: Callable[[Path, Path, int, bool], int]
    now: Callable[[], str] = _now
    # render_pipeline(job_dir, source, clip_n, aspect, spec, watermark) re-renders one clip
    render_pipeline: Callable | None = None

    def run_once(self) -> bool:
        """Process one queued job, or else one queued re-render. Returns False when idle."""
        if self.run_job_once():
            return True
        return self.run_render_once()

    def run_render_once(self) -> bool:
        row = self.service.store.claim_next_render(self.now())
        if row is None:
            return False
        try:
            if self.render_pipeline is None:
                raise RuntimeError("no render pipeline configured")
            job = self.service.store.get_job(row["job_id"])
            owner = self.service.store.get_user_by_id(job["user_id"]) or {}
            plan = self.service.plans.get(owner.get("plan", "free"), PLANS["free"])
            spec = self.service.render_spec(job["id"], row["clip_n"])
            self.render_pipeline(self.service.job_dir(job["id"]), Path(job["source_path"]),
                                 row["clip_n"], row["aspect"], spec, plan["watermark"])
            self.service.store.finish_render(row["id"], None, self.now())
        except Exception as e:
            self.service.store.finish_render(row["id"], f"{type(e).__name__}: {e}", self.now())
        return True

    def run_job_once(self) -> bool:
        job = self.service.store.claim_next_job(self.now())
        if job is None:
            return False
        owner = self.service.store.get_user_by_id(job["user_id"]) or {}
        plan = self.service.plans.get(owner.get("plan", "free"), PLANS["free"])
        try:
            clips = self.run_pipeline(
                Path(job["source_path"]), self.service.job_dir(job["id"]), job["top"], plan["watermark"],
            )
            self.service.store.finish_job(job["id"], clips, self.now())
        except Exception as e:  # record the failure; the worker keeps running
            self.service.store.fail_job(job["id"], f"{type(e).__name__}: {e}", self.now())
        return True
