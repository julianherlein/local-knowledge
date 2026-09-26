"""End-to-end pipeline with a fake fetcher and fake LLM: state machine, files, commit, crash resume."""

from datetime import date

import pytest
from conftest import classifier_or_summary, git
from kb_llm import FakeLLMClient, LLMError

from kb import fm, pipeline
from kb.models import FetchedItem, FetchError
from kb.ops import OpError, retag
from kb.queue import FAILED, FAILED_PERMANENT


def fake_fetch(item, ctx):
    return FetchedItem(
        source_type=item.source_type,
        url=item.canonical_url,
        title=f"Why we moved off Airflow {item.id}",
        body="# Intro\n\nWe moved. Failures dropped 40%.",
        author="Jane",
        published=date(2026, 9, 20),
    )


@pytest.fixture
def patched_fetch(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)


def run(settings, queue, llm, **kw):
    return pipeline.run(settings, queue=queue, llm=llm, capture=False, **kw)


def test_full_run_produces_raw_summary_queue_and_commit(settings, queue, fake_llm, patched_fetch, vault_dir):
    iid = queue.enqueue("https://example.com/airflow", "cli", hint_tags=["de"]).item_id
    rep = run(settings, queue, fake_llm)
    assert rep.done == 1 and rep.failed == 0
    item = queue.get(iid)
    assert item.status == "done" and item.done_at
    assert item.domains == ["data-engineering"] and item.tag_method == "hashtag"
    assert item.raw_path.startswith("raw/web/") and item.raw_path.endswith("-why-we-moved-off-airflow-1.md")
    assert item.summary_path == "wiki/sources/" + item.raw_path.rsplit("/", 1)[1]
    assert queue.load_payload(iid) is None

    raw_meta, raw_body = fm.split((vault_dir / item.raw_path).read_text(encoding="utf-8"))
    assert raw_meta["id"] == iid and raw_meta["domains"] == ["data-engineering"]
    assert "Failures dropped 40%" in raw_body

    s_meta, s_body = fm.split((vault_dir / item.summary_path).read_text(encoding="utf-8"))
    assert s_meta["source"] == f"[[{item.raw_path[:-3]}]]" and s_meta["status"] == "uncompiled"
    assert "**TL;DR:** The team replaced Airflow with Dagster. It cut failures by 40%." in s_body

    cq = (vault_dir / "wiki/_compile-queue.md").read_text(encoding="utf-8")
    stem = item.summary_path.rsplit("/", 1)[1][:-3]
    assert f"- [ ] [[wiki/sources/{stem}]] #data-engineering — The team replaced" in cq

    assert rep.committed and rep.commit_message == f"auto: ingest 1 items [{iid}]"
    assert git(vault_dir, "status", "--porcelain") == ""
    calls = queue.conn.execute("SELECT purpose FROM llm_calls ORDER BY id").fetchall()
    assert [r["purpose"] for r in calls] == ["classify", "summarize"]


def test_transient_failure_resumes_from_last_step(settings, queue, patched_fetch):
    iid = queue.enqueue("https://example.com/a", "cli").item_id
    responder = classifier_or_summary()
    state = {"fail": True}

    def flaky(req):
        if "confidence" not in req.json_schema["properties"] and state["fail"]:
            return LLMError("rate limited")
        return responder(req)

    llm = FakeLLMClient(flaky)
    rep = run(settings, queue, llm)
    item = queue.get(iid)
    assert rep.failed == 1 and item.status == FAILED and item.resume_from == "written" and item.attempts == 1
    raw_path = item.raw_path

    state["fail"] = False
    llm.calls.clear()
    rep = run(settings, queue, llm)
    item = queue.get(iid)
    assert item.status == "done" and item.raw_path == raw_path
    assert len(llm.calls) == 1  # only the summary was redone, no re-classify
    assert not (settings.vault_path / (raw_path[:-3] + "-2.md")).exists()


