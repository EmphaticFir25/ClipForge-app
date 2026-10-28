"""Clip review, trims, caption edits, aspect variants, exports, schedules and render jobs."""
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from webapp.service import QuotaExceeded, Service, ServiceError, Worker
from webapp.store import Store


@pytest.fixture
def svc(tmp_path):
    return Service(Store(tmp_path / "app.sqlite3"), tmp_path, probe_minutes=lambda p: 10.0)


def _moments(n=3):
    return [{"start": 10.0 + i * 100, "end": 40.0 + i * 100, "score": 90 - i, "title": f"Clip {i+1}",
             "hook": f"Hook {i+1}", "caption": "cap", "hashtags": ["#a"], "reason": "good",
             } for i in range(n)]


def _job_with_clips(svc, email="a@example.com", plan="free", n=3):
    key = svc.create_user(email, plan=plan)
    user = svc.authenticate(key)
    job = svc.create_job(user, "stream.mp4", io.BytesIO(b"x" * 100), top=n, rights_confirmed=True)
    job_dir = svc.job_dir(job["id"])
    (job_dir / "clips").mkdir(parents=True, exist_ok=True)
    (job_dir / "moments.json").write_text(json.dumps(_moments(n)), encoding="utf-8")
    words = [{"word": w, "start": 12.0 + i, "end": 12.5 + i} for i, w in enumerate(["hello", "there", "friend", "again"])]
    (job_dir / "transcript.json").write_text(json.dumps({"words": words, "segments": []}), encoding="utf-8")
    for i in range(1, n + 1):
        (job_dir / "clips" / f"clip_{i:02d}.mp4").write_bytes(b"mp4")
    svc.store.finish_job(job["id"], n, "2026-10-28T10:00:00+00:00")
    return user, job["id"], job_dir


def test_clip_list_exposes_reason_hook_and_default_variant(svc):
    user, job_id, _ = _job_with_clips(svc)
    clips = svc.clip_list(user, job_id)
    assert len(clips) == 3
    first = clips[0]
    assert first["hook"] == "Hook 1" and first["reason"] == "good" and first["score"] == 90
    assert first["variants"]["9:16"]["status"] == "done"
    assert first["variants"] == {"9:16": {"status": "done", "error": None}}  # free plan: vertical only


def test_trim_must_be_a_sensible_length_and_order(svc):
    user, job_id, _ = _job_with_clips(svc)
    with pytest.raises(ServiceError, match="end after"):
        svc.update_clip(user, job_id, 1, {"start": 30, "end": 20})
    with pytest.raises(ServiceError, match="between"):
        svc.update_clip(user, job_id, 1, {"start": 10, "end": 11})
    with pytest.raises(ServiceError, match="between"):
        svc.update_clip(user, job_id, 1, {"start": 10, "end": 200})


def test_trim_saves_and_queues_a_render(svc):
    user, job_id, _ = _job_with_clips(svc)
    clip = svc.update_clip(user, job_id, 1, {"start": 12, "end": 35, "aspect": "9:16"})
    assert clip["start"] == 12 and clip["end"] == 35 and clip["edited"] is True
    assert clip["variants"]["9:16"]["status"] == "queued"
    assert clip["original_start"] == 10.0  # the engine's values are kept for reference


def test_caption_words_are_listed_with_indices_and_can_be_edited(svc):
    user, job_id, _ = _job_with_clips(svc)
    words = svc.clip_words(user, job_id, 1)
    assert [w["word"] for w in words][:2] == ["hello", "there"]
    clip = svc.update_clip(user, job_id, 1, {"word_edits": {"1": "their"}, "caption_style": "plain"})
    assert clip["word_edits"] == {"1": "their"} and clip["caption_style"] == "plain"
    assert svc.clip_words(user, job_id, 1)[1]["word"] == "their"
    assert svc.clip_words(user, job_id, 1)[1]["original"] == "there"


@pytest.mark.parametrize("bad", ["{evil}", "back\\slash", "", "x" * 41])
def test_word_edits_reject_unsafe_text(svc, bad):
    user, job_id, _ = _job_with_clips(svc)
    with pytest.raises(ServiceError):
        svc.update_clip(user, job_id, 1, {"word_edits": {"0": bad}})


def test_word_edit_keys_must_be_numbers_and_style_must_be_known(svc):
    user, job_id, _ = _job_with_clips(svc)
    with pytest.raises(ServiceError, match="word numbers"):
        svc.update_clip(user, job_id, 1, {"word_edits": {"abc": "x"}})
    with pytest.raises(ServiceError, match="caption style"):
        svc.update_clip(user, job_id, 1, {"caption_style": "neon"})


def test_free_plan_cannot_export_square_or_landscape(svc):
    user, job_id, _ = _job_with_clips(svc, plan="free")
    with pytest.raises(ServiceError, match="paid plans"):
        svc.update_clip(user, job_id, 1, {"aspect": "1:1"})
    assert svc.usage(user)["aspects"] == ["9:16"]


