# Launch blockers

These must be resolved before charging anyone. Engineering alone doesn't clear them.

## Platform approvals (weeks to months)

- **TikTok:** the Content Posting API needs an app audit before public posts work.
  Unaudited apps post only as private.
- **YouTube:** uploads from unverified Google API projects are private until audited.
- **Instagram:** Meta app review for content publishing.

Publishing is not in the web app yet. Plan it as per-user OAuth through the approved apps.

## Rights and terms

- Require a rights confirmation on every upload (built). Keep the record with the job.
- Write terms of service that cover ownership, licensing, and takedowns.
- Set up a takedown process and a way to delete a user's media.
- Get legal review before launch, especially for content from other people's streams.

## Privacy and data

- Publish a privacy policy covering uploads, transcripts, and deletion.
- Decide retention: how long source files and clips are kept after the job completes.
- Encrypt stored media and keys at rest. Use HTTPS only.

## Business

- Decide pricing and limits. The current plan numbers in `webapp/service.py` are placeholders.
- Model the per-hour cost of GPU processing and LLM inference, then price above it.
- Add billing (for example Stripe) and a way to cancel and downgrade.

## Engineering (before public launch)

- Add CI that runs `pytest -q` on every push. The tests exist; no CI config is committed yet.
- Replace SQLite with Postgres when you move to multiple workers.
- Add object storage, monitoring, and error reporting.
- Verify the engine on real footage, including whisper.cpp output and face tracking.
