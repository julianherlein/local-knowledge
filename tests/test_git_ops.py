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
