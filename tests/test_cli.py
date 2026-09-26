"""CLI wiring: commands parse, call the right layer, and print what a user needs."""

import pytest
from typer.testing import CliRunner

from kb.cli import app
from kb.queue import Queue

runner = CliRunner()


@pytest.fixture
def home(settings, tmp_path):
    cfg = settings.home / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f'vault_path = "{settings.vault_path.as_posix()}"\n[telegram]\nenabled = false\n[x]\nenabled = false\n',
        encoding="utf-8",
    )
    return settings.home


def kb(home, *args):
    return runner.invoke(app, ["--home", str(home), *args], catch_exceptions=False)


def test_add_with_bare_and_hash_tags(home, settings):
    r = kb(home, "add", "https://youtu.be/dQw4w9WgXcQ?si=x", "ai", "#SD", "--note", "watch later")
    assert r.exit_code == 0, r.output
    assert "queued: #1 https://youtube.com/watch?v=dQw4w9WgXcQ" in r.output
    q = Queue(settings.db_path)
    item = q.get(1)
    assert item.hint_tags == ["ai", "sd"] and item.note == "watch later" and item.origin == "cli"
    q.close()
    r = kb(home, "add", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert "duplicate: #1" in r.output


def test_add_warns_on_unknown_tag_and_rejects_bad_url(home):
    r = kb(home, "add", "https://example.com/a", "cooking")
    assert r.exit_code == 0 and "unknown tag(s) cooking" in r.output
    r = runner.invoke(app, ["--home", str(home), "add", "not a url"])
    assert r.exit_code == 1


def test_status_retry_show(home, settings):
    q = Queue(settings.db_path)
    iid = q.enqueue("https://example.com/gone", "cli").item_id
    q.fail(iid, "HTTP 404 Not Found", permanent=True, reason="http_404", max_attempts=3)
    q.close()
    r = kb(home, "status")
    assert r.exit_code == 0
    assert "failed_permanent=1" in r.output and "http_404" in r.output and "HTTP 404 Not Found" in r.output
    r = kb(home, "retry")
    assert r.exit_code == 1
    r = kb(home, "retry", str(iid))
    assert "reset 1 item(s)" in r.output
    r = kb(home, "show", str(iid))
    assert '"status": "failed"' in r.output and '"attempts": 0' in r.output


def test_tag_rejects_unknown_domain(home, settings):
    q = Queue(settings.db_path)
    iid = q.enqueue("https://example.com/a", "cli").item_id
    q.close()
    r = runner.invoke(app, ["--home", str(home), "tag", str(iid), "cooking"])
    assert r.exit_code == 1 and "unknown domain" in r.output


def test_run_requires_vault(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text(f'vault_path = "{(tmp_path / "nope").as_posix()}"\n', encoding="utf-8")
    r = runner.invoke(app, ["--home", str(home), "run", "--no-capture"])
    assert r.exit_code == 1 and "kb init" in r.output


def test_run_with_nothing_pending(home):
    r = kb(home, "run", "--no-capture")
    assert r.exit_code == 0 and "done: 0, failed: 0" in r.output
