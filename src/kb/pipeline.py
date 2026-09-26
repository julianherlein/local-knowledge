"""The automated pipeline (SDD §4, §6.3, §7): capture, then drive each item to `done`.

Each step persists its result before the next begins, so a crash or Ctrl-C resumes
from the last completed step on the next `kb run`:

    queued      --fetch-->      fetched     (payload saved in `fetched` table)
    fetched     --tag-->        tagged      (domains on the item row)
    tagged      --write raw-->  written     (raw/<type>/<stem>.md)
    written     --summarize-->  summarized  (wiki/sources/<stem>.md)
    summarized  --queue line--> done        (wiki/_compile-queue.md)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import httpx
from kb_llm import LLMClient, LLMError, make_client

from . import fm, summarizer, tagger, writer
from .capture import CaptureContext, CaptureReport
from .config import Settings
from .fetchers import FetchContext, fetch
from .git_ops import GitError, Tracker, sha256_bytes
from .models import FetchedItem, FetchError, TagResult
from .obs import run_lock
from .queue import FAILED, Item, Queue, parse_iso
from .vault import COMPILE_QUEUE, Vault

log = logging.getLogger("kb.pipeline")

Progress = Callable[[str], None]


@dataclass
class ItemOutcome:
    item_id: int
    status: str
    title: str | None
    error: str | None = None


@dataclass
class RunReport:
    captures: list[CaptureReport] = field(default_factory=list)
    outcomes: list[ItemOutcome] = field(default_factory=list)
    committed: bool = False
    commit_message: str = ""

    @property
    def done(self) -> int:
        return sum(o.status == "done" for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(o.status.startswith("failed") for o in self.outcomes)


def make_http(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=settings.web.timeout_s,
        follow_redirects=True,
        headers={"User-Agent": settings.web.user_agent},
    )


def make_llm(settings: Settings) -> LLMClient:
    return make_client(settings.llm.backend, settings.llm.model, binary=settings.llm.binary, effort=settings.llm.effort)


class Pipeline:
    def __init__(self, settings: Settings, queue: Queue, llm: LLMClient, http: httpx.Client, vault: Vault) -> None:
        self.settings = settings
        self.queue = queue
        self.llm = llm
        self.http = http
        self.vault = vault
        self.fetch_ctx = FetchContext(settings, http, queue)

    # Steps ------------------------------------------------------------------------
    def _payload(self, item: Item) -> FetchedItem:
        data = self.queue.load_payload(item.id)
        if data is None:
            # Payload lost (e.g. DB restored): refetch rather than fail.
            fetched = fetch(item, self.fetch_ctx)
            self.queue.save_payload(item.id, fetched.model_dump(mode="json"))
            return fetched
        return FetchedItem.model_validate(data)

    def step_fetch(self, item: Item) -> None:
        fetched = fetch(item, self.fetch_ctx)
        self.queue.save_payload(item.id, fetched.model_dump(mode="json"))
        self.queue.advance(
            item.id,
            "fetched",
            title=fetched.title,
            author=fetched.author,
            published=fetched.published.isoformat() if fetched.published else None,
            language=fetched.language,
        )

    def step_tag(self, item: Item) -> None:
        fetched = self._payload(item)
        if item.tag_method == "manual" and item.domains:
            # `kb tag` ran before this step: the user's choice wins, no classifier call.
            self.queue.advance(item.id, "tagged", language=item.language or fetched.language)
            return
        result = tagger.tag(
            self.settings, self.llm, fetched, item.hint_tags, note=item.note, queue=self.queue, item_id=item.id
        )
        self.queue.advance(
            item.id,
            "tagged",
            domains=result.domains,
            tag_method=result.method,
            tag_confidence=result.confidence,
            tag_reason=result.reason,
            language=result.language,
        )

    def step_write(self, item: Item) -> None:
        fetched = self._payload(item)
        tagged = TagResult(
            domains=item.domains or [tagger.UNSORTED],
            method=item.tag_method or "fallback",
            confidence=item.tag_confidence,
            language=item.language,
        )
        raw_rel = item.raw_path
        if not raw_rel:
            captured = parse_iso(item.captured_at)
            day = captured.date() if captured else date.today()
            stem = self.vault.stem_for(fetched.source_type, day, fetched.title, taken=self.queue.taken_stems())
            raw_rel = writer.raw_rel(fetched.source_type, stem)
            # Persist the path before writing so a crash re-uses it instead of creating a -2 twin.
            self.queue.update(item.id, raw_path=raw_rel, summary_path=writer.summary_rel(stem))
        self.vault.write(raw_rel, writer.render_raw(item, fetched, tagged), item.id)
        self.queue.advance(item.id, "written", content_hash=sha256_bytes(fetched.body.encode("utf-8")))

    def step_summarize(self, item: Item) -> None:
        if not item.raw_path or not self.vault.exists(item.raw_path):
            raise FetchError(f"raw file missing: {item.raw_path}", permanent=True, reason="raw_missing")
        meta, body = fm.split(self.vault.read(item.raw_path))
        summary, truncated = summarizer.summarize(
            self.settings, self.llm, meta, body, queue=self.queue, item_id=item.id
        )
        summary_rel = item.summary_path or writer.summary_rel(item.raw_path.rsplit("/", 1)[-1].removesuffix(".md"))
        self.vault.write(summary_rel, writer.render_summary(item.raw_path, meta, item.id, summary, truncated), item.id)
        # The item title becomes the English title (D5), so digests and status read in English.
        self.queue.advance(
            item.id,
            "summarized",
            summary_path=summary_rel,
            tldr=" ".join(summary.tldr.split()),
            title=" ".join(summary.title.split()) or item.title,
        )

    def step_queue(self, item: Item) -> None:
        stem = (item.summary_path or "").rsplit("/", 1)[-1].removesuffix(".md")
        existing = self.vault.read(COMPILE_QUEUE) if self.vault.exists(COMPILE_QUEUE) else None
        new = writer.append_compile_queue(existing, stem, item.domains, item.tldr or "")
        if new is not None:
            self.vault.write(COMPILE_QUEUE, new, item.id)
        self.queue.advance(item.id, "done")
        self.queue.drop_payload(item.id)

    STEPS = {
        "queued": "step_fetch",
        "fetched": "step_tag",
        "tagged": "step_write",
        "written": "step_summarize",
        "summarized": "step_queue",
    }

    def process(self, item: Item) -> ItemOutcome:
        item = self.queue.resume(item) if item.status == FAILED else item
        while item.status in self.STEPS:
            step = item.status
            try:
                getattr(self, self.STEPS[step])(item)
            except FetchError as e:
                return self._fail(item, step, str(e), e.permanent, e.reason)
            except LLMError as e:
                return self._fail(item, step, f"LLM: {e}", not e.transient, "llm_error")
            except Exception as e:  # a bug or an unexpected upstream failure: retry, but record it
                log.exception("step %s crashed for item %s", step, item.id)
                return self._fail(item, step, f"{type(e).__name__}: {e}", False, "unexpected_error")
            item = self.queue.get(item.id)  # type: ignore[assignment]
            log.info("item advanced", extra={"item_id": item.id, "status": item.status})
        return ItemOutcome(item.id, item.status, item.title)

    def _fail(self, item: Item, step: str, error: str, permanent: bool, reason: str | None) -> ItemOutcome:
        status = self.queue.fail(
            item.id, error, permanent=permanent, reason=reason, max_attempts=self.settings.run.max_attempts
        )
        log.warning(
            "item failed", extra={"item_id": item.id, "step": step, "status": status, "reason": reason, "error": error}
        )
        return ItemOutcome(item.id, status, item.title, error)


def run(
    settings: Settings,
    *,
    queue: Queue | None = None,
    llm: LLMClient | None = None,
    http: httpx.Client | None = None,
    limit: int | None = None,
    capture: bool = True,
    commit: bool = True,
    progress: Progress | None = None,
    skip: Callable[[Item], bool] | None = None,
) -> RunReport:
    """One `kb run`. Raises GitError before touching anything if the vault repo is unusable.

    `skip` filters pending items out of this run (used by --dry-run to avoid calls
    with side effects, such as X token rotation).
    """
    say = progress or (lambda _msg: None)
    own_queue = queue is None
    own_http = http is None
    queue = queue or Queue(settings.db_path)
    http = http or make_http(settings)
    report = RunReport()
    try:
        with run_lock(settings.lock_path):
            # Writes are always tracked, so a --no-commit run's files are picked up by the next commit.
            tracker = Tracker(settings.vault_path, queue)
            run_id = queue.start_run()
            vault = Vault(settings.vault_path, tracker)
            if capture:
                report.captures = run_captures(CaptureContext(settings, http, queue), say)
            items = queue.pending(limit if limit is not None else settings.run.max_items, settings.run.max_attempts)
            if skip:
                skipped = [i for i in items if skip(i)]
                items = [i for i in items if not skip(i)]
                if skipped:
                    say(f"skipping {len(skipped)} item(s) this run: " + ", ".join(f"#{i.id}" for i in skipped))
            if items:
                llm = llm or make_llm(settings)
                pipe = Pipeline(settings, queue, llm, http, vault)
                for i, item in enumerate(items, 1):
                    say(f"[{i}/{len(items)}] #{item.id} {item.title or item.canonical_url}")
                    outcome = pipe.process(item)
                    report.outcomes.append(outcome)
                    say(f"    -> {outcome.status}" + (f": {outcome.error}" if outcome.error else ""))
            if report.outcomes:
                # Keep today's digest current after a manual run; it rides in the same auto commit.
                try:
                    from . import digest

                    digest.run(settings, queue, day=date.today(), commit=False)
                except Exception:  # the digest is a convenience; never fail the ingest over it
                    log.exception("refreshing today's digest failed")
            if commit:
                try:
                    report.committed, report.commit_message = tracker.commit()
                except GitError as e:
                    # Files are written and still pending; the next run retries the commit.
                    log.error("auto commit failed: %s", e)
                    report.commit_message = f"commit failed (will retry next run): {e}"
            queue.finish_run(
                run_id,
                captured=sum(c.enqueued for c in report.captures),
                processed=len(report.outcomes),
                done=report.done,
                failed=report.failed,
                committed=int(report.committed),
                notes=report.commit_message,
            )
    finally:
        if own_http:
            http.close()
        if own_queue:
            queue.close()
    return report


def run_captures(ctx: CaptureContext, say: Progress) -> list[CaptureReport]:
    from .capture import telegram, x_bookmarks

    reports = []
    for name, adapter in (("telegram", telegram), ("x", x_bookmarks)):
        try:
            rep = adapter.poll(ctx)
        except Exception as e:  # one broken adapter must not block the rest of the run
            log.exception("capture %s failed", name)
            rep = CaptureReport(source=name, errors=[f"{type(e).__name__}: {e}"])
        reports.append(rep)
        if rep.skipped:
            say(f"{name}: skipped ({rep.message})")
        else:
            say(
                f"{name}: {rep.enqueued} new, {rep.duplicates} duplicate"
                + (f", errors: {'; '.join(rep.errors)}" if rep.errors else "")
            )
    return reports
