"""Fetcher for X posts, with self-thread unrolling (SDD §7.2).

Two paths:

- Backfill items (`origin == "backfill"`) carry the twitter-web-exporter record in
  `inline_text`. By default they render from it with no X API call (free); the only
  network use is resolving bare t.co links through t.co itself when the export has no
  entity data. `x.backfill_fetch_via_api = true` sends them down the API path instead.
- Everything else: `GET /2/tweets/:id`, then, for a thread, recent search.

Thread rules. A post belongs to a self-thread when it is the conversation root or a
reply by the author to themselves (then the root is fetched too). Recent search only
covers 7 days, so:

    root younger than 7 days  -> search `conversation_id:<cid> from:<user>`,
                                 keep the author's self-reply chain -> thread: complete
    root older, or unknown    -> the single post                    -> thread: incomplete
    reply to someone else     -> the single post                    -> thread: None

"incomplete" therefore means "could not be verified", not "known to have more posts".
The chain is built by following `replied_to` links from the root, which drops the
author's answers to other people (and the author's follow-ups under those answers).
A post whose parent is missing from the results (deleted) but that replies to the
author themselves is kept, so one deleted post does not cut the rest of a thread.

Wire names: requests use the long-standing `tweet.fields` / `referenced_tweets`
names from the data dictionary. The 2026 OpenAPI spec also shows `note_post`,
`referenced_posts` and `includes.posts`; responses are parsed tolerantly for both.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..capture import x_auth
from ..capture.x_api import RateLimited, XApiError, XClient, format_reset
from ..models import FetchedItem, FetchError
from ..normalize import URL_RE, X_HOSTS, InvalidURL, normalize
from ..queue import Item
from ..textutil import one_line

TWEET_FIELDS = (
    "created_at,conversation_id,note_tweet,entities,referenced_tweets,author_id,attachments,in_reply_to_user_id,lang"
)
EXPANSIONS = "author_id,attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id"
LOOKUP_PARAMS = {
    "tweet.fields": TWEET_FIELDS,
    "expansions": EXPANSIONS,
    "user.fields": "username,name",
    "media.fields": "type,url,preview_image_url,alt_text",
}
SEARCH_WINDOW = timedelta(days=7) - timedelta(minutes=10)  # margin for clock skew and fetch latency
SEARCH_MAX_PAGES = 5
TCO = re.compile(r"https?://t\.co/[A-Za-z0-9]+(?![A-Za-z0-9])")
_TRAILING_TCO = re.compile(r"(https?://t\.co/[A-Za-z0-9]+)\s*$")
TCO_RESOLVE_LIMIT = 10
_X_MEDIA_PATH = re.compile(r"/(?:photo|video)/\d+/?$")
_STATUS_ID = re.compile(r"/status(?:es)?/(\d+)")
# Legacy/odd X language codes -> ISO 639-1. Codes starting with q (qme, qht, ...) are X's
# "media only"/"hashtags only" markers and are dropped with und/zxx.
_LANG_MAP = {"in": "id", "iw": "he", "ji": "yi", "fil": "tl", "nb": "no"}
_TWITTER_EPOCH_MS = 1288834974657


def _now() -> datetime:
    return datetime.now(UTC)


# Small pure helpers --------------------------------------------------------------------
def iso_lang(code: str | None) -> str | None:
    if not code:
        return None
    c = code.strip().lower().split("-", 1)[0]
    c = _LANG_MAP.get(c, c)
    if c in ("und", "zxx") or c.startswith("q") and len(c) == 3:
        return None
    return c if len(c) == 2 and c.isalpha() else None


def snowflake_time(post_id: str) -> datetime | None:
    """Creation time encoded in a post id (ids after Nov 2010)."""
    try:
        n = int(post_id)
    except (TypeError, ValueError):
        return None
    if n < (1 << 32):  # pre-snowflake ids carry no timestamp
        return None
    return datetime.fromtimestamp(((n >> 22) + _TWITTER_EPOCH_MS) / 1000, tz=UTC)


def parse_created(value: Any) -> datetime | None:
    """API ISO dates, legacy `Wed Oct 10 20:19:24 +0000 2018`, and the exporter's
    default `YYYY-MM-DD HH:mm:ss +08:00` (dayjs `Z`), plus a few tolerant variants."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is None:
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M %z", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_x_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host in X_HOSTS or host == "t.co" or host.endswith(".twimg.com") or host == "pic.x.com"


