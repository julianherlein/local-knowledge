"""The vault on disk: paths, slugs, and the single write path every automated write goes through."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from slugify import slugify

from .git_ops import Tracker

RAW_DIRS = {"x": "raw/x", "youtube": "raw/youtube", "web": "raw/web"}
SOURCES_DIR = "wiki/sources"
COMPILE_QUEUE = "wiki/_compile-queue.md"
LOG = "wiki/log.md"
DIGEST_DAILY = "digests/daily"
DIGEST_WEEKLY = "digests/weekly"


def make_slug(title: str, max_length: int = 60) -> str:
    s = slugify(title or "", max_length=max_length, word_boundary=True, save_order=True)
    return s or "untitled"


class Vault:
    def __init__(self, root: Path, tracker: Tracker | None = None) -> None:
        self.root = Path(root)
        self.tracker = tracker

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def read(self, rel: str) -> str:
        return self.path(rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str, item_id: int | None = None) -> None:
        """Write with LF endings and UTF-8, tracked for the auto commit."""
        data = text.encode("utf-8")
        if self.tracker:
            self.tracker.before_write(rel)
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if self.tracker:
            self.tracker.after_write(rel, data, item_id)

    def stem_for(self, source_type: str, day: date, title: str, taken: set[str] | None = None) -> str:
        """`<YYYY-MM-DD>-<slug>`, unique across raw/<type>/ and wiki/sources/."""
        base = f"{day.isoformat()}-{make_slug(title)}"
        stem, n = base, 2
        taken = taken or set()
        while (
            stem in taken
            or self.exists(f"{RAW_DIRS[source_type]}/{stem}.md")
            or self.exists(f"{SOURCES_DIR}/{stem}.md")
        ):
            stem = f"{base}-{n}"
            n += 1
        return stem
