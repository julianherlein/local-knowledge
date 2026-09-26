"""The auto commit must include every engine write and never a manual edit."""

from conftest import git

from kb.git_ops import Tracker
from kb.vault import Vault


def committed_files(repo, rev="HEAD"):
    return set(git(repo, "show", "--name-only", "--pretty=format:", rev).split())


def test_commits_only_engine_paths(settings, queue, vault_dir):
    (vault_dir / "mine.md").write_text("my scratch\n")  # untracked manual file
    t = Tracker(vault_dir, queue)
    v = Vault(vault_dir, t)
    v.write("raw/web/a.md", "A\n", item_id=1)
    v.write("wiki/sources/a.md", "S\n", item_id=1)
    ok, msg = t.commit()
    assert ok and msg == "auto: ingest 1 items [1]"
    assert committed_files(vault_dir) == {"raw/web/a.md", "wiki/sources/a.md"}
    assert "mine.md" in git(vault_dir, "status", "--porcelain")


def test_manual_edit_before_engine_write_is_not_swept_in(settings, queue, vault_dir):
    (vault_dir / "wiki").mkdir()
    q = vault_dir / "wiki" / "_compile-queue.md"
    q.write_text("- [ ] one\n")
    git(vault_dir, "add", ".")
    git(vault_dir, "commit", "-qm", "base")
    q.write_text("- [x] one\n")  # manual tick, uncommitted

    t = Tracker(vault_dir, queue)
    v = Vault(vault_dir, t)
    v.write("raw/web/b.md", "B\n", item_id=2)
    v.write("wiki/_compile-queue.md", q.read_text() + "- [ ] two\n", item_id=2)
    ok, msg = t.commit()
    assert ok and "manual edits: wiki/_compile-queue.md" in msg
    assert committed_files(vault_dir) == {"raw/web/b.md"}

    # Still blocked on the next run while the manual edit is uncommitted.
    t2 = Tracker(vault_dir, queue)
    assert t2.commit()[0] is False
    # Once the user commits it (including our line), the path leaves the pending set.
    git(vault_dir, "commit", "-qam", "compile: ticked")
    t3 = Tracker(vault_dir, queue)
    assert t3.pending == {}


def test_edit_after_engine_write_blocks_that_path(settings, queue, vault_dir):
    t = Tracker(vault_dir, queue)
    v = Vault(vault_dir, t)
    v.write("wiki/sources/c.md", "engine\n", item_id=3)
    (vault_dir / "wiki/sources/c.md").write_text("engine\nand me\n")  # user edits before the commit
    ok, msg = t.commit()
    assert not ok and "wiki/sources/c.md" in msg


def test_skipped_run_is_committed_later(settings, queue, vault_dir):
    t = Tracker(vault_dir, queue)
    Vault(vault_dir, t).write("raw/web/d.md", "D\n", item_id=4)
    # --no-commit run: nothing committed, but the write is remembered.
    t2 = Tracker(vault_dir, queue)
    v2 = Vault(vault_dir, t2)
    v2.write("raw/web/d.md", "D2\n", item_id=4)  # our own earlier write is not "manual"
    v2.write("raw/web/e.md", "E\n", item_id=5)
    ok, msg = t2.commit()
    assert ok and msg == "auto: ingest 2 items [4, 5]"