def status_link(username: str | None, post_id: str) -> str:
    return f"https://x.com/{username}/status/{post_id}" if username else f"https://x.com/i/status/{post_id}"


def quote_block(handle: str | None, text: str, link: str) -> str:
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    out = []
    if lines:
        lead = f"**@{handle}:** " if handle else ""
        out.append(f"> {lead}{lines[0]}".rstrip())
        out.extend(f"> {ln}".rstrip() for ln in lines[1:])
    out.append(f"> [quoted post]({link})")
    return "\n".join(out)


# Parsed post ---------------------------------------------------------------------------
@dataclass
class Media:
    type: str
    url: str | None
    alt: str | None = None


@dataclass
class Post:
    id: str
    text: str
    author_id: str | None = None
    username: str | None = None
    name: str | None = None
    created_at: datetime | None = None
    conversation_id: str | None = None
    in_reply_to_user_id: str | None = None
    replied_to: str | None = None
    quoted_id: str | None = None
    lang: str | None = None
    url_entities: list[dict[str, Any]] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)
    quoted: Post | None = None

    @property
    def link(self) -> str:
        return status_link(self.username, self.id)


@dataclass
class Rendered:
    text: str
    outlinks: list[str]


def expand_text(
    text: str,
    url_entities: list[dict[str, Any]],
    *,
    drop_urls: set[str] | None = None,
    quoted_id: str | None = None,
    has_attachments: bool = False,
) -> Rendered:
    """Replace t.co links by their targets; drop the ones that point to attached media or
    the quoted post. Returns the text and the expanded non-X links (in order)."""
    drop = set(drop_urls or ())
    repl: dict[str, str] = {}
    for e in url_entities:
        short = e.get("url")
        if not short:
            continue
        target = e.get("unwound_url") or e.get("expanded_url") or ""
        sid = _STATUS_ID.search(target)
        if e.get("media_key") or (target and is_x_url(target) and _X_MEDIA_PATH.search(urlsplit(target).path)):
            drop.add(short)
        elif quoted_id and sid and sid.group(1) == quoted_id:
            drop.add(short)
        elif target:
            repl[short] = target
    trailing = _TRAILING_TCO.search(text)
    if has_attachments and trailing and trailing.group(1) not in repl:
        drop.add(trailing.group(1))  # an unexplained last t.co on a media/quote post is that link

    def swap(m: re.Match[str]) -> str:
        link = m.group(0)
        return "" if link in drop else repl.get(link, link)

    out = TCO.sub(swap, text)  # whole tokens only: t.co/abc never matches inside t.co/abcd
    outlinks: list[str] = []
    for e in url_entities:  # entity order == text order
        target = repl.get(e.get("url") or "")
        if target and not is_x_url(target) and target not in outlinks:
            outlinks.append(target)
    out = html.unescape(out)
    out = "\n".join(ln.rstrip() for ln in out.strip().splitlines())
    return Rendered(out, outlinks)


def render_post(p: Post) -> Rendered:
    r = expand_text(
        p.text,
        p.url_entities,
        quoted_id=p.quoted_id,
        has_attachments=bool(p.media) or bool(p.quoted_id),
    )
    parts = [r.text] if r.text else []
    media_lines = []
    for m in p.media:
        if m.type == "photo" and m.url:
            media_lines.append(f"![{(m.alt or '').replace(']', ')')}]({m.url})")
        elif m.type in ("video", "animated_gif"):
            media_lines.append(f"[{'GIF' if m.type == 'animated_gif' else 'video'}]({p.link})")
    if media_lines:
        parts.append("\n".join(media_lines))
    if p.quoted_id:
        if p.quoted:
            qr = expand_text(
                p.quoted.text,
                p.quoted.url_entities,
                quoted_id=p.quoted.quoted_id,
                has_attachments=bool(p.quoted.media) or bool(p.quoted.quoted_id),
            )
            parts.append(quote_block(p.quoted.username, qr.text, p.quoted.link))
        else:
            parts.append(quote_block(None, "", status_link(None, p.quoted_id)))
    return Rendered("\n\n".join(parts), r.outlinks)


