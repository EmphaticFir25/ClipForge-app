"""HTTP API (FastAPI). Thin layer over service.py, which holds the rules.

Routes are plain `def` so FastAPI runs them in a threadpool; uploads are
blocking file copies.

Not yet run in this repo's CI. The logic is covered by tests/test_service.py.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .service import QuotaExceeded, Service, ServiceError


def create_app(service: Service) -> FastAPI:
    app = FastAPI(title="ClipForge API", version="0.1.0")

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

    @app.get("/v1/jobs")
    def list_jobs(user: dict = Depends(current_user)):
        return service.list_jobs(user)

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, user: dict = Depends(current_user)):
        job = service.get_job(user, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="not found")
        return job

    @app.get("/v1/jobs/{job_id}/clips/{n}.mp4")
    def download_clip(job_id: str, n: int, user: dict = Depends(current_user)):
        path = service.clip_file(user, job_id, n)
        if path is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path, media_type="video/mp4", filename=f"{job_id}_clip_{n:02d}.mp4")

    return app
