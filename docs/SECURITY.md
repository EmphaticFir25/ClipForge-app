# Security status

What is built, what is tested, and what only the owner can do.

## Built and tested (offline, 146 tests)

- **Cross-user isolation.** Every job, clip, render, schedule and key lookup is checked against the signed-in user. A foreign ID and a missing ID get the same answer (`tests/test_isolation.py`).
- **Uploads judged by content.** ffprobe reads the container. A renamed text file, audio-only file, file with no video stream, or over-long source is refused, and nothing is left on disk (`webapp/media.py`, `tests/test_upload_guard.py`).
- **Limits on ffmpeg and ffprobe.** Wrappers in `webapp/bin/` apply memory, CPU and wall-clock limits. The worker uses them for every render (`tests/test_upload_guard.py`).
- **Body size cap while streaming.** Uploads are cut off once they pass the cap, from Content-Length and again as bytes arrive, so a request can't fill the disk (`webapp/security.py`).
- **Sign-in rate limit in the database.** Holds across processes and restarts (`tests/test_ratelimit.py`).
- **Security headers.** A strict Content-Security-Policy (scripts from this origin only, no inline scripts), X-Frame-Options DENY, nosniff, no-referrer, and no-store on `/v1/` responses. Static pages are checked for inline scripts and handlers (`tests/test_security.py`). The pages were loaded in headless Chromium under the real policy with no violations.
- **HTTPS only** (when `CLIPFORGE_HTTPS_ONLY=1`): 308 redirect and HSTS.
- **Token encryption.** Fernet helper for stored platform tokens, with errors that never echo the token or key (`webapp/secretbox.py`). No platform tokens are stored yet, because OAuth isn't built.
- **Log redaction.** API keys, Stripe keys, bearer tokens, OAuth tokens and login links are masked in every log handler (`tests/test_secrets.py`).
- **Account deletion.** `DELETE /v1/me` with the email typed back. Removes files, rows, keys and links. Cancels a paid subscription first and keeps the account if cancellation fails (`tests/test_deletion.py`).

## Not verified in this environment

- **The HTTP routes have never run.** FastAPI could not be installed (the package index is blocked here). `tests/test_api.py` covers the routes and skips without FastAPI. Run `pytest tests/test_api.py` on a machine with FastAPI installed. Reading the code found one bug, which is fixed: a route used `current_user` before it was defined, so the app would have crashed at startup.
- **Stripe, live or test mode.** The Stripe API is unreachable from here and no test keys are configured. Signature checks and webhook handling are tested with signed fake events only.
- **Dependency audit.** `pip-audit` needs the package index, which is blocked here. Run `pip-audit` in CI.
- **Platform OAuth and publishing.** Not built, and not tested against YouTube or TikTok.

## Owner actions

1. Install FastAPI and run `pytest` in a normal environment.
2. Create Stripe test keys, run a checkout, and send a real webhook delivery to the endpoint. Then confirm an incorrect signature is rejected.
3. Set `CLIPFORGE_DATA_KEY`, `CLIPFORGE_HTTPS_ONLY=1` and `CLIPFORGE_TRUST_PROXY=1` (behind a proxy) in production. Use `.env.example` as the template, and keep real values out of the repo.
4. Have a lawyer review `webapp/static/site/legal/*.html`, fill in the placeholders, and remove the draft notices.
5. Register a DMCA designated agent with the US Copyright Office. Without it, the safe harbour in 17 U.S.C. § 512 doesn't apply to the takedown process.
6. Run the maintenance job (`python -m webapp.maintenance --loop`) so retention runs.