def title_line(rendered_text: str) -> str:
    """First line of prose: no media or quote lines, no URLs (they make poor titles)."""
    for ln in rendered_text.splitlines():
        if ln.startswith((">", "![", "[video](", "[GIF](")):
            continue
        prose = " ".join(URL_RE.sub(" ", ln).split()).rstrip(":")
        if prose:
            return prose
    return ""


def build_item(posts: list[Post], *, url_post: Post, thread: str | None) -> FetchedItem:
    first = posts[0]
    rendered = [render_post(p) for p in posts]
    handle = first.username or ""
    who = f"{first.name} (@{handle})" if first.name and handle else (f"@{handle}" if handle else None)
    day = first.created_at.date().isoformat() if first.created_at else ""
    header = f"**{who or 'unknown author'}**" + (f" · {day}" if day else "") + f" · [post]({first.link})"
    body = header + "\n\n" + "\n\n---\n\n".join(r.text for r in rendered if r.text)
    outlinks: list[str] = []
    seen: set[str] = set()
    for r in rendered:
        for u in r.outlinks:
            try:
                key = normalize(u).canonical_url
            except InvalidURL:
                key = u
            if key not in seen:
                seen.add(key)
                outlinks.append(u)
    title_text = one_line(title_line(rendered[0].text), 80) or f"post {first.id}"
    return FetchedItem(
        source_type="x",
        url=url_post.link,
        title=f"@{handle}: {title_text}" if handle else title_text,
        body=body.rstrip() + "\n",
        author=who,
        published=first.created_at.date() if first.created_at else None,
        language=iso_lang(first.lang),
        outlinks=outlinks,
        thread=thread,
        extra={"x_id": url_post.id, "author_handle": handle or None, "thread_posts": len(posts)},
    )


# API response parsing -------------------------------------------------------------------
def _includes(resp: dict[str, Any]) -> dict[str, Any]:
    inc = resp.get("includes") or {}
    return {
        "users": {u["id"]: u for u in inc.get("users") or [] if u.get("id")},
        "media": {m["media_key"]: m for m in inc.get("media") or [] if m.get("media_key")},
        "tweets": {t["id"]: t for t in (inc.get("tweets") or []) + (inc.get("posts") or []) if t.get("id")},
    }


def parse_api_post(t: dict[str, Any], inc: dict[str, Any], *, depth: int = 0) -> Post:
    note = t.get("note_tweet") or t.get("note_post") or {}
    if note.get("text"):
        text = note["text"]
        entities = note.get("entities") or {}
    else:
        text = t.get("text") or ""
        entities = t.get("entities") or {}
    refs = t.get("referenced_tweets") or t.get("referenced_posts") or []
    replied = next((r.get("id") for r in refs if r.get("type") == "replied_to"), None)
    quoted = next((r.get("id") for r in refs if r.get("type") == "quoted"), None)
    user = inc["users"].get(t.get("author_id") or "", {})
    media = []
    for key in (t.get("attachments") or {}).get("media_keys") or []:
        m = inc["media"].get(key)
        if m:
            media.append(Media(m.get("type", "photo"), m.get("url") or m.get("preview_image_url"), m.get("alt_text")))
    p = Post(
        id=str(t["id"]),
        text=text,
        author_id=t.get("author_id"),
        username=user.get("username"),
        name=user.get("name"),
        created_at=parse_created(t.get("created_at")),
        conversation_id=t.get("conversation_id"),
        in_reply_to_user_id=t.get("in_reply_to_user_id"),
        replied_to=replied,
        quoted_id=quoted,
        lang=t.get("lang"),
        url_entities=list(entities.get("urls") or []),
        media=media,
    )
    if quoted and depth == 0 and quoted in inc["tweets"]:
        p.quoted = parse_api_post(inc["tweets"][quoted], inc, depth=1)
    return p


