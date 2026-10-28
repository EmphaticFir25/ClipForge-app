"""Queue records shared with the pipeline. Platform publishing lives outside the engine."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

PLATFORMS = ("tiktok", "youtube")


@dataclass
class QueueItem:
    clip_path: str
    title: str
    caption: str
    hashtags: list[str]
    platforms: list[str] = field(default_factory=lambda: list(PLATFORMS))
    status: str = "pending"


class ReviewQueue:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[QueueItem]:
        if not self.path.exists():
            return []
        return [QueueItem(**item) for item in json.loads(self.path.read_text(encoding="utf-8"))]

    def save(self, items: list[QueueItem]) -> None:
        self.path.write_text(json.dumps([asdict(i) for i in items], indent=2, ensure_ascii=False),
                             encoding="utf-8")

    def add(self, item: QueueItem) -> None:
        items = self.load()
        items.append(item)
        self.save(items)