def test_permanent_fetch_error(settings, queue, fake_llm, monkeypatch):
    def gone(item, ctx):
        raise FetchError("HTTP 404", permanent=True, reason="http_404")

    monkeypatch.setattr(pipeline, "fetch", gone)
    iid = queue.enqueue("https://example.com/gone", "cli").item_id
    rep = run(settings, queue, fake_llm)
    item = queue.get(iid)
    assert item.status == FAILED_PERMANENT and item.error_reason == "http_404" and rep.failed == 1
    assert fake_llm.calls == []


def test_unexpected_exception_is_retried_then_permanent(settings, queue, fake_llm, monkeypatch):
    def boom(item, ctx):
        raise ValueError("parser bug")

    monkeypatch.setattr(pipeline, "fetch", boom)
    iid = queue.enqueue("https://example.com/x", "cli").item_id
    for _ in range(3):
        run(settings, queue, fake_llm)
    item = queue.get(iid)
    assert item.status == FAILED_PERMANENT and item.attempts == 3 and "parser bug" in item.last_error


def test_crash_between_steps_resumes(settings, queue, fake_llm, patched_fetch):
    iid = queue.enqueue("https://example.com/a", "cli").item_id
    pipe = pipeline.Pipeline(settings, queue, fake_llm, None, pipeline.Vault(settings.vault_path))
    pipe.step_fetch(queue.get(iid))
    pipe.step_tag(queue.get(iid))
    assert queue.get(iid).status == "tagged"
    # "crash" here; next run continues from tagged
    run(settings, queue, fake_llm)
    assert queue.get(iid).status == "done"


def test_same_title_gets_unique_stems(settings, queue, fake_llm, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "fetch",
        lambda item, ctx: FetchedItem(source_type="web", url=item.canonical_url, title="Same", body="b"),
    )
    a = queue.enqueue("https://example.com/1", "cli").item_id
    b = queue.enqueue("https://example.com/2", "cli").item_id
    run(settings, queue, fake_llm)
    pa, pb = queue.get(a).raw_path, queue.get(b).raw_path
    assert pa != pb and pb.endswith("-same-2.md")


def test_limit_and_no_commit(settings, queue, fake_llm, patched_fetch, vault_dir):
    for n in range(3):
        queue.enqueue(f"https://example.com/{n}", "cli")
    rep = run(settings, queue, fake_llm, limit=2, commit=False)
    assert len(rep.outcomes) == 2 and not rep.committed
    assert git(vault_dir, "status", "--porcelain") != ""
    rep = run(settings, queue, fake_llm)
    assert rep.committed and "3 items" in rep.commit_message  # earlier writes included


def test_unsorted_then_retag(settings, queue, patched_fetch, vault_dir):
    llm = FakeLLMClient(classifier_or_summary(domains=("tennis",), confidence=0.3))
    iid = queue.enqueue("https://example.com/u", "cli").item_id
    run(settings, queue, llm)
    item = queue.get(iid)
    assert item.domains == ["unsorted"] and item.tag_method == "fallback"
    raw_before = (vault_dir / item.raw_path).read_text(encoding="utf-8")

    retag(settings, queue, iid, ["#sd", "tennis"])
    item = queue.get(iid)
    assert item.domains == ["system-design", "tennis"] and item.tag_method == "manual"
    raw_after = (vault_dir / item.raw_path).read_text(encoding="utf-8")
    assert fm.split(raw_after)[1] == fm.split(raw_before)[1]  # body untouched
    assert fm.split(raw_after)[0]["tags"] == ["raw", "system-design", "tennis"]
    s_meta = fm.split((vault_dir / item.summary_path).read_text(encoding="utf-8"))[0]
    assert s_meta["domains"] == ["system-design", "tennis"] and s_meta["tags"] == ["source", "system-design", "tennis"]
    assert "#system-design #tennis —" in (vault_dir / "wiki/_compile-queue.md").read_text(encoding="utf-8")
    assert "auto: retag 1 items" in git(vault_dir, "log", "-1", "--pretty=%s")
    assert git(vault_dir, "status", "--porcelain") == ""


