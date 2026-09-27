"""Web fetcher: one GET, main-content extraction to markdown with trafilatura (SDD §7.2).

Failure mapping follows SDD §13. Only outcomes that retrying cannot change go straight
to `failed_permanent`: 404, 410, 401 (content behind a login), a content type we cannot
read yet, and pages that yield less than `settings.web.min_chars` of text (paywalls and
JS-only pages). Everything else (403 bot walls, 429, 5xx, timeouts, connection errors)
is transient; `run.max_attempts` caps the cost of being wrong about that.

trafilatura gets the raw bytes so it can detect the encoding from the document, except
when the charset is declared only in the HTTP header: then the header wins (see
`html_input`). The body is streamed with a byte cap and a wall-clock deadline.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
import trafilatura
from trafilatura.metadata import extract_metadata
from trafilatura.utils import load_html

from ..models import FetchedItem, FetchError
from ..normalize import InvalidURL, normalize
from ..queue import Item
from ..textutil import one_line

if TYPE_CHECKING:
    from lxml.html import HtmlElement

PERMANENT_STATUS = {401, 404, 410, 451}
HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown")
MAX_TITLE_CHARS = 200
MAX_BYTES = 10 * 1024 * 1024
"""Bigger than any article; anything larger is a media file or a runaway response."""
_LANG = re.compile(r"[a-z]{2}")
_META_CHARSET = re.compile(rb"<meta[^>]+charset", re.IGNORECASE)
_BINARY_MAGIC = (b"%PDF-", b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"PK\x03\x04", b"\x1f\x8b", b"RIFF", b"\x00\x00\x00")
# Login, consent and subscription walls, recognised on the URL a redirect lands on.
_WALL_WORD = re.compile(
    r"^(?:consent|guce|login|log-in|logon|signin|sign-in|sign_in|subscribe|subscription|account|accounts"
    # Whole segment (or `login.php`), never a slug prefix like `login-form-design`.
    r"|myaccount|auth|oauth|sso|paywall|register|signup)(?:$|\.)",
    re.IGNORECASE,
)

_LOCALE = re.compile(r"[a-z]{2}(?:[-_][a-z]{2})?", re.IGNORECASE)

_clock = time.monotonic  # tests replace this to simulate a slow server without sleeping


@dataclass(frozen=True)
class Page:
    url: str
    """Final URL after redirects, as the server reported it."""
    media_type: str
    charset: str | None
    content: bytes


def _fail_status(status: int, url: str) -> FetchError:
    permanent = status in PERMANENT_STATUS
    reason = "http_5xx" if status >= 500 else f"http_{status}"
    hint = {401: " (login required)", 451: " (unavailable for legal reasons)"}.get(status, "")
    return FetchError(f"HTTP {status}{hint} for {url}", permanent=permanent, reason=reason)


def parse_content_type(value: str) -> tuple[str, str | None]:
    """`text/html; charset=ISO-8859-1` -> ("text/html", "iso-8859-1")."""
    parts = [p.strip() for p in value.split(";")]
    charset = None
    for param in parts[1:]:
        key, _, val = param.partition("=")
        if key.strip().lower() == "charset" and val.strip().strip("\"'"):
            charset = val.strip().strip("\"'").lower()
    return parts[0].lower(), charset


def is_wall_redirect(requested: str, final: str) -> bool:
    """A redirect that landed on a login / consent / subscribe page instead of the content."""
    req, fin = urlsplit(requested), urlsplit(final)
    if fin.path.rstrip("/") == req.path.rstrip("/"):
        return False  # apex -> www, http -> https, trailing slash: same page
    req_label = (req.hostname or "").removeprefix("www.").split(".", 1)[0]
    fin_label = (fin.hostname or "").removeprefix("www.").split(".", 1)[0]
    if fin_label != req_label and _WALL_WORD.match(fin_label):
        return True  # consent.example.com, accounts.example.com, login.example.com
    # Only where a wall lives: the first path segment (after an optional locale such as
    # /en/ or /es-ar/), and only if the requested URL did not already have it there.
    # Scanning every segment flagged real articles like /articles/login-walls.
    segs = [seg for seg in fin.path.split("/") if seg]
    if segs and _LOCALE.fullmatch(segs[0]):
        segs = segs[1:]
    req_segs = {seg.lower() for seg in req.path.split("/") if seg}
    return bool(segs) and segs[0].lower() not in req_segs and bool(_WALL_WORD.match(segs[0]))


def _wall_error(requested: str, final: str) -> FetchError:
    return FetchError(
        f"{requested} redirected to a login, consent or subscription wall ({final}); "
        "clip the page manually (automatic clipping is Phase 2)",
        permanent=True,
        reason="login_wall",
    )


def _unsupported(what: str, url: str) -> FetchError:
    return FetchError(
        f"unsupported content type {what} at {url} (only HTML pages for now; PDF support is Phase 2)",
        permanent=True,
        reason="unsupported_content_type",
    )


def _too_large(url: str, limit: int) -> FetchError:
    return FetchError(
        f"response from {url} exceeds {limit // (1024 * 1024)} MB: not an article",
        permanent=True,
        reason="too_large",
    )


def download(http: httpx.Client, url: str, timeout: float, max_bytes: int = MAX_BYTES) -> Page:
    """Stream `url` and return its body, translating every failure into a FetchError.

    Rejections happen as early as the information allows: status, wall redirects and
    content type from the headers, before any body byte is read; the size cap on
    Content-Length or while streaming; and a wall-clock deadline of `timeout` seconds
    from the request start, which per-read timeouts alone cannot give (a server that
    drips one byte every few seconds never trips them).
    """
    started = _clock()
    try:
        with http.stream("GET", url, timeout=timeout) as resp:
            if resp.status_code >= 400:
                raise _fail_status(resp.status_code, url)
            final = str(resp.url)
            if is_wall_redirect(url, final):
                raise _wall_error(url, final)
            mtype, charset = parse_content_type(resp.headers.get("content-type", ""))
            if mtype and mtype not in HTML_TYPES and mtype not in TEXT_TYPES:
                raise _unsupported("PDF" if mtype == "application/pdf" else mtype, final)
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise _too_large(final, max_bytes)
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise _too_large(final, max_bytes)
                if _clock() - started > timeout:
                    raise FetchError(f"download of {url} took longer than {timeout:g}s", reason="timeout")
    except httpx.TimeoutException as e:
        raise FetchError(f"timed out after {timeout:g}s: {url}", reason="timeout") from e
    except httpx.TooManyRedirects as e:
        raise FetchError(f"redirect loop: {url}", permanent=True, reason="too_many_redirects") from e
    except (httpx.UnsupportedProtocol, httpx.InvalidURL) as e:
        raise FetchError(f"invalid URL {url}: {e}", permanent=True, reason="invalid_url") from e
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__}: {e}", reason="network_error") from e
    content = bytes(buf)
    if not mtype:
        head = content.lstrip()[:8]
        if head.startswith(b"%PDF-"):
            raise _unsupported("PDF", final)
        if head.startswith(_BINARY_MAGIC) or b"\x00" in content[:1024]:
            raise _unsupported("binary data", final)
    return Page(final, mtype, charset, content)


def decode_text(content: bytes, charset: str | None) -> str:
    """Plain text: the declared charset, else UTF-8, else Windows-1252 (a superset of Latin-1)."""
    for enc in (charset, "utf-8"):
        if enc:
            try:
                return content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                pass
    return content.decode("cp1252", errors="replace")


def html_input(content: bytes, charset: str | None) -> bytes | str:
    """What to hand trafilatura. It detects encodings from the document, so bytes are
    best, except when the charset is only in the HTTP header: then decode with it."""
    if charset and not _META_CHARSET.search(content[:4096]):
        try:
            return content.decode(charset)
        except (UnicodeDecodeError, LookupError):
            pass
    return content


def iso_language(value: str | None) -> str | None:
    """`en-US`, `pt_BR`, `EN` -> ISO 639-1 lowercase; anything else -> None."""
    if not value:
        return None
    base = value.strip().replace("_", "-").split("-", 1)[0].lower()
    return base if _LANG.fullmatch(base) else None


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _first(tree: HtmlElement, *xpaths: str) -> str | None:
    for xp in xpaths:
        for v in tree.xpath(xp):
            text = " ".join(str(v).split())
            if text:
                return text
    return None


def page_language(tree: HtmlElement) -> str | None:
    return iso_language(
        _first(
            tree,
            "/html/@lang",
            "//html/@xml:lang",
            "//meta[translate(@http-equiv,'CONTENT-LANGUAGE','content-language')='content-language']/@content",
            "//meta[@property='og:locale']/@content",
        )
    )


def fallback_title(tree: HtmlElement | None, url: str) -> str:
    """og:title, then <title>, then host + path."""
    if tree is not None:
        found = _first(tree, "//meta[@property='og:title']/@content", "//title/text()")
        if found:
            return found
    parts = urlsplit(url)
    host = parts.hostname or url
    host = host[4:] if host.startswith("www.") else host
    path = parts.path.rstrip("/")
    return f"{host}{path}" if path else host


def final_url(fetched_url: str, item: Item) -> str:
    try:
        return normalize(fetched_url).canonical_url
    except InvalidURL:
        return item.canonical_url


def _too_short(url: str, n: int, min_chars: int) -> FetchError:
    return FetchError(
        f"extracted only {n} characters (< {min_chars}) from {url}: "
        "likely a paywall, a login wall or a JavaScript-only page",
        permanent=True,
        reason="extraction_empty",
    )


# A table cell this long holds prose, not data: the page uses tables for layout.
LAYOUT_CELL_CHARS = 400
_TABLE_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "td", "th"}


def _trafilatura(html: bytes | str | HtmlElement, url: str) -> str:
    out = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_comments=False,
        include_images=False,
    )
    return (out or "").strip()


def is_layout_table(md: str) -> bool:
    """True when a markdown table row carries a paragraph-sized cell (old-school table layout)."""
    return any(
        line.lstrip().startswith("|") and max(len(c) for c in line.split("|")) > LAYOUT_CELL_CHARS
        for line in md.splitlines()
    )


def unwrap_layout_tables(tree: HtmlElement) -> HtmlElement:
    """Turn every table that wraps a paragraph-sized cell into plain divs, in place."""
    big = [cell for cell in tree.iter("td", "th") if len(" ".join(cell.text_content().split())) > LAYOUT_CELL_CHARS]
    for cell in big:
        for el in (cell, *cell.iterancestors()):
            if el.tag in _TABLE_TAGS:
                el.tag = "div"
    return tree


def _extract_html(raw: bytes | str, url: str) -> tuple[str, dict[str, Any], HtmlElement | None]:
    body = _trafilatura(raw, url)
    tree = load_html(raw) if raw.strip() else None
    if tree is not None and is_layout_table(body):
        # Sites like paulgraham.com put the whole article in a layout table, which comes
        # out as one giant markdown cell. As divs it extracts as prose, links included.
        body = _trafilatura(unwrap_layout_tables(load_html(raw)), url) or body
    meta: dict[str, Any] = {}
    if tree is not None:
        # extensive=False: dates from markup and URLs only. The extensive search guesses
        # from free text ("in the summer of 1995" became 1995-01-01); no date beats a wrong one.
        doc = extract_metadata(tree, default_url=url, extensive=False)
        meta = {k: getattr(doc, k, None) for k in ("title", "author", "date", "sitename", "hostname")}
        meta["language"] = page_language(tree)
    return body, meta, tree


_LIST_ITEM = re.compile(r"(?:[-*+]|\d+[.)])\s")
_FENCE = re.compile(r"^\s*(```|~~~)")
_EMPTY_ROW = re.compile(r"^\|[\s|]*$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{3,}")
_SPACES = re.compile(r"(?<=\S) {2,}(?=\S)")


def _block_start(stripped: str) -> bool:
    """Lines that open their own markdown block and must never be glued to the previous one."""
    return bool(stripped[:1] in ("#", "|", ">") or _LIST_ITEM.match(stripped) or _FENCE.match(stripped))


def _joinable_after(prev: str) -> bool:
    s = prev.strip()
    return bool(s) and s[:1] not in ("#", "|") and not _FENCE.match(s)


def tidy_markdown(md: str, title: str | None = None) -> str:
    """Undo trafilatura's layout artifacts and drop the page's own title heading.

    - Source-HTML line wraps and indentation survive when text sits outside a `<p>`
      (tails of links, `<font>`/`<div>` layouts). A plain newline inside a paragraph is
      such a wrap (trafilatura renders `<br>` as a blank line), so wrapped lines are
      joined back. Leading indentation goes too: 4+ spaces would render prose as code.
      List items keep theirs, since it encodes nesting.
    - Rows of empty cells left by layout tables are dropped.
    - A closing code fence can be glued to the next paragraph; a blank line is added.
    - A leading `# <title>` duplicates the title the summary page already carries.
    Code blocks are left byte-for-byte alone.
    """
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    after_fence = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            if not in_code and out and out[-1].strip():
                out.append("")
            out.append(line)
            in_code = not in_code
            after_fence = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        stripped = line.strip()
        if not stripped.startswith("|"):  # table cells keep their padding
            stripped = _SPACES.sub(" ", stripped)
        if after_fence and stripped:
            out.append("")
        after_fence = False
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if _EMPTY_ROW.match(stripped) and not _TABLE_SEP.match(nxt):
            continue
        if stripped and out and _joinable_after(out[-1]) and not _block_start(stripped):
            out[-1] = out[-1].rstrip() + " " + stripped
        elif stripped and not _LIST_ITEM.match(stripped):
            out.append(stripped)
        else:
            out.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if title:
        first, _, rest = text.partition("\n")
        if first.startswith("# ") and is_title_heading(first[2:], title):
            text = rest.strip()
    return text


_TITLE_SEPARATORS = (" - ", " | ", " – ", " — ", ": ", " · ", " :: ")


def is_title_heading(heading: str, title: str) -> bool:
    """The H1 is the page title, possibly without the site suffix ("Tenis - Wikipedia, ...")."""
    h, t = _fold(heading), _fold(title)
    return bool(h) and (h == t or any(t.startswith(h + sep) for sep in _TITLE_SEPARATORS))


def _fold(s: str) -> str:
    return " ".join(s.split()).casefold()


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def fetch(item: Item, ctx) -> FetchedItem:
    settings = ctx.settings
    page = download(ctx.http, item.canonical_url, settings.web.timeout_s)
    url = final_url(page.url, item)
    min_chars = settings.web.min_chars

    if page.media_type in TEXT_TYPES:
        body = decode_text(page.content, page.charset).strip()
        if len(body) < min_chars:
            raise _too_short(url, len(body), min_chars)
        first = next((ln for ln in body.splitlines() if ln.strip()), "")
        return FetchedItem(
            source_type="web",
            url=url,
            title=one_line(first.lstrip("#"), 120) or fallback_title(None, url),
            body=body + "\n",
            extra={"hostname": urlsplit(url).hostname or None, "content_type": page.media_type},
        )

    raw_body, meta, tree = _extract_html(html_input(page.content, page.charset), url)
    title = one_line(_clean(meta.get("title")) or fallback_title(tree, url), MAX_TITLE_CHARS)
    body = tidy_markdown(raw_body, title)
    if len(body) < min_chars:
        raise _too_short(url, len(body), min_chars)

    extra = {"sitename": _clean(meta.get("sitename")), "hostname": urlsplit(url).hostname}
    return FetchedItem(
        source_type="web",
        url=url,
        title=title,
        body=body + "\n",
        author=_clean(meta.get("author")),
        published=parse_date(meta.get("date")),
        language=meta.get("language"),
        outlinks=[],
        extra={k: v for k, v in extra.items() if v},
    )