def _classify_errors(errors: list[dict[str, Any]], post_id: str) -> FetchError:
    err = next((e for e in errors if str(e.get("resource_id") or e.get("value") or "") == post_id), errors[0])
    kind = f"{err.get('type', '')} {err.get('title', '')}"
    detail = err.get("detail") or err.get("title") or "unknown error"
    if "resource-not-found" in kind or "Not Found" in kind:
        return FetchError(f"X post {post_id} not found: {detail}", permanent=True, reason="x_not_found")
    if "not-authorized" in kind or "Authorization Error" in kind or "Forbidden" in kind:
        return FetchError(f"X post {post_id} is protected or suspended: {detail}", permanent=True, reason="x_protected")
    return FetchError(f"X error for post {post_id}: {detail}", reason="x_error")


def _api_error(e: Exception, what: str) -> FetchError:
    if isinstance(e, RateLimited):
        return FetchError(f"X rate limit while fetching {what}; {format_reset(e.reset_at)}", reason="x_rate_limited")
    if isinstance(e, x_auth.XAuthError):
        return FetchError(f"X auth: {e}", reason="x_auth")
    assert isinstance(e, XApiError)
    if e.status == 401:
        return FetchError(f"X returned 401 for {what} after a token refresh", permanent=True, reason="http_401")
    if e.status == 404:
        return FetchError(f"X post {what} not found", permanent=True, reason="x_not_found")
    if e.status == 403:
        return FetchError(f"X returned 403 for {what}: {e}", permanent=True, reason="http_403")
    return FetchError(str(e), reason="x_error")


def lookup(client: XClient, post_id: str) -> Post:
    try:
        resp = client.get(f"/tweets/{post_id}", LOOKUP_PARAMS)
    except (XApiError, x_auth.XAuthError) as e:
        raise _api_error(e, post_id) from e
    data = resp.get("data")
    if not isinstance(data, dict) or not data.get("id"):
        errors = resp.get("errors") or []
        if errors:
            raise _classify_errors(errors, post_id)
        raise FetchError(f"X returned no data for post {post_id}", reason="x_error")
    return parse_api_post(data, _includes(resp))


def search_thread(client: XClient, root: Post) -> list[Post]:
    """The author's posts in the root's conversation (newest pages first, up to 5 pages)."""
    params: dict[str, Any] = {
        "query": f"conversation_id:{root.conversation_id or root.id} from:{root.username}",
        "max_results": 100,
        **LOOKUP_PARAMS,
    }
    out: list[Post] = []
    for _ in range(SEARCH_MAX_PAGES):
        try:
            resp = client.get("/tweets/search/recent", params)
        except (XApiError, x_auth.XAuthError) as e:
            raise _api_error(e, f"thread {root.id}") from e
        inc = _includes(resp)
        out.extend(parse_api_post(t, inc) for t in resp.get("data") or [] if t.get("id"))
        token = (resp.get("meta") or {}).get("next_token")
        if not token:
            break
        params["next_token"] = token
    return out


def self_chain(root: Post, candidates: list[Post]) -> list[Post]:
    """Root plus the author's self-reply chain, oldest first."""
    fetched = {p.id for p in candidates} | {root.id}
    kept = {root.id}
    chain = [root]
    epoch = datetime.min.replace(tzinfo=UTC)
    for p in sorted(candidates, key=lambda c: (c.created_at or epoch, int(c.id))):
        if p.id in kept or p.author_id != root.author_id:
            continue
        if p.conversation_id and p.conversation_id != (root.conversation_id or root.id):
            continue
        parent_ok = p.replied_to in kept
        orphan_self_reply = p.replied_to not in fetched and p.in_reply_to_user_id == root.author_id
        if parent_ok or orphan_self_reply:
            kept.add(p.id)
            chain.append(p)
    return chain


