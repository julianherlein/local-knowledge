"""Git for the vault repo (SDD §13): commit only what the engine wrote, never manual edits.

The engine records every path it writes, with the sha256 of what it wrote, in the
`git.pending` state key. Before it writes a path it asks git whether the file
already differs from HEAD for reasons that are not the engine's own earlier write.
If so, the path is *tainted*: someone (you, Obsidian, a compile session) has
uncommitted edits there, and committing it would sweep those edits into an
`auto:` commit. Tainted or since-modified paths are left out of the commit with a
warning (the rest is committed); they stay pending for the next run, and a path
drops out of the pending set as soon as it is clean against HEAD (for example
because you committed it yourself along with your edits).
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .queue import Queue

log = logging.getLogger("kb.git")
PENDING_KEY = "git.pending"
AUTHOR = ("kb-engine", "kb-engine@localhost")


class GitError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def is_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def init_repo(repo: Path) -> None:
    if not is_repo(repo):
        run_git(repo, "init", "-q", "-b", "main")


def has_head(repo: Path) -> bool:
    return run_git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0


def dirty_paths(repo: Path) -> set[str]:
    """Every path that differs from HEAD (modified, staged, deleted, or untracked), in one git call."""
    out = run_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", check=False).stdout
    paths: set[str] = set()
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        if len(entry) > 3:
            paths.add(entry[3:])
            if entry[0] in "RC":  # rename/copy: the next field is the old path
                i += 1
                if i < len(fields) and fields[i]:
                    paths.add(fields[i])
        i += 1
    return paths


@dataclass
class PendingEntry:
    sha: str
    tainted: bool = False
    item_ids: list[int] = field(default_factory=list)


class Tracker:
    """Tracks engine writes for the auto commit. One per run; state persists in the DB."""

    def __init__(self, repo: Path, queue: Queue) -> None:
        self.repo = repo
        self.queue = queue
        self.enabled = is_repo(repo)
        raw = json.loads(queue.get_state(PENDING_KEY) or "{}")
        self.pending: dict[str, PendingEntry] = {k: PendingEntry(**v) for k, v in raw.items()}
        # Snapshot of paths that differ from HEAD before this run writes anything. Only the
        # engine writes during a run, so the snapshot stays valid for before_write checks.
        self._dirty: set[str] = set()
        self._prune_clean()

    def _save(self) -> None:
        self.queue.set_state(PENDING_KEY, json.dumps({k: vars(v) for k, v in self.pending.items()}))

    def _prune_clean(self) -> None:
        if not self.enabled:
            return
        self._dirty = dirty_paths(self.repo)
        for rel in list(self.pending):
            if rel not in self._dirty:
                del self.pending[rel]
        self._save()

    def before_write(self, rel: str) -> None:
        if not self.enabled:
            return
        entry = self.pending.get(rel)
        path = self.repo / rel
        current = sha256_bytes(path.read_bytes()) if path.exists() else None
        ours = entry is not None and not entry.tainted and entry.sha == current
        if not ours and rel in self._dirty:
            log.warning("manual edits in %s; auto commit will skip until they are committed", rel)
            self.pending[rel] = PendingEntry(sha=current or "", tainted=True, item_ids=entry.item_ids if entry else [])
            self._save()

    def after_write(self, rel: str, data: bytes, item_id: int | None) -> None:
        if not self.enabled:
            return
        entry = self.pending.get(rel) or PendingEntry(sha="")
        entry.sha = sha256_bytes(data)
        if item_id is not None and item_id not in entry.item_ids:
            entry.item_ids.append(item_id)
        self.pending[rel] = entry
        self._save()

    def blockers(self) -> list[str]:
        out = []
        for rel, e in self.pending.items():
            path = self.repo / rel
            current = sha256_bytes(path.read_bytes()) if path.exists() else ""
            if e.tainted or current != e.sha:
                out.append(rel)
        return sorted(out)

    def commit(self, prefix: str = "auto: ingest", message: str | None = None) -> tuple[bool, str]:
        """Commit every pending path that holds only engine writes. Returns (committed, message).

        Paths with manual edits stay pending and are named in the message; everything
        else is committed, so one hand-edited file never holds back the whole ingest.
        `message` replaces the default `<prefix> N items [ids]` subject (e.g. for digests).
        """
        if not self.enabled:
            return False, "vault is not a git repo"
        self._prune_clean()
        if not self.pending:
            return False, "nothing to commit"
        blocked = set(self.blockers())
        if blocked:
            log.warning("auto commit skips paths with manual edits: %s", ", ".join(sorted(blocked)))
        paths = sorted(p for p in self.pending if p not in blocked)
        if not paths:
            return False, "skipped auto commit: manual edits in " + ", ".join(sorted(blocked))
        ids = sorted({i for p in paths for i in self.pending[p].item_ids})
        if message is None:
            message = f"{prefix} {len(ids)} items [{', '.join(map(str, ids))}]" if ids else "auto: update vault"
        elif ids:
            message += f" (+ items [{', '.join(map(str, ids))}])"
        run_git(self.repo, "add", "--", *paths)
        env_author = (
            [
                "-c",
                f"user.name={AUTHOR[0]}",
                "-c",
                f"user.email={AUTHOR[1]}",
            ]
            if not run_git(self.repo, "config", "user.email", check=False).stdout.strip()
            else []
        )
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *env_author, "commit", "-q", "-m", message, "--", *paths],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise GitError(f"git commit failed: {proc.stderr.strip() or proc.stdout.strip()}")
        for p in paths:
            del self.pending[p]
        self._save()
        if blocked:
            message += " (left uncommitted, manual edits: " + ", ".join(sorted(blocked)) + ")"
        return True, message
