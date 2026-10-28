"""Wiring: where data lives and how the service is built from the environment."""
from __future__ import annotations

import os
from pathlib import Path

from .service import Service
from .store import Store


def data_dir() -> Path:
    return Path(os.environ.get("CLIPFORGE_APP_DATA", "data")).resolve()


def build_service() -> tuple[Service, Path]:
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "app.sqlite3")
    return Service(store, root), root


def watermark_path(root: Path) -> Path:
    """Logo overlaid on free-tier clips. Replace assets/watermark.png with your real logo."""
    return Path(os.environ.get("CLIPFORGE_WATERMARK", root / "assets" / "watermark.png"))
