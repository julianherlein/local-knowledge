"""Git for the vault repo (SDD §13): commit only what the engine wrote, never manual edits.

Every write goes through `Vault.write`, which first calls `Tracker.before_write`. That
call does two things, both persisted in the `git.pending` state key before a single
byte hits the disk:

1. Taint check. If the file on disk differs from HEAD and its content is not one the
   engine itself wrote, someone (you, Obsidian, a compile session) has uncommitted
   edits there, so the path is marked *tainted*. The check compares git blob ids
   against HEAD at the moment of the write, not a snapshot from the start of the run,
   so an edit made while the run sits in a slow LLM call is still caught.
2. Intent. The sha256 of the content about to be written is recorded. A crash between
   recording and writing therefore never makes the engine mistake its own file for a
   manual edit on the next run.

At commit time, paths that are tainted or whose content is not engine content are left
out (with a warning) and stay pending; everything else is committed with
`git commit -- <paths>`, which never includes anything else you have staged. A path
leaves the pending set once it matches HEAD again (for example because you committed
it yourself along with your edits). Any git failure raises GitError instead of being
read as "nothing to do", so pending writes are never silently forgotten.
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
MAX_SHAS = 8


class GitError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha(data: bytes) -> str:
    """The object id git gives `data` (no filters; the vault's .gitattributes keeps LF as-is)."""
    return hashlib.sha1(b"blob " + str(len(data)).encode() + bytes(1) + data).hexdigest()


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
    # Surface a broken repo (e.g. "dubious ownership") instead of reading it as "no commits yet".
    run_git(repo, "rev-parse", "--is-inside-work-tree")
    return run_git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0


def head_blobs(repo: Path) -> dict[str, str]:
    """path -> blob id for every file in HEAD (empty for a repo with no commits)."""
    if not has_head(repo):
        return {}
    out = run_git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD").stdout
    blobs: dict[str, str] = {}
    for entry in out.split(chr(0)):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if path and len(parts) == 3 and parts[1] == "blob":
            blobs[path] = parts[2]
    return blobs


def read_head_commit(repo: Path) -> str | None:
    """HEAD's commit id read straight from .git files (no subprocess), or None if unsure.

    None makes callers fall back to asking git, so odd layouts (worktree `.git` files,
    detached states we do not parse) cost speed, never correctness.
    """
    git_dir = repo / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:]
        loose = git_dir / ref
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip() or None
        packed = git_dir / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
        # Unborn branch, or a ref backend we do not parse (reftable): unsure, ask git.
        return None
    except OSError:
        return None


def author_args(repo: Path) -> list[str]:
    """`-c user.name=... -c user.email=...` when the repo has no identity configured."""
    if run_git(repo, "config", "user.email", check=False).stdout.strip():
        return []
    return ["-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}"]


@dataclass
class PendingEntry:
    shas: list[str] = field(default_factory=list)
    """sha256 of every content the engine wrote (or was about to write) at this path."""
    tainted: bool = False
    item_ids: list[int] = field(default_factory=list)


class Tracker:
    """Tracks engine writes for the auto commit. State persists in the DB between runs."""

    def __init__(self, repo: Path, queue: Queue) -> None:
        self.repo = repo
        self.queue = queue
        self.enabled = is_repo(repo)
        self.pending: dict[str, PendingEntry] = {}
        self._head: dict[str, str] = {}
        self._head_ref: str | None = None
        self._prune_clean()

    def _load(self) -> None:
        # The DB is the source of truth: another Tracker in the same process (e.g. the digest
        # refresh inside `kb run`) may have recorded writes since we last looked.
        raw = json.loads(self.queue.get_state(PENDING_KEY) or "{}")
        self.pending = {k: PendingEntry(**v) for k, v in raw.items()}

    def _save(self) -> None:
        self.queue.set_state(PENDING_KEY, json.dumps({k: vars(v) for k, v in self.pending.items()}))

    def _current(self, rel: str) -> bytes | None:
        p = self.repo / rel
        return p.read_bytes() if p.exists() else None

    def _head_moved(self) -> bool:
        current = read_head_commit(self.repo)
        return current is None or current != self._head_ref

    def differs_from_head(self, rel: str) -> bool:
        data = self._current(rel)
        blob = git_blob_sha(data) if data is not None else None
        if self._head.get(rel) == blob:
            return False
        if rel not in self._head and data is not None and not self._head_moved():
            return True  # new file, HEAD unchanged since we read it: no need to ask git per path
        # HEAD may have moved since we read it (you committed mid-run): re-read this one path.
        proc = run_git(self.repo, "rev-parse", "-q", "--verify", f"HEAD:{rel}", check=False)
        fresh = proc.stdout.strip() if proc.returncode == 0 else None
        if fresh:
            self._head[rel] = fresh
        else:
            self._head.pop(rel, None)
        return fresh != blob

    def _prune_clean(self) -> None:
        self._load()
        if not self.enabled:
            return
        self._head_ref = read_head_commit(self.repo)
        self._head = head_blobs(self.repo)
        for rel in list(self.pending):
            if not self.differs_from_head(rel):
                del self.pending[rel]
        self._save()

    def _is_ours(self, rel: str, data: bytes | None) -> bool:
        entry = self.pending.get(rel)
        return entry is not None and not entry.tainted and data is not None and sha256_bytes(data) in entry.shas

    def before_write(self, rel: str, data: bytes, item_id: int | None = None) -> None:
        if not self.enabled:
            return
        self._load()
        current = self._current(rel)
        entry = self.pending.get(rel) or PendingEntry()
        # An untracked empty file (Obsidian's "open today's daily note" creates one) holds no
        # user content, so overwriting and committing it sweeps nothing in.
        empty_placeholder = current is not None and not current.strip() and rel not in self._head
        if (
            not entry.tainted
            and not empty_placeholder
            and not self._is_ours(rel, current)
            and self.differs_from_head(rel)
        ):
            log.warning("manual edits in %s; auto commit leaves it for you to commit", rel)
            entry.tainted = True
        entry.shas = (entry.shas + [sha256_bytes(data)])[-MAX_SHAS:]
        if item_id is not None and item_id not in entry.item_ids:
            entry.item_ids.append(item_id)
        self.pending[rel] = entry
        self._save()

    def blockers(self) -> list[str]:
        return sorted(rel for rel, e in self.pending.items() if e.tainted or not self._is_ours(rel, self._current(rel)))

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
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *author_args(self.repo), "commit", "-q", "-m", message, "--", *paths],
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
