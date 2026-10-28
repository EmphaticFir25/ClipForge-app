# ClipForge App

A paid web service that turns uploaded livestream recordings into scored, captioned
vertical clips. The processing engine runs locally or on a GPU worker.

**Status: pre-launch.** Accounts, sign-in, plans, uploads, the job queue, the worker, the
watermark, the FastAPI routes, and the web pages are built and covered by 201 tests. The app
starts under uvicorn and serves its pages. Billing checkout and webhook code exists but isn't
live. YouTube and TikTok account connection and scheduled upload are built but wait on platform
app approval. See `docs/PRICING.md` and `docs/LAUNCH_BLOCKERS.md`.

## Design choices

- **Upload only, no URL downloads.** Users upload footage they own or have rights to.
  This avoids the terms-of-service problems with downloading streams from other platforms.
- **Engine shared with the local tool.** `engine/clipforge` is the same pipeline used by
  the local ClipForge: transcription, moment scoring, face tracking, and captions.
- **Workers are separate from the API.** The API accepts uploads and queues jobs. Workers
  run the GPU work. Scale by running more workers.

## Layout

```
engine/clipforge/   processing pipeline (transcribe, score, track, caption, render)
webapp/store.py     SQLite: users and jobs
webapp/service.py   accounts, API keys, plan limits, uploads, ownership, worker logic
webapp/api.py       FastAPI routes and static files (thin layer over service.py)
webapp/static/      web app: sign-in, upload, job tracking, clip previews, plans
webapp/worker.py    background worker: python -m webapp.worker
webapp/admin.py     admin commands: create users, set plans
docs/               architecture and launch blockers
tests/              service, store and engine tests
```

## Local development

```
pip install -e ".[dev]"            # add ".[ml]" for face tracking
pytest -q                          # runs without FFmpeg, GPU, or network
python -m webapp.admin create-user you@example.com --plan pro
uvicorn webapp.asgi:app --reload   # app on http://127.0.0.1:8000 (needs fastapi installed)
python -m webapp.worker            # in a second terminal
```

Upload a file:

```
curl -H "X-API-Key: $KEY" -F file=@stream.mp4 -F rights_confirmed=true -F top=5 http://127.0.0.1:8000/v1/jobs
```

## Not done yet

- The web UI hasn't been clicked through in a browser. Sign in, upload a file, and check the clip previews.
- Placeholder watermark logo (`webapp/assets/watermark.png`).
- Clip retention and seats aren't enforced yet. See `docs/PRICING.md`.
- Real-footage tuning, whisper.cpp output check, and face-tracking evaluation.
- Turning on payments, public signup, and password or key rotation.
- Storage cleanup and retention rules.
- Platform app approval for TikTok, YouTube, and Instagram before public posting.