def test_retag_validates(settings, queue):
    iid = queue.enqueue("https://example.com/u", "cli").item_id
    with pytest.raises(OpError):
        retag(settings, queue, iid, ["cooking"])
    with pytest.raises(OpError):
        retag(settings, queue, iid, ["ai", "sd", "de"])
    with pytest.raises(OpError):
        retag(settings, queue, 999, ["ai"])


def test_lock_prevents_overlapping_runs(settings, queue, fake_llm):
    from kb.obs import AlreadyRunning, run_lock

    with run_lock(settings.lock_path), pytest.raises(AlreadyRunning):
        run(settings, queue, fake_llm)


def test_failed_write_keeps_its_stem_reserved(settings, queue, fake_llm, monkeypatch):
    """Critic C1: A fails inside the raw write, B with the same title must not take A's name."""
    monkeypatch.setattr(
        pipeline,
        "fetch",
        lambda item, ctx: FetchedItem(source_type="web", url=item.canonical_url, title="Home", body=f"body {item.id}"),
    )
    a = queue.enqueue("https://example.com/a", "cli").item_id
    real_write = pipeline.Vault.write
    calls = {"n": 0}

    def flaky(self, rel, text, item_id=None):
        if rel.startswith("raw/") and item_id == a and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("file locked by antivirus")
        return real_write(self, rel, text, item_id)

    monkeypatch.setattr(pipeline.Vault, "write", flaky)
    run(settings, queue, fake_llm)
    assert queue.get(a).status == FAILED
    b = queue.enqueue("https://example.com/b", "cli").item_id
    run(settings, queue, fake_llm)
    pa, pb = queue.get(a).raw_path, queue.get(b).raw_path
    assert queue.get(a).status == queue.get(b).status == "done"
    assert pa != pb
    assert f"body {b}" in (settings.vault_path / pb).read_text(encoding="utf-8")
    assert f"body {a}" in (settings.vault_path / pa).read_text(encoding="utf-8")


def test_manual_tag_before_tagging_step_sticks(settings, queue, fake_llm, patched_fetch):
    """Critic M4: kb tag on a queued item must not be overwritten by the classifier."""
    iid = queue.enqueue("https://example.com/m", "cli").item_id
    retag(settings, queue, iid, ["tennis"])
    run(settings, queue, fake_llm)
    item = queue.get(iid)
    assert item.domains == ["tennis"] and item.tag_method == "manual"
    assert [
        c.json_schema["properties"].get("confidence")
        for c in fake_llm.calls
        if "confidence" in c.json_schema["properties"]
    ] == []


def test_commit_failure_is_reported_not_raised(settings, queue, fake_llm, patched_fetch, vault_dir):
    """Critic M5: a failing commit (hook, gpgsign) keeps the run's report and retries later."""
    hook = vault_dir / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    queue.enqueue("https://example.com/h", "cli")
    rep = run(settings, queue, fake_llm)
    assert rep.done == 1 and not rep.committed and rep.commit_message.startswith("commit failed")
    row = queue.conn.execute("SELECT finished_at, notes FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["finished_at"] and "commit failed" in row["notes"]
    hook.unlink()
    rep = run(settings, queue, fake_llm)
    assert rep.committed


def test_skip_filter(settings, queue, fake_llm, patched_fetch):
    a = queue.enqueue("https://x.com/u/status/1", "x_bookmark").item_id
    b = queue.enqueue("https://example.com/w", "cli").item_id
    rep = run(settings, queue, fake_llm, skip=lambda i: i.source_type == "x")
    assert [o.item_id for o in rep.outcomes] == [b]
    assert queue.get(a).status == "queued"


def test_run_refreshes_todays_digest_in_same_commit(settings, queue, fake_llm, patched_fetch, vault_dir):
    from datetime import date as _date

    queue.enqueue("https://example.com/d", "cli")
    rep = run(settings, queue, fake_llm)
    note = vault_dir / "digests" / "daily" / f"{_date.today().isoformat()}.md"
    assert rep.committed and note.exists()
    assert git(vault_dir, "status", "--porcelain") == ""
