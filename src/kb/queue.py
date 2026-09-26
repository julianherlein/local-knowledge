"""SQLite queue and state (SDD §6).

Status machine:

    queued -> fetched -> tagged -> written -> summarized -> done
       \\________\\_________\\_________\\____________\\-> failed (attempts < max, retried next run)
                                                     failed_permanent (attempts >= max, or 404/paywall)

`status` is always the last *completed* step, or a failure status. On failure the
last completed step is kept in `resume_from`, so a retry resumes exactly there.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .normalize import InvalidURL, normalize

STEPS = ["queued", "fetched", "tagged", "written", "summarized", "done"]
FAILED = "failed"
FAILED_PERMANENT = "failed_permanent"
STATUSES = STEPS + [FAILED, FAILED_PERMANENT]
ORIGINS = {"x_bookmark", "telegram", "cli", "backfill"}

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY,
    url             TEXT NOT NULL,
    canonical_url   TEXT NOT NULL UNIQUE,
    source_type     TEXT NOT NULL CHECK (source_type IN ('x','youtube','web')),
    origin          TEXT NOT NULL,
    hint_tag        TEXT,
    note            TEXT,
    inline_text     TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    resume_from     TEXT,
    domains         TEXT,
    tag_method      TEXT,
    tag_confidence  REAL,
    tag_reason      TEXT,
    language        TEXT,
    title           TEXT,
    author          TEXT,
    published       TEXT,
    raw_path        TEXT,
    summary_path    TEXT,
    tldr            TEXT,
    content_hash    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    error_reason    TEXT,
    captured_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    done_at         TEXT
);
CREATE INDEX IF NOT EXISTS items_status ON items(status);

CREATE TABLE IF NOT EXISTS fetched (
    item_id  INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    payload  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER,
    purpose       TEXT NOT NULL,
    model         TEXT,
    cost_usd      REAL,
    duration_ms   INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    captured     INTEGER DEFAULT 0,
    processed    INTEGER DEFAULT 0,
    done         INTEGER DEFAULT 0,
    failed       INTEGER DEFAULT 0,
    committed    INTEGER DEFAULT 0,
    notes        TEXT
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.astimezone()


@dataclass
class Item:
    id: int
    url: str
    canonical_url: str
    source_type: str
    origin: str
    hint_tag: str | None
    note: str | None
    inline_text: str | None
    status: str
    resume_from: str | None
    domains: list[str]
    tag_method: str | None
    tag_confidence: float | None
    tag_reason: str | None
    language: str | None
    title: str | None
    author: str | None
    published: str | None
    raw_path: str | None
    summary_path: str | None
    tldr: str | None
    content_hash: str | None
    attempts: int
    last_error: str | None
    error_reason: str | None
    captured_at: str
    updated_at: str
    done_at: str | None

    @property
    def hint_tags(self) -> list[str]:
        return [t for t in (self.hint_tag or "").split() if t]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Item:
        d = dict(row)
        d["domains"] = json.loads(d["domains"]) if d["domains"] else []
        return cls(**d)


@dataclass
class EnqueueResult:
    status: str  # queued | duplicate | invalid
    item_id: int | None
    canonical_url: str | None = None
    message: str = ""


class Queue:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            self.conn.executescript(_SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # Capture ----------------------------------------------------------------------
    def enqueue(
        self,
        url: str,
        origin: str,
        hint_tags: list[str] | None = None,
        note: str | None = None,
        inline_text: str | None = None,
    ) -> EnqueueResult:
        if origin not in ORIGINS:
            raise ValueError(f"unknown origin {origin!r}")
        try:
            n = normalize(url)
        except InvalidURL:
            return EnqueueResult("invalid", None, message=f"not a valid URL: {url}")
        tags = " ".join(t.lstrip("#").lower() for t in (hint_tags or []) if t.strip("#")) or None
        ts = now_iso()
        with self.tx() as c:
            row = c.execute("SELECT id, origin, note FROM items WHERE canonical_url = ?", (n.canonical_url,)).fetchone()
            if row:
                extra = []
                if row["origin"] != origin:
                    extra.append(f"also captured via {origin} on {ts[:10]}")
                if tags:
                    extra.append(" ".join(f"#{t}" for t in tags.split()))
                if note:
                    extra.append(note)
                if extra and row["origin"] != origin:
                    merged = "; ".join(filter(None, [row["note"], " ".join(extra)]))
                    c.execute("UPDATE items SET note = ?, updated_at = ? WHERE id = ?", (merged, ts, row["id"]))
                return EnqueueResult("duplicate", row["id"], n.canonical_url, "already captured")
            cur = c.execute(
                """INSERT INTO items (url, canonical_url, source_type, origin, hint_tag, note, inline_text,
                                      status, captured_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (url, n.canonical_url, n.source_type, origin, tags, note, inline_text, ts, ts),
            )
            return EnqueueResult("queued", cur.lastrowid, n.canonical_url, "queued")

    # Processing -------------------------------------------------------------------
    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return Item.from_row(row) if row else None

    def pending(self, limit: int | None, max_attempts: int) -> list[Item]:
        """Items to work on this run: unfinished steps first, then retryable failures."""
        rows = self.conn.execute(
            f"""SELECT * FROM items
                WHERE status IN ({",".join("?" * 5)})
                   OR (status = ? AND attempts < ?)
                ORDER BY CASE WHEN status = ? THEN 1 ELSE 0 END, id
                {"LIMIT ?" if limit else ""}""",
            (*STEPS[:5], FAILED, max_attempts, FAILED, *([limit] if limit else [])),
        ).fetchall()
        return [Item.from_row(r) for r in rows]

    def update(self, item_id: int, **fields: Any) -> None:
        if "domains" in fields and fields["domains"] is not None:
            fields["domains"] = json.dumps(fields["domains"])
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))

    def advance(self, item_id: int, status: str, **fields: Any) -> None:
        if status not in STEPS:
            raise ValueError(status)
        if status == "done":
            fields.setdefault("done_at", now_iso())
        self.update(item_id, status=status, resume_from=None, last_error=None, error_reason=None, **fields)

    def resume(self, item: Item) -> Item:
        """Move a `failed` item back to its last completed step."""
        if item.status == FAILED:
            step = item.resume_from if item.resume_from in STEPS else "queued"
            self.update(item.id, status=step)
            item.status = step
        return item

    def fail(self, item_id: int, error: str, *, permanent: bool, reason: str | None, max_attempts: int) -> str:
        item = self.get(item_id)
        assert item is not None
        attempts = item.attempts + 1
        resume_from = item.status if item.status in STEPS else item.resume_from
        status = FAILED_PERMANENT if permanent or attempts >= max_attempts else FAILED
        self.update(
            item_id,
            status=status,
            resume_from=resume_from,
            attempts=attempts,
            last_error=error[:2000],
            error_reason=reason,
        )
        return status

    def retry(self, item_id: int | None = None) -> int:
        """Reset failed items (one, or all) so the next run picks them up again."""
        where = "status IN (?, ?)" + (" AND id = ?" if item_id is not None else "")
        args: tuple[Any, ...] = (FAILED, FAILED_PERMANENT) + ((item_id,) if item_id is not None else ())
        cur = self.conn.execute(
            f"UPDATE items SET status = ?, attempts = 0, updated_at = ? WHERE {where}", (FAILED, now_iso(), *args)
        )
        return cur.rowcount

    # Fetched payload (persisted between steps so a crash resumes) -------------------
    def save_payload(self, item_id: int, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO fetched (item_id, payload) VALUES (?, ?) ON CONFLICT(item_id) DO UPDATE SET payload = excluded.payload",
            (item_id, json.dumps(payload, ensure_ascii=False, default=str)),
        )

    def load_payload(self, item_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload FROM fetched WHERE item_id = ?", (item_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def drop_payload(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM fetched WHERE item_id = ?", (item_id,))

    # State ------------------------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM state WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # Observability ----------------------------------------------------------------
    def record_llm_call(self, item_id: int | None, purpose: str, resp: Any) -> None:
        self.conn.execute(
            """INSERT INTO llm_calls (item_id, purpose, model, cost_usd, duration_ms, input_tokens, output_tokens, at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                purpose,
                resp.model,
                resp.cost_usd,
                resp.duration_ms,
                resp.input_tokens,
                resp.output_tokens,
                now_iso(),
            ),
        )

    def start_run(self) -> int:
        return self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now_iso(),)).lastrowid

    def finish_run(self, run_id: int, **fields: Any) -> None:
        fields["finished_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()
        out = {s: 0 for s in STATUSES}
        out.update({r["status"]: r["n"] for r in rows})
        return out

    def recent_errors(self, limit: int = 10) -> list[Item]:
        rows = self.conn.execute(
            # julianday() parses the UTC offset, so ordering survives DST and timezone changes.
            "SELECT * FROM items WHERE status IN (?, ?) ORDER BY julianday(updated_at) DESC, id DESC LIMIT ?",
            (FAILED, FAILED_PERMANENT, limit),
        ).fetchall()
        return [Item.from_row(r) for r in rows]

    def all_items(self) -> list[Item]:
        return [Item.from_row(r) for r in self.conn.execute("SELECT * FROM items ORDER BY id").fetchall()]

    def llm_cost_since(self, iso: str) -> tuple[int, float]:
        row = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost_usd), 0) c FROM llm_calls WHERE julianday(at) >= julianday(?)",
            (iso,),
        ).fetchone()
        return row["n"], row["c"]

    def taken_stems(self) -> set[str]:
        """Every file stem already assigned to an item, written to disk or not."""
        rows = self.conn.execute(
            "SELECT raw_path, summary_path FROM items WHERE raw_path IS NOT NULL OR summary_path IS NOT NULL"
        ).fetchall()
        return {p.rsplit("/", 1)[-1].removesuffix(".md") for r in rows for p in (r["raw_path"], r["summary_path"]) if p}

    def known_x_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        urls = [f"https://x.com/i/status/{i}" for i in ids]
        rows = self.conn.execute(
            f"SELECT canonical_url FROM items WHERE canonical_url IN ({','.join('?' * len(urls))})", urls
        ).fetchall()
        return {r["canonical_url"].rsplit("/", 1)[-1] for r in rows}
