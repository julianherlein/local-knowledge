"""Observability: structured JSON-lines log with rotation (SDD §13) and the run lock."""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

_STD = set(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in vars(record).items():
            if k not in _STD and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(log_dir: Path, *, verbose: bool = False) -> None:
    root = logging.getLogger("kb")
    if getattr(root, "_kb_configured", False):
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "kb.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(ch)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False
    root._kb_configured = True  # type: ignore[attr-defined]


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path), timeout=0)
    try:
        lock.acquire()
    except Timeout as e:
        raise AlreadyRunning(f"another kb process holds {path}") from e
    try:
        yield
    finally:
        lock.release()
