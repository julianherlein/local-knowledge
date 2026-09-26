"""Canonical URLs, source-type detection and dedup keys (SDD §7.1).

Pure functions. The canonical URL is the dedup key, so every rule here must be
stable: changing one silently re-admits duplicates. Covered by tests/test_normalize.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com"}
YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}
YOUTU_BE = "youtu.be"

# Tracking params dropped everywhere. `t` is dropped for YouTube too (SDD §7.1).
DROP_PARAMS = {"si", "s", "t", "ref", "ref_src", "ref_url", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "feature"}

_X_STATUS = re.compile(r"^/(?:[^/]+|i(?:/web)?)/status(?:es)?/(\d+)")
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_PATH_ID = re.compile(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})")
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
TELEGRAM_SCHEME = "telegram://"


class InvalidURL(ValueError):
    pass


@dataclass(frozen=True)
class Normalized:
    canonical_url: str
    source_type: str  # x | youtube | web
    x_status_id: str | None = None
    youtube_id: str | None = None


def _host(netloc: str) -> str:
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


_TRAIL_PUNCT = ".,;:!?'\""
_PAIRS = {")": "(", "]": "[", "}": "{"}


def trim_trailing(url: str) -> str:
    """Drop sentence punctuation after a URL, but keep a closing bracket the URL itself opened
    (`wiki/Mercury_(planet)` stays intact, `(see https://x.com/a)` loses the `)`)."""
    while url:
        last = url[-1]
        if last in _TRAIL_PUNCT:
            url = url[:-1]
        elif last in _PAIRS and url.count(_PAIRS[last]) < url.count(last):
            url = url[:-1]
        else:
            break
    return url


def normalize(url: str) -> Normalized:
    url = trim_trailing(url.strip().strip("<>"))
    if url.startswith(TELEGRAM_SCHEME):
        return Normalized(canonical_url=url, source_type="web")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise InvalidURL(url)
    host = _host(parts.netloc)
    if not host or "." not in host:
        raise InvalidURL(url)
    path = parts.path or "/"

    if host in X_HOSTS:
        m = _X_STATUS.match(path)
        if m:
            sid = m.group(1)
            return Normalized(canonical_url=f"https://x.com/i/status/{sid}", source_type="x", x_status_id=sid)
        # Profiles and other X pages are plain web pages for our purposes.
        return Normalized(canonical_url=_clean_web("x.com", path, parts.query), source_type="web")

    yt_id = None
    if host == YOUTU_BE:
        cand = path.strip("/").split("/", 1)[0]
        yt_id = cand if _YT_ID.match(cand) else None
    elif host in YOUTUBE_HOSTS:
        if path.rstrip("/") == "/watch":
            cand = dict(parse_qsl(parts.query)).get("v", "")
            yt_id = cand if _YT_ID.match(cand) else None
        else:
            m = _YT_PATH_ID.match(path)
            yt_id = m.group(1) if m else None
    if yt_id:
        return Normalized(canonical_url=f"https://youtube.com/watch?v={yt_id}", source_type="youtube", youtube_id=yt_id)
    if host in YOUTUBE_HOSTS or host == YOUTU_BE:
        # Channel, playlist or search pages: no transcript to fetch, treat as web.
        return Normalized(canonical_url=_clean_web("youtube.com", path, parts.query), source_type="web")

    try:
        port = parts.port
    except ValueError as e:
        raise InvalidURL(url) from e
    scheme = parts.scheme.lower()
    if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
        # A non-default port is a different server: keep it, and the scheme that goes with it.
        return Normalized(canonical_url=_clean_web(f"{host}:{port}", path, parts.query, scheme), source_type="web")
    return Normalized(canonical_url=_clean_web(host, path, parts.query), source_type="web")


def _clean_web(host: str, path: str, query: str, scheme: str = "https") -> str:
    kept = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in DROP_PARAMS
    ]
    kept.sort()
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, urlencode(kept), ""))


def extract_urls(text: str) -> list[str]:
    """URLs in free text, in order, without duplicates (by canonical form)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.finditer(text or ""):
        raw = trim_trailing(m.group(0))
        try:
            key = normalize(raw).canonical_url
        except InvalidURL:
            continue
        if key not in seen:
            seen.add(key)
            out.append(raw)
    return out


_HASHTAG = re.compile(r"(?<![\w#])#([A-Za-z][\w-]*)")


def extract_hashtags(text: str) -> list[str]:
    """Hashtags (lowercased, no '#') in order, without duplicates. URL fragments are ignored."""
    stripped = URL_RE.sub(" ", text or "")
    out: list[str] = []
    for m in _HASHTAG.finditer(stripped):
        t = m.group(1).lower()
        if t not in out:
            out.append(t)
    return out