def test_paid_plan_gets_all_aspects(svc):
    user, job_id, _ = _job_with_clips(svc, email="p@example.com", plan="pro")
    clip = svc.update_clip(user, job_id, 2, {"aspect": "16:9"})
    assert set(clip["variants"]) == {"9:16", "1:1", "16:9"}
    assert clip["variants"]["16:9"]["status"] == "queued"
    assert svc.usage(user)["aspects"] == ["9:16", "1:1", "16:9"]


def test_other_users_cannot_edit_or_read_clips(svc):
    _, job_id, _ = _job_with_clips(svc, email="owner@example.com")
    stranger = svc.authenticate(svc.create_user("other@example.com"))
    assert svc.clip_list(stranger, job_id) is None
    assert svc.clip_words(stranger, job_id, 1) is None
    with pytest.raises(ServiceError):
        svc.update_clip(stranger, job_id, 1, {"start": 11, "end": 30})


def test_zip_export_contains_each_clip_in_that_aspect(svc):
    user, job_id, job_dir = _job_with_clips(svc, email="p@example.com", plan="pro")
    (job_dir / "clips" / "clip_01__1x1.mp4").write_bytes(b"sq1")
    (job_dir / "clips" / "clip_02__1x1.mp4").write_bytes(b"sq2")
    path = svc.export_zip(user, job_id, "1:1")
    with zipfile.ZipFile(path) as z:
        names = sorted(z.namelist())
        assert len(names) == 2 and all("1x1" in n for n in names)
        assert z.read(names[0]) == b"sq1"
    assert svc.export_zip(user, job_id, "16:9") is None  # nothing rendered in that aspect yet


def test_scheduling_is_a_pro_feature(svc):
    user, job_id, _ = _job_with_clips(svc, plan="free")
    later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(QuotaExceeded, match="Pro and Business"):
        svc.create_schedule(user, job_id, 1, "youtube", later)


def test_schedule_validation_and_listing(svc):
    user, job_id, _ = _job_with_clips(svc, email="p@example.com", plan="pro")
    later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    row = svc.create_schedule(user, job_id, 1, "tiktok", later)
    assert row["status"] == "scheduled" and row["platform"] == "tiktok"
    assert [r["id"] for r in svc.list_schedules(user)] == [row["id"]]
    with pytest.raises(ServiceError, match="platform"):
        svc.create_schedule(user, job_id, 1, "myspace", later)
    with pytest.raises(ServiceError, match="minute"):
        svc.create_schedule(user, job_id, 1, "youtube", datetime.now(timezone.utc).isoformat())
    assert svc.delete_schedule(user, row["id"]) is True
    assert svc.list_schedules(user) == []


def test_worker_renders_queued_edits(svc):
    user, job_id, job_dir = _job_with_clips(svc, email="p@example.com", plan="pro")
    svc.update_clip(user, job_id, 1, {"start": 12, "end": 30, "aspect": "1:1",
                                      "word_edits": {"0": "hi"}, "caption_style": "signal"})
    seen = {}

    def render(job_dir_arg, source, clip_n, aspect, spec, watermark):
        seen.update(clip_n=clip_n, aspect=aspect, spec=spec, watermark=watermark)
        (job_dir_arg / "clips" / f"clip_{clip_n:02d}__1x1.mp4").write_bytes(b"sq")

    worker = Worker(svc, lambda *a: 0, render_pipeline=render)
    assert worker.run_once() is True
    assert seen["clip_n"] == 1 and seen["aspect"] == "1:1"
    assert seen["spec"]["word_edits"] == {0: "hi"} and seen["spec"]["caption_style"] == "signal"
    assert seen["watermark"] is False
    clip = next(c for c in svc.clip_list(user, job_id) if c["n"] == 1)
    assert clip["variants"]["1:1"]["status"] == "done"
    assert svc.variant_file(user, job_id, 1, "1:1").exists()


def test_failed_render_is_recorded_and_worker_continues(svc):
    user, job_id, _ = _job_with_clips(svc, email="p@example.com", plan="pro")
    svc.update_clip(user, job_id, 2, {"aspect": "16:9"})

    def broken(*a):
        raise RuntimeError("ffmpeg exploded")

    Worker(svc, lambda *a: 0, render_pipeline=broken).run_once()
    clip = next(c for c in svc.clip_list(user, job_id) if c["n"] == 2)
    assert clip["variants"]["16:9"]["status"] == "failed"
    assert "ffmpeg exploded" in clip["variants"]["16:9"]["error"]


def test_idle_worker_with_no_renders_returns_false(svc):
    assert Worker(svc, lambda *a: 0, render_pipeline=lambda *a: None).run_once() is False
