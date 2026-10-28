"""ASGI entry point for uvicorn:  uvicorn webapp.asgi:app"""
from .api import create_app
from .main import build_service, configure_logging
from .security import SecurityMiddleware

configure_logging()
service, _ = build_service()
app = SecurityMiddleware(create_app(service))
