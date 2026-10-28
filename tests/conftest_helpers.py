"""Shared helpers for the security tests."""
import io
import subprocess
from pathlib import Path

from webapp.service import Service
from webapp.store import Store


def make_service(tmp_path: Path, **kwargs) -> Service:
    kwargs.setdefault("probe_minutes", lambda p: 5.0)
    return Service(Store(tmp_path / "app.sqlite3"), tmp_path, **kwargs)


def make_user(service: Service, email: str, plan: str = "pro") -> dict:
    key = service.create_user(email, plan=plan)
    return service.authenticate(key)


def make_job(service: Service, user: dict, name: str = "s.mp4") -> str:
    job = service.create_job(user, name, io.BytesIO(b"x" * 64), top=2, rights_confirmed=True)
    job_dir = service.job_dir(job["id"])
    (job_dir / "clips").mkdir(exist_ok=True)
    (job_dir / "clips" / "clip_01.mp4").write_bytes(b"mp4")
    service.store.finish_job(job["id"], 1, "2026-10-28T09:00:00+00:00")
    return job["id"]


def make_video(path: Path, seconds: float = 2, audio_only: bool = False) -> Path:
    args = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10"]
    if audio_only:
        args = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440"]
    args += ["-t", str(seconds)]
    if audio_only:
        args += ["-vn", "-c:a", "aac"]
    else:
        args += ["-pix_fmt", "yuv420p"]
    subprocess.run(args + [str(path)], check=True, capture_output=True)
    return path


def connect_fake_account(service: Service, user: dict, platform: str, display: str = "Test channel") -> None:
    """Stores a connected platform account with encrypted fake tokens, as the connect flow would."""
    from cryptography.fernet import Fernet
    from datetime import datetime, timedelta, timezone

    from webapp.secretbox import SecretBox

    if service.secret_box is None:
        service.secret_box = SecretBox(Fernet.generate_key())
    box = service.secret_box
    now = datetime.now(timezone.utc)
    service.store.upsert_platform_account({
        "user_id": user["id"], "platform": platform, "platform_user_id": f"{platform}-{user['id'][:6]}",
        "display_name": display, "scopes": "[]",
        "access_token_enc": box.seal(f"access-{platform}-{user['id'][:6]}"),
        "refresh_token_enc": box.seal(f"refresh-{platform}-{user['id'][:6]}"),
        "access_expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
        "connected_at": now.isoformat(timespec="seconds"),
    })