def fetch_via_api(item: Item, ctx: Any, post_id: str, now: datetime) -> FetchedItem:
    client = XClient(ctx.settings, ctx.queue, ctx.http)
    post = lookup(client, post_id)
    cid = post.conversation_id or post.id
    is_root = cid == post.id
    self_reply = not is_root and post.author_id is not None and post.in_reply_to_user_id == post.author_id
    if not (is_root or self_reply):
        return build_item([post], url_post=post, thread=None)

    root = post
    if self_reply:
        try:
            root = lookup(client, cid)
        except FetchError as e:
            if not e.permanent:
                raise
            return build_item([post], url_post=post, thread="incomplete")  # root deleted or hidden
        if root.author_id != post.author_id:
            # The author's chain starts under someone else's post: no self-thread root.
            return build_item([post], url_post=post, thread="incomplete")

    root_time = root.created_at or snowflake_time(root.id)
    if not root.username or not root_time or now - root_time >= SEARCH_WINDOW:
        return build_item([post], url_post=post, thread="incomplete")

    try:
        candidates = search_thread(client, root)
    except FetchError as e:
        # Search is best effort: on the last attempt keep the post rather than lose it.
        if e.permanent or item.attempts + 1 >= ctx.settings.run.max_attempts:
            return build_item([post], url_post=post, thread="incomplete")
        raise
    chain = self_chain(root, candidates)
    if post.id not in {p.id for p in chain}:
        chain.append(post)  # search index lag: never drop the bookmarked post itself
        chain.sort(key=lambda c: int(c.id))
    return build_item(chain, url_post=post, thread="complete")


# Backfill export ----------------------------------------------------------------------
def _unwrap_graphql(t: Any) -> dict[str, Any] | None:
    """GraphQL tweet unions: {__typename: TweetWithVisibilityResults, tweet: {...}}."""
    if isinstance(t, dict) and isinstance(t.get("tweet"), dict) and "legacy" not in t:
        t = t["tweet"]
    return t if isinstance(t, dict) and isinstance(t.get("legacy"), dict) else None


def _gql_user(t: dict[str, Any]) -> tuple[str | None, str | None]:
    u = ((t.get("core") or {}).get("user_results") or {}).get("result") or {}
    core, legacy = u.get("core") or {}, u.get("legacy") or {}
    return core.get("screen_name") or legacy.get("screen_name"), core.get("name") or legacy.get("name")


