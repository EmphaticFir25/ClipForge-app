# Architecture

```
browser / API client
      │  POST /v1/jobs (upload + rights_confirmed)
      ▼
   API (FastAPI)  ──► Service (rules, quotas, ownership) ──► SQLite (users, jobs)
      │                                 │
      │                                 └─► data/jobs/<id>/source.mp4   (uploaded file)
      │
      ▼  GET /v1/jobs/<id>, GET /v1/jobs/<id>/clips/<n>.mp4
                                              ▲
Worker (python -m webapp.worker) ── claims queued jobs atomically
      │
      └─► engine: audio → transcript → moments (LLM) → face-tracked 9:16 crop → captions → clips
```

## Decisions

| Area | Choice | Why |
|---|---|---|
| Input | Upload only | No stream downloads, so no platform ToS problem from our side |
| Database | SQLite | Enough for one worker. Move to Postgres before several workers or real traffic |
| Queue | Table + atomic claim | Simple, and safe with more than one worker on the same database |
| API keys | Random key, stored as SHA-256 hash | The raw key is shown once, never stored |
| Plans | Monthly source minutes (probed at upload), upload size, clip cap, watermark flag | Minutes match how creators think about pricing |
| Failures | Recorded on the job, worker keeps going | One bad video doesn't stop the queue |

## Web UI

Plain HTML, CSS and JavaScript in `webapp/static`, with no build step. The API key is kept in
sessionStorage for the tab only. Clips are fetched with the key and shown through object URLs,
because `<video>` tags can't send custom headers.

## Known gaps

- Uploads are written to local disk. Object storage (S3-compatible) comes with hosting.
- Clips are only available while the job folder exists. There's no retention policy yet.
- The LLM is called from the worker. Hosted inference is needed to scale beyond one GPU.
- `error` text is shown to the user. Strip paths and internals before launch.
