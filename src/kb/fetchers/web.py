"""Web fetcher: one GET, main-content extraction to markdown with trafilatura (SDD §7.2).

Failure mapping follows SDD §13. Only outcomes that retrying cannot change go straight
to `failed_permanent`: 404, 410, 401 (content behind a login), a content type we cannot
read yet, and pages that yield less than `settings.web.min_chars` of text (paywalls and
JS-only pages). Everything else (403 bot walls, 429, 5xx, timeouts, connection errors)
is transient; `run.max_attempts` caps the cost of being wrong about that.

trafilatura gets the raw bytes rather than `response.text`: it detects the encoding
from the document itself, which is more reliable than a missing or wrong charset header.
"""

from __future__ import annotations

import re
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

PERMANENT_STATUS = {401, 404, 410}
HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown")
_LANG = re.compile(r"[a-z]{2}")


def _fail_status(status: int, url: str) -> FetchError:
    permanent = status in PERMANENT_STATUS
    reason = "http_5xx" if status >= 500 else f"http_{status}"
    hint = " (login required)" if status == 401 else ""
    return FetchError(f"HTTP {status}{hint} for {url}", permanent=permanent, reason=reason)


def get(http: httpx.Client, url: str, timeout: float) -> httpx.Response:
    """GET `url`, translating every httpx failure into a FetchError."""
    try:
        resp = http.get(url, timeout=timeout)
    except httpx.TimeoutException as e:
        raise FetchError(f"timed out after {timeout:g}s: {url}", reason="timeout") from e
    except httpx.TooManyRedirects as e:
        raise FetchError(f"redirect loop: {url}", permanent=True, reason="too_many_redirects") from e
    except (httpx.UnsupportedProtocol, httpx.InvalidURL) as e:
        raise FetchError(f"invalid URL {url}: {e}", permanent=True, reason="invalid_url") from e
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__}: {e}", reason="network_error") from e
    if resp.status_code >= 400:
        raise _fail_status(resp.status_code, url)
    return resp


def media_type(resp: httpx.Response) -> str:
    ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not ctype and resp.content.lstrip()[:5] == b"%PDF-":
        return "application/pdf"
    return ctype


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


def final_url(resp: httpx.Response, item: Item) -> str:
    try:
        return normalize(str(resp.url)).canonical_url
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


def _trafilatura(html: bytes | HtmlElement, url: str) -> str:
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


def _extract_html(raw: bytes, url: str) -> tuple[str, dict[str, Any], HtmlElement | None]:
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
    resp = get(ctx.http, item.canonical_url, settings.web.timeout_s)
    url = final_url(resp, item)
    ctype = media_type(resp)
    min_chars = settings.web.min_chars

    if ctype in TEXT_TYPES:
        body = resp.text.strip()
        if len(body) < min_chars:
            raise _too_short(url, len(body), min_chars)
        first = next((ln for ln in body.splitlines() if ln.strip()), "")
        return FetchedItem(
            source_type="web",
            url=url,
            title=one_line(first.lstrip("#"), 120) or fallback_title(None, url),
            body=body + "\n",
            extra={"hostname": urlsplit(url).hostname or None, "content_type": ctype},
        )

    if ctype and ctype not in HTML_TYPES:
        what = "PDF" if ctype == "application/pdf" else ctype
        raise FetchError(
            f"unsupported content type {what} at {url} (only HTML pages for now; PDF support is Phase 2)",
            permanent=True,
            reason="unsupported_content_type",
        )

    raw_body, meta, tree = _extract_html(resp.content, url)
    title = _clean(meta.get("title")) or fallback_title(tree, url)
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
