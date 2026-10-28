"""ASGI entry point for uvicorn:  uvicorn webapp.asgi:app"""
from .api import create_app
from .main import build_service

service, _ = build_service()
app = create_app(service)
