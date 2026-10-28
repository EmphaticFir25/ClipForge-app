# ClipForge App

A paid web service that turns uploaded livestream recordings into scored, captioned
vertical clips. The processing engine runs locally or on a GPU worker.

**Status: early foundation.** Accounts, uploads, quotas, the job queue and the worker are
built and tested. The FastAPI routes are written but haven't been run yet. Billing,
publishing, and the web frontend are not built. See `docs/LAUNCH_BLOCKERS.md`.

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
webapp/api.py       FastAPI routes (thin layer over service.py)
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
uvicorn webapp.asgi:app --reload   # API on http://127.0.0.1:8000 (needs fastapi installed)
python -m webapp.worker            # in a second terminal
```

Upload a file:

```
curl -H "X-API-Key: $KEY" -F file=@stream.mp4 -F rights_confirmed=true -F top=5 http://127.0.0.1:8000/v1/jobs
```

## Not done yet

- The FastAPI routes haven't been run. Run `pytest` and start uvicorn before relying on them.
- Real-footage tuning, whisper.cpp output check, and face-tracking evaluation.
- Payments, public signup, email login, and password or key rotation.
- Storage cleanup and retention rules.
- Platform publishing with per-user OAuth.
