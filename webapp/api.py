"""HTTP API (FastAPI). Thin layer over service.py, which holds the rules.

Routes are plain `def` so FastAPI runs them in a threadpool; uploads are
blocking file copies.

Not yet run in this repo's CI. The logic is covered by tests/test_service.py.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .billing import BillingConfig, BillingDisabled, BillingError, checkout_form, create_checkout_session, handle_event, verify_signature
from .service import PLANS, QuotaExceeded, Service, ServiceError

STATIC_DIR = Path(__file__).parent / "static"
SITE_DIR = STATIC_DIR / "site"


def create_app(service: Service) -> FastAPI:
    app = FastAPI(title="ClipForge API", version="0.1.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    billing = BillingConfig.from_env()

    @app.get("/", include_in_schema=False)
    def landing():
        return FileResponse(SITE_DIR / "index.html", media_type="text/html")

    @app.get("/pricing", include_in_schema=False)
    def pricing():
        return FileResponse(SITE_DIR / "pricing.html", media_type="text/html")

    @app.get("/app", include_in_schema=False)
    def application():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/checkout", include_in_schema=False)
    def checkout_page():
        return FileResponse(SITE_DIR / "checkout.html", media_type="text/html")

    @app.get("/v1/billing/status")
    def billing_status():
        return {"enabled": billing.enabled}

    @app.post("/v1/billing/checkout")
    def start_checkout(body: dict, user: dict = Depends(current_user)):
        try:
            form = checkout_form(billing, user, str(body.get("plan", "")), str(body.get("interval", "monthly")))
            return {"url": create_checkout_session(billing, form)}
        except BillingDisabled as e:
            raise HTTPException(status_code=503, detail=str(e))
        except BillingError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/v1/billing/webhook", include_in_schema=False)
    async def stripe_webhook(request: Request):
        payload = await request.body()
        if not verify_signature(payload, request.headers.get("stripe-signature"), billing.webhook_secret):
            raise HTTPException(status_code=400, detail="bad signature")
        import json as _json
        result = handle_event(service.store, _json.loads(payload))
        return {"received": True, "result": result}

    site_url = os.environ.get("CLIPFORGE_SITE_URL", "http://127.0.0.1:8000")

    @app.post("/v1/auth/start", status_code=202)
    def start_sign_in(body: dict = Body(...)):
        return {"message": service.request_sign_in(str(body.get("email", "")), site_url)}

    @app.post("/v1/auth/redeem")
    def redeem_sign_in(body: dict = Body(...)):
        try:
            return service.redeem_sign_in(str(body.get("token", "")))
        except ServiceError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/v1/auth/session", status_code=204)
    def sign_out(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        service.sign_out(x_api_key)

    @app.get("/terms", include_in_schema=False)
    def terms():
        return FileResponse(SITE_DIR / "legal" / "terms.html", media_type="text/html")

    @app.get("/privacy", include_in_schema=False)
    def privacy():
        return FileResponse(SITE_DIR / "legal" / "privacy.html", media_type="text/html")

    @app.get("/takedown", include_in_schema=False)
    def takedown():
        return FileResponse(SITE_DIR / "legal" / "takedown.html", media_type="text/html")

    @app.get("/v1/plans")
    def plans():
        return PLANS

    def current_user(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict:
        user = service.authenticate(x_api_key)
        if user is None:
            raise HTTPException(status_code=401, detail="missing or invalid API key")
        return user

    @app.post("/v1/jobs", status_code=201)
    def submit_job(
        file: UploadFile = File(...),
        rights_confirmed: bool = Form(False),
        top: int | None = Form(None),
        user: dict = Depends(current_user),
    ):
        try:
            return service.create_job(user, file.filename or "", file.file,
                                      top=top, rights_confirmed=rights_confirmed)
        except QuotaExceeded as e:
            raise HTTPException(status_code=429, detail=str(e))
        except ServiceError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/v1/me")
    def me(user: dict = Depends(current_user)):
        return service.usage(user)

    @app.get("/v1/jobs")
    def list_jobs(user: dict = Depends(current_user)):
        return service.list_jobs(user)

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, user: dict = Depends(current_user)):
        job = service.get_job(user, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="not found")
        return job

    @app.get("/v1/jobs/{job_id}/clips")
    def list_clips(job_id: str, user: dict = Depends(current_user)):
        clips = service.clip_list(user, job_id)
        if clips is None:
            raise HTTPException(status_code=404, detail="not found")
        return clips

    @app.patch("/v1/jobs/{job_id}/clips/{n}")
    def edit_clip(job_id: str, n: int, body: dict = Body(...), user: dict = Depends(current_user)):
        try:
            return service.update_clip(user, job_id, n, body)
        except ServiceError as e:
            status = 404 if "not found" in str(e) else 400
            raise HTTPException(status_code=status, detail=str(e))

    @app.get("/v1/jobs/{job_id}/clips/{n}/words")
    def clip_words(job_id: str, n: int, user: dict = Depends(current_user)):
        words = service.clip_words(user, job_id, n)
        if words is None:
            raise HTTPException(status_code=404, detail="not found")
        return words

    @app.get("/v1/jobs/{job_id}/clips/{n}.mp4")
    def download_clip(job_id: str, n: int, aspect: str = Query("9:16"), user: dict = Depends(current_user)):
        path = service.variant_file(user, job_id, n, aspect)
        if path is None:
            raise HTTPException(status_code=404, detail="not rendered yet")
        tag = aspect.replace(":", "x")
        return FileResponse(path, media_type="video/mp4", filename=f"{job_id[:8]}_clip_{n:02d}_{tag}.mp4")

    @app.get("/v1/jobs/{job_id}/clips.zip")
    def download_all(job_id: str, aspect: str = Query("9:16"), user: dict = Depends(current_user)):
        path = service.export_zip(user, job_id, aspect)
        if path is None:
            raise HTTPException(status_code=404, detail="nothing rendered in that aspect yet")
        return FileResponse(path, media_type="application/zip", filename=f"clipforge_{job_id[:8]}_{aspect.replace(':', 'x')}.zip")

    @app.get("/v1/schedules")
    def list_schedules(user: dict = Depends(current_user)):
        return service.list_schedules(user)

    @app.post("/v1/schedules", status_code=201)
    def create_schedule(body: dict = Body(...), user: dict = Depends(current_user)):
        try:
            return service.create_schedule(user, str(body.get("job_id", "")), int(body.get("clip_n", 0)),
                                           str(body.get("platform", "")), str(body.get("scheduled_at", "")))
        except QuotaExceeded as e:
            raise HTTPException(status_code=403, detail=str(e))
        except (ServiceError, ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/v1/schedules/{schedule_id}", status_code=204)
    def delete_schedule(schedule_id: str, user: dict = Depends(current_user)):
        if not service.delete_schedule(user, schedule_id):
            raise HTTPException(status_code=404, detail="not found")

    return app