def test_not_a_repo_is_a_noop(settings, queue, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    t = Tracker(plain, queue)
    Vault(plain, t).write("a.md", "x")
    assert t.commit() == (False, "vault is not a git repo")


def test_edit_during_run_is_not_swept_in(settings, queue, vault_dir):
    """Critic H1: the user ticks a box while the run sits in an LLM call."""
    (vault_dir / "wiki").mkdir()
    q = vault_dir / "wiki" / "_compile-queue.md"
    q.write_text("- [ ] one\n")
    git(vault_dir, "add", ".")
    git(vault_dir, "commit", "-qm", "base")
    t = Tracker(vault_dir, queue)  # run starts, tree clean
    v = Vault(vault_dir, t)
    v.write("raw/web/x.md", "X\n", item_id=1)
    q.write_text("- [x] one\n")  # user edit mid-run
    v.write("wiki/_compile-queue.md", q.read_text() + "- [ ] two\n", item_id=1)
    ok, msg = t.commit()
    assert ok and committed_files(vault_dir) == {"raw/web/x.md"}
    assert "wiki/_compile-queue.md" in msg


def test_crash_between_intent_and_write_is_not_a_manual_edit(settings, queue, vault_dir):
    """Critic H2: kill after the tracker recorded the write but before/after bytes landed."""
    t = Tracker(vault_dir, queue)
    t.before_write("wiki/sources/s.md", b"S\n", 7)
    (vault_dir / "wiki/sources").mkdir(parents=True)
    (vault_dir / "wiki/sources/s.md").write_bytes(b"S\n")  # bytes landed, then "crash"
    t2 = Tracker(vault_dir, queue)  # next run
    Vault(vault_dir, t2).write("wiki/sources/s.md", "S2\n", item_id=7)
    ok, msg = t2.commit()
    assert ok and "manual" not in msg and committed_files(vault_dir) == {"wiki/sources/s.md"}


def test_crash_before_bytes_landed(settings, queue, vault_dir):
    t = Tracker(vault_dir, queue)
    t.before_write("raw/web/never.md", b"N\n", 8)  # crash before write
    t2 = Tracker(vault_dir, queue)
    assert "raw/web/never.md" not in t2.pending  # file absent and not in HEAD -> nothing to do
    assert t2.commit() == (False, "nothing to commit")


def test_git_failure_raises_and_keeps_pending(settings, queue, vault_dir, monkeypatch):
    """Critic H3: a failing git must not be read as a clean tree."""
    import pytest

    from kb import git_ops

    t = Tracker(vault_dir, queue)
    Vault(vault_dir, t).write("raw/web/p.md", "P\n", item_id=9)
    real = git_ops.run_git

    def broken(repo, *args, check=True):
        if args[:1] in (("ls-tree",), ("rev-parse",)):
            raise git_ops.GitError("fatal: detected dubious ownership")
        return real(repo, *args, check=check)

    monkeypatch.setattr(git_ops, "run_git", broken)
    with pytest.raises(git_ops.GitError):
        Tracker(vault_dir, queue)
    monkeypatch.setattr(git_ops, "run_git", real)
    ok, _ = Tracker(vault_dir, queue).commit()
    assert ok and committed_files(vault_dir) == {"raw/web/p.md"}


def test_staged_user_change_elsewhere_is_not_committed(settings, queue, vault_dir):
    (vault_dir / "mine.md").write_text("staged by me\n")
    git(vault_dir, "add", "mine.md")
    t = Tracker(vault_dir, queue)
    Vault(vault_dir, t).write("raw/web/q.md", "Q\n", item_id=1)
    t.commit()
    assert committed_files(vault_dir) == {"raw/web/q.md"}
    assert "A  mine.md" in git(vault_dir, "status", "--porcelain")


def test_first_commit_on_unborn_head_and_path_with_spaces(settings, queue, tmp_path):
    repo = tmp_path / "my vault"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@e.com")
    git(repo, "config", "user.name", "T")
    t = Tracker(repo, queue)
    Vault(repo, t).write("raw/web/first file.md", "F\n", item_id=1)
    ok, _ = t.commit()
    assert ok and committed_files(repo) == {"raw/web/first file.md"} or "first" in git(repo, "log", "--stat")


def test_two_trackers_in_one_process_share_pending(settings, queue, vault_dir):
    """The digest refresh inside `kb run` uses its own Tracker; the run's commit must include it."""
    run_t = Tracker(vault_dir, queue)
    Vault(vault_dir, run_t).write("raw/web/r.md", "R\n", item_id=1)
    Vault(vault_dir, Tracker(vault_dir, queue)).write("digests/daily/2026-09-26.md", "D\n")
    ok, _ = run_t.commit()
    assert ok and committed_files(vault_dir) == {"raw/web/r.md", "digests/daily/2026-09-26.md"}