def _gql_text_and_urls(t: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    legacy = t.get("legacy") or {}
    note = (((t.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result")) or {}
    if note.get("text"):
        return note["text"], list((note.get("entity_set") or {}).get("urls") or [])
    return legacy.get("full_text") or "", list((legacy.get("entities") or {}).get("urls") or [])


def _gql_media(t: dict[str, Any]) -> list[dict[str, Any]]:
    legacy = t.get("legacy") or {}
    return list(
        (legacy.get("extended_entities") or {}).get("media") or (legacy.get("entities") or {}).get("media") or []
    )


def _resolve_tco(http: httpx.Client, links: list[str]) -> list[dict[str, Any]]:
    """Ask t.co where each link goes (no X API cost). t.co answers 301 to non-browser
    user agents and a 200 HTML page to browsers, hence the explicit UA."""
    out = []
    for short in links[:TCO_RESOLVE_LIMIT]:
        try:
            r = http.head(short, follow_redirects=False, headers={"User-Agent": "kb-engine"}, timeout=5.0)
        except httpx.HTTPError:
            continue
        loc = r.headers.get("location")
        if r.status_code in (301, 302, 307, 308) and loc:
            out.append({"url": short, "expanded_url": loc})
    return out


def export_post(rec: dict[str, Any], http: httpx.Client | None) -> tuple[Post, bool, bool]:
    """A Post from a twitter-web-exporter record. Returns (post, is_reply, is_self_reply).

    Exporter fields (src/components/table/columns-tweet.tsx): id, created_at (formatted,
    default `YYYY-MM-DD HH:mm:ss Z`), full_text (note text when present, t.co links
    unexpanded), media [{type, url (the t.co link), thumbnail, original, ext_alt_text}],
    screen_name, name, in_reply_to (parent id), quoted_status (quoted id only), url, and
    `metadata` (the raw GraphQL tweet) when "Include all metadata" was ticked. The
    metadata, when present, supplies expanded links, the quoted post's text and `lang`.
    """
    meta = _unwrap_graphql(rec.get("metadata"))
    post_id = str(rec.get("id") or (meta or {}).get("rest_id") or "")
    username = rec.get("screen_name")
    if not username and isinstance(rec.get("url"), str):
        m = re.match(r"https?://(?:www\.)?(?:twitter|x)\.com/([^/]+)/status", rec["url"])
        username = m.group(1) if m and m.group(1) != "i" else None
    name = rec.get("name")
    text = rec.get("full_text") or ""
    url_entities: list[dict[str, Any]] = []
    lang = None
    quoted: Post | None = None
    self_reply = False
    created = parse_created(rec.get("created_at"))
    in_reply = rec.get("in_reply_to")
    quoted_id = str(rec["quoted_status"]) if rec.get("quoted_status") else None

    if meta:
        mtext, url_entities = _gql_text_and_urls(meta)
        text = text or mtext
        legacy = meta["legacy"]
        lang = legacy.get("lang")
        created = created or parse_created(legacy.get("created_at"))
        in_reply = in_reply or legacy.get("in_reply_to_status_id_str")
        quoted_id = quoted_id or legacy.get("quoted_status_id_str")
        mu, mn = _gql_user(meta)
        username, name = username or mu, name or mn
        owner = legacy.get("user_id_str") or ((meta.get("core") or {}).get("user_results") or {}).get("result", {}).get(
            "rest_id"
        )
        self_reply = bool(in_reply and owner and legacy.get("in_reply_to_user_id_str") == owner)
        q = _unwrap_graphql((meta.get("quoted_status_result") or {}).get("result"))
        if q:
            qtext, qurls = _gql_text_and_urls(q)
            qu, qn = _gql_user(q)
            qmedia = _gql_media(q)
            quoted = Post(
                id=str(q.get("rest_id") or q["legacy"].get("id_str") or quoted_id or ""),
                text=qtext,
                username=qu,
                name=qn,
                quoted_id=q["legacy"].get("quoted_status_id_str"),
                url_entities=qurls + [{"url": m.get("url"), "media_key": "m"} for m in qmedia if m.get("url")],
            )
            quoted_id = quoted_id or quoted.id

    media_recs = [m for m in rec.get("media") or [] if isinstance(m, dict)]
    media = []
    for m in media_recs:
        mtype = m.get("type") or "photo"
        media.append(Media(mtype, m.get("original") or m.get("thumbnail"), m.get("ext_alt_text")))
        if m.get("url"):
            url_entities.append({"url": m["url"], "media_key": "export"})

    if not meta and http is not None:
        known = {e["url"] for e in url_entities}
        bare = [u for u in dict.fromkeys(TCO.findall(text)) if u not in known]
        if quoted_id and bare and text.rstrip().endswith(bare[-1]):
            bare = bare[:-1]  # the trailing link of a quote post is the quoted post
        url_entities.extend(_resolve_tco(http, bare))

    post = Post(
        id=post_id,
        text=text,
        username=username,
        name=name,
        created_at=created or snowflake_time(post_id),
        quoted_id=quoted_id,
        lang=lang,
        url_entities=url_entities,
        media=media,
        quoted=quoted,
    )
    return post, bool(in_reply), self_reply


def fetch_from_export(item: Item, ctx: Any, rec: dict[str, Any], post_id: str) -> FetchedItem:
    post, is_reply, self_reply = export_post({**rec, "id": rec.get("id") or post_id}, getattr(ctx, "http", None))
    thread = "incomplete" if (not is_reply or self_reply) else None
    return build_item([post], url_post=post, thread=thread)


# Entry point --------------------------------------------------------------------------
def post_id_of(item: Item) -> str:
    m = _STATUS_ID.search(item.canonical_url)
    if not m:
        raise FetchError(f"not an X status URL: {item.canonical_url}", permanent=True, reason="bad_url")
    return m.group(1)


def fetch(item: Item, ctx: Any, *, now: datetime | None = None) -> FetchedItem:
    post_id = post_id_of(item)
    if item.origin == "backfill" and item.inline_text and not ctx.settings.x.backfill_fetch_via_api:
        try:
            rec = json.loads(item.inline_text)
        except ValueError:
            rec = None
        if isinstance(rec, dict):
            return fetch_from_export(item, ctx, rec, post_id)
        # A corrupt record falls through to the API, which is the source of truth anyway.
    return fetch_via_api(item, ctx, post_id, now or _now())
