"""Contracts shared by fetchers, tagger, writer and summarizer."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

# Front matter must stay flat (SDD §11.1): scalars and lists of scalars only.
FlatValue = str | int | float | bool | date | list[str] | None


class FetchedItem(BaseModel):
    """What every fetcher returns. Persisted as JSON between pipeline steps."""

    source_type: str  # x | youtube | web
    url: str
    """Best human-facing URL for the source (canonical, or the page's own canonical link)."""
    title: str
    body: str
    """Clean markdown body. Written once to raw/ and never edited."""
    author: str | None = None
    published: date | None = None
    language: str | None = None
    """ISO 639-1 if known from the source; the classifier fills it otherwise."""
    outlinks: list[str] = Field(default_factory=list)
    thread: str | None = None
    """X only: complete | incomplete."""
    extra: dict[str, FlatValue] = Field(default_factory=dict)
    """Additional flat front matter, e.g. channel, duration, subtitles."""


class FetchError(Exception):
    """A fetch failed. `permanent` skips retries (404, paywall, no subtitles, ...)."""

    def __init__(self, message: str, *, permanent: bool = False, reason: str | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.reason = reason or ("fetch_error" if not permanent else "permanent_error")


class TagResult(BaseModel):
    domains: list[str]
    method: str  # hashtag | classifier | fallback
    confidence: float | None = None
    reason: str | None = None
    language: str | None = None


class SummaryResult(BaseModel):
    title: str
    tldr: str
    key_points: list[str]
    claims: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
