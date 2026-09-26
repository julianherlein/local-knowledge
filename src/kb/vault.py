"""The vault on disk: paths, slugs, and the single write path every automated write goes through."""

from __future__ import annotations

import os
import time
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


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file + os.replace, so a crash never leaves a truncated file.

    Windows can briefly refuse the replace while an indexer or antivirus holds the
    target open, so a PermissionError is retried a few times before giving up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.kb-tmp")
    tmp.write_bytes(data)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.2 * (attempt + 1))


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
        """Write UTF-8 with LF endings. The tracker records the intent before the bytes land."""
        data = text.encode("utf-8", errors="replace")  # lone surrogates from bad decoding
        if self.tracker:
            self.tracker.before_write(rel, data, item_id)
        atomic_write(self.path(rel), data)

    def stem_for(self, source_type: str, day: date, title: str, taken: set[str] | None = None) -> str:
        """`<YYYY-MM-DD>-<slug>`, unique across raw/<type>/, wiki/sources/ and `taken`.

        `taken` must hold every stem already assigned in the DB: a file that failed to
        write still owns its name, or a later item could claim it and be overwritten
        when the first one is retried.
        """
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
