"""`kb backfill-x`: import a twitter-web-exporter JSON dump of bookmarks (SDD §8.1).

The API returns only the ~800 most recent bookmarks, so history comes from an export
made in the browser with https://github.com/prinsss/twitter-web-exporter
(Bookmarks module -> Export Data -> JSON; tick "Include all metadata" for expanded
links and quoted-post text).

Each record is stored whole in `inline_text`, so the X fetcher can render it without
an API call (`x.backfill_fetch_via_api = false`, the default, costs nothing).

Accepted shapes: a JSON array of records, an object wrapping one (e.g. `{"data": [...]}`),
or JSON Lines. Records are enqueued oldest first (by the id's embedded timestamp, which
is the post's creation time), so item ids follow post age. A record without a usable id
is reported and skipped; one bad record never aborts the import.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..fetchers.x import parse_created, snowflake_time
from . import CaptureContext, CaptureReport

SOURCE = "backfill"
MAX_ERROR_LINES = 20
_ID = re.compile(r"^\d{1,20}$")
_URL_ID = re.compile(r"/status(?:es)?/(\d+)")


def load_records(text: str) -> list[Any]:
    """Records from an array, a wrapping object, or JSON Lines. Raises ValueError."""
    text = text.lstrip("﻿").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        records = []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"not JSON or JSON Lines (line {n}: {e.msg})") from e
        return records
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "tweets", "bookmarks", "items", "records"):
            if isinstance(data.get(key), list):
                return data[key]
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        if "id" in data or "full_text" in data:
            return [data]
    raise ValueError("expected a JSON array of posts (twitter-web-exporter JSON export)")


def record_id(rec: Any) -> str | None:
    if not isinstance(rec, dict):
        return None
    for key in ("id", "rest_id", "id_str"):
        v = rec.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        if isinstance(v, str) and _ID.match(v.strip()):
            return v.strip()
    url = rec.get("url")
    m = _URL_ID.search(url) if isinstance(url, str) else None
    return m.group(1) if m else None


def _sort_key(pid: str, rec: dict[str, Any]) -> tuple[float, int]:
    ts = snowflake_time(pid) or parse_created(rec.get("created_at"))
    return (ts.timestamp() if ts else 0.0, int(pid))


def import_export(path: Path, ctx: CaptureContext) -> CaptureReport:
    rep = CaptureReport(source=SOURCE)
    path = Path(path).expanduser()
    try:
        records = load_records(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        rep.errors.append(f"cannot read {path}: {e.strerror or e}")
        return rep
    except (ValueError, UnicodeDecodeError) as e:
        rep.errors.append(f"{path.name}: {e}")
        return rep

    valid: list[tuple[str, dict[str, Any]]] = []
    bad: list[str] = []
    for n, rec in enumerate(records, 1):
        pid = record_id(rec)
        if pid is None:
            what = "not an object" if not isinstance(rec, dict) else "no post id"
            bad.append(f"record {n}: {what}; skipped")
            continue
        valid.append((pid, rec))
    valid.sort(key=lambda pr: _sort_key(*pr))

    for pid, rec in valid:
        rec = {**rec, "id": pid}
        res = ctx.queue.enqueue(
            f"https://x.com/i/status/{pid}", "backfill", inline_text=json.dumps(rec, ensure_ascii=False)
        )
        rep.seen += 1
        if res.status == "queued":
            rep.enqueued += 1
        elif res.status == "duplicate":
            rep.duplicates += 1
        else:
            bad.append(f"post {pid}: {res.message}")

    rep.errors.extend(bad[:MAX_ERROR_LINES])
    if len(bad) > MAX_ERROR_LINES:
        rep.errors.append(f"... and {len(bad) - MAX_ERROR_LINES} more malformed records")
    rep.message = f"{len(records)} records in {path.name}"
    return rep
