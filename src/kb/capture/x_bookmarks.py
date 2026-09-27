"""Capture adapter: X bookmarks (SDD §8.1).

Walks `GET /2/users/{me}/bookmarks` newest-first and enqueues what is new.

Stop conditions, in order:
1. the watermark `x.last_bookmark_id` (the newest bookmark already enqueued). The
   list is ordered by *bookmark* time, not post id, so the check is equality, never
   `id <= watermark`;
2. the first post already in the DB *as a bookmark* (origin `x_bookmark` or `backfill`).
   The list is in bookmark order, so everything below it was bookmarked earlier and is
   either captured or older than what the engine tracks (history comes only from
   `kb backfill-x`). This covers a watermark that was un-bookmarked since the last run
   (the read-later pattern): without it the walk would continue into never-enqueued
   history and import, and pay for, up to ~800 old posts. Items that entered the DB
   another way (Telegram, CLI) do not count: bookmarking posts you had already sent
   from your phone must not end the walk above the genuinely new bookmarks below;
3. `x.max_bookmark_pages` pages (40 x 20 = the API's ~800 bookmark cap);
4. no `next_token`.

The watermark only moves after a walk that ended without error. After a failed walk
(429 mid-way, network error) the items already read are still enqueued, which is safe
because enqueue dedups, and `x.walk_incomplete` is set: the next walk then ignores
stop condition 2, because the fully-known pages at the top are exactly the ones the
failed walk enqueued and stopping there would skip the older, unread bookmarks.

Cost (§12): bookmark reads are billed per post returned, so we request no extra
fields or expansions; the post fetcher reads full content later, once per item.
"""

from __future__ import annotations

from ..queue import Queue, now_iso
from . import CaptureContext, CaptureReport, x_auth
from .x_api import RateLimited, XApiError, XClient

SOURCE = "x_bookmarks"
PAGE_SIZE = 20
INCOMPLETE_KEY = "x.walk_incomplete"
LAST_POLL_KEY = "x.last_poll_at"


def status_url(post_id: str) -> str:
    return f"https://x.com/i/status/{post_id}"


BOOKMARK_ORIGINS = ("x_bookmark", "backfill")


def known_bookmarks(q: Queue, ids: list[str]) -> set[str]:
    """The ids already in the DB with a bookmark origin (see stop condition 2)."""
    if not ids:
        return set()
    urls = [status_url(i) for i in ids]
    rows = q.conn.execute(
        f"SELECT canonical_url FROM items WHERE origin IN (?, ?) AND canonical_url IN ({','.join('?' * len(urls))})",
        (*BOOKMARK_ORIGINS, *urls),
    ).fetchall()
    return {r["canonical_url"].rsplit("/", 1)[-1] for r in rows}


def _skip(message: str) -> CaptureReport:
    return CaptureReport(source=SOURCE, skipped=True, message=message)


def _user_id(ctx: CaptureContext, client: XClient) -> str:
    uid = ctx.queue.get_state(x_auth.USER_ID_KEY)
    if uid:
        return uid
    data = client.get("/users/me").get("data")
    data = data if isinstance(data, dict) else {}
    if not data.get("id"):
        raise XApiError("/2/users/me returned no user id")
    x_auth.remember_user(ctx.queue, str(data["id"]), str(data.get("username") or ""))
    return str(data["id"])


def poll(ctx: CaptureContext) -> CaptureReport:
    s, q = ctx.settings, ctx.queue
    if not s.x.enabled:
        return _skip("disabled in config")
    if not s.x_client_id:
        return _skip("X_CLIENT_ID not set in ~/.kb/.env")
    if not x_auth.is_authorized(s, q):
        return _skip("not authorized; run `kb auth x`")

    rep = CaptureReport(source=SOURCE)
    client = XClient(s, q, ctx.http)
    watermark = q.get_state(x_auth.WATERMARK_KEY)
    first_run = watermark is None
    trust_known_pages = q.get_state(INCOMPLETE_KEY) != "1"

    new_ids: list[str] = []  # newest-first, as read
    newest: str | None = None
    complete = False
    stop_reason = ""
    token: str | None = None
    pages = 0
    try:
        uid = _user_id(ctx, client)
        while pages < s.x.max_bookmark_pages:
            params: dict[str, str | int] = {"max_results": PAGE_SIZE}
            if token:
                params["pagination_token"] = token
            page = client.get(f"/users/{uid}/bookmarks", params)
            pages += 1
            rows = page.get("data") if isinstance(page.get("data"), list) else []
            ids = [str(t["id"]) for t in rows if isinstance(t, dict) and isinstance(t.get("id"), str | int)]
            rep.seen += len(ids)
            if newest is None and ids:
                newest = ids[0]
            known = known_bookmarks(q, ids) if trust_known_pages else set()
            for pid in ids:
                if pid == watermark:
                    stop_reason = "watermark"
                    break
                if pid in known:
                    stop_reason = "known_bookmark"
                    break
                new_ids.append(pid)
            if stop_reason:
                break
            if first_run:
                stop_reason = "first_run"
                break
            meta = page.get("meta") if isinstance(page.get("meta"), dict) else {}
            token = meta.get("next_token") if isinstance(meta.get("next_token"), str) else None
            if not token:
                stop_reason = "end"
                break
        else:
            stop_reason = "page_cap"
        complete = True
    except RateLimited as e:
        rep.errors.append(str(e))
    except XApiError as e:
        if e.status == 401:
            rep.errors.append(f"X returned 401 after a token refresh; {x_auth.REAUTH} ({e})")
        else:
            rep.errors.append(str(e))
    except x_auth.XAuthError as e:
        rep.errors.append(str(e))

    # Oldest first, so item ids ascend with bookmark time.
    for pid in reversed(new_ids):
        res = q.enqueue(status_url(pid), "x_bookmark")
        if res.status == "queued":
            rep.enqueued += 1
        elif res.status == "duplicate":
            rep.duplicates += 1

    if complete:
        if newest:
            q.set_state(x_auth.WATERMARK_KEY, newest)
        q.set_state(INCOMPLETE_KEY, None)
        q.set_state(LAST_POLL_KEY, now_iso())
    elif new_ids:
        q.set_state(INCOMPLETE_KEY, "1")

    notes = []
    if first_run and complete:
        notes.append("first run: queued the latest page only; import older bookmarks with `kb backfill-x`")
    if stop_reason == "page_cap":
        notes.append(f"stopped at the {s.x.max_bookmark_pages}-page cap")
    notes.append(f"{client.requests} API request{'s' if client.requests != 1 else ''}")
    rep.message = "; ".join(notes)
    return rep
