"""`kb` command line (SDD §4, §13). Every command is meant to be run by hand."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import ENV_TEMPLATE, Settings, load_settings, render_config_template
from .git_ops import GitError
from .obs import AlreadyRunning, run_lock, setup_logging
from .queue import Queue

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Personal knowledge base engine.")
auth_app = typer.Typer(no_args_is_help=True, help="One-time authorization flows.")
app.add_typer(auth_app, name="auth")

HomeOpt = Annotated[Path | None, typer.Option("--home", envvar="KB_HOME", help="Engine state dir (default ~/.kb).")]


class _State:
    home: Path | None = None
    verbose: bool = False


_state = _State()


@app.callback()
def main(
    home: HomeOpt = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log to the console too.")] = False,
) -> None:
    _state.home = home
    _state.verbose = verbose


def settings(*, log: bool = True) -> Settings:
    s = load_settings(_state.home)
    if log:
        setup_logging(s.log_dir, verbose=_state.verbose)
    return s


@contextmanager
def locked(s: Settings) -> Iterator[None]:
    """Serialize vault writers (run, tag, digest): they share files and the pending-commit state."""
    try:
        with run_lock(s.lock_path):
            yield
    except AlreadyRunning as e:
        die(f"{e}; wait for it to finish")


def open_queue(s: Settings) -> Queue:
    return Queue(s.db_path)


def echo(msg: str = "") -> None:
    typer.echo(msg)


def die(msg: str, code: int = 1) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


# init -------------------------------------------------------------------------------
@app.command()
def init(
    vault: Annotated[Path | None, typer.Option(help="Vault location (default ~/knowledge).")] = None,
) -> None:
    """Create ~/.kb (config.toml, .env) and the vault skeleton. Safe to re-run."""
    from .config import default_home
    from .init import init_vault

    home = _state.home or default_home()
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "config.toml"
    if not cfg.exists():
        vault_path = (vault or Path.home() / "knowledge").expanduser().resolve()
        cfg.write_text(render_config_template(vault_path), encoding="utf-8")
        echo(f"created {cfg}")
    elif vault is not None:
        echo(f"{cfg} exists; edit vault_path there to move the vault (ignoring --vault)")
    env = home / ".env"
    if not env.exists():
        env.write_text(ENV_TEMPLATE, encoding="utf-8")
        echo(f"created {env} (fill in secrets)")
    s = settings()
    for rel in init_vault(s):
        echo(f"created {s.vault_path / rel}")
    echo(f"vault ready at {s.vault_path}. Next: `kb doctor`.")


# capture ----------------------------------------------------------------------------
@app.command()
def add(
    url: str,
    tags: Annotated[
        list[str] | None, typer.Argument(help="Domain hashtags: ai, de, sd, ... ('#' optional; quote it in shells).")
    ] = None,
    note: Annotated[str | None, typer.Option("--note", "-n", help="Free-text note stored with the item.")] = None,
) -> None:
    """Queue a URL for the next `kb run`."""
    s = settings()
    q = open_queue(s)
    res = q.enqueue(url, "cli", hint_tags=tags or [], note=note)
    if res.status == "invalid":
        die(res.message)
    unknown = [t for t in (tags or []) if t.lstrip("#").lower() not in s.hashtag_map()]
    if unknown:
        typer.secho(
            f"warning: unknown tag(s) {', '.join(unknown)} (the classifier will decide)", fg=typer.colors.YELLOW
        )
    echo(f"{res.status}: #{res.item_id} {res.canonical_url}")


@app.command("backfill-x")
def backfill_x(export: Path) -> None:
    """Import a twitter-web-exporter JSON dump of your bookmarks (one-time)."""
    from .capture import CaptureContext, backfill
    from .pipeline import make_http

    s = settings()
    q = open_queue(s)
    with make_http(s) as http:
        rep = backfill.import_export(export, CaptureContext(s, http, q))
    echo(f"backfill: {rep.enqueued} queued, {rep.duplicates} duplicates, {rep.seen} seen")
    for e in rep.errors:
        typer.secho(f"  {e}", fg=typer.colors.YELLOW)
    echo("Run `kb run` to process them.")


@auth_app.command("x")
def auth_x(
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Print the URL instead of opening it.")] = False,
) -> None:
    """Authorize X bookmark access (OAuth 2.0 PKCE, opens a browser)."""
    from .capture import x_auth

    s = settings()
    q = open_queue(s)
    try:
        who = x_auth.authorize(s, q, open_browser=not no_browser, printer=echo)
    except x_auth.XAuthError as e:
        die(f"X auth failed: {e}")
    echo(f"authorized as @{who}")


# processing -------------------------------------------------------------------------
@app.command()
def run(
    limit: Annotated[int | None, typer.Option(min=1, help="Process at most N items (default: all pending).")] = None,
    no_capture: Annotated[bool, typer.Option("--no-capture", help="Skip polling Telegram and X.")] = False,
    no_commit: Annotated[bool, typer.Option("--no-commit", help="Write files but do not git commit.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Work on a copy of the DB and a temp vault; touch nothing real.")
    ] = False,
) -> None:
    """Capture from Telegram and X, process the queue, commit to the vault."""
    from .pipeline import run as run_pipeline

    s = settings(log=not dry_run)
    skip = None
    if dry_run:
        s = _dry_run_settings(s)
        setup_logging(s.log_dir, verbose=_state.verbose)
        no_capture, no_commit = True, True
        skip = _needs_x_api(s)
        echo(f"dry run: DB copy and vault in {s.home} (LLM calls are real)")
    elif not s.vault_path.exists():
        die(f"vault {s.vault_path} does not exist; run `kb init` first")
    try:
        rep = run_pipeline(s, limit=limit, capture=not no_capture, commit=not no_commit, progress=echo, skip=skip)
    except AlreadyRunning as e:
        die(f"{e}; wait for it to finish")
    except GitError as e:
        die(f"vault git repo unusable, nothing was processed: {e}")
    echo(f"done: {rep.done}, failed: {rep.failed}, processed: {len(rep.outcomes)}")
    if rep.commit_message:
        echo(f"git: {rep.commit_message}")
    if dry_run:
        echo(f"inspect: {s.vault_path}")


def _needs_x_api(s: Settings):
    """X API items are skipped in dry runs: a token refresh rotates the refresh token, and
    the rotated one would land in the throwaway DB copy, breaking the real auth."""

    def skip(item) -> bool:
        if item.source_type != "x":
            return False
        if item.origin != "backfill" or s.x.backfill_fetch_via_api:
            return True
        # Offline only if the stored export record is actually usable; a corrupt one would
        # fall back to the API (and a token refresh) inside the fetcher.
        try:
            return not isinstance(json.loads(item.inline_text or ""), dict)
        except ValueError:
            return True

    return skip


def _dry_run_settings(s: Settings) -> Settings:
    from .init import init_vault

    tmp = Path(tempfile.mkdtemp(prefix="kb-dry-"))
    if s.db_path.exists():
        src = sqlite3.connect(str(s.db_path))
        dst = sqlite3.connect(str(tmp / "kb.sqlite"))
        src.backup(dst)
        src.close()
        dst.close()
    vault = tmp / "knowledge"
    if s.vault_path.exists():
        shutil.copytree(s.vault_path, vault, ignore=shutil.ignore_patterns(".git"))
    d = s.model_copy(update={"home": tmp, "vault_path": vault})
    init_vault(d, commit=False)
    return d


@app.command()
def retry(
    item_id: Annotated[int | None, typer.Argument(help="Item id; omit with --all.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Retry every failed item.")] = False,
) -> None:
    """Reset failed items so the next `kb run` tries them again."""
    if item_id is None and not all_:
        die("give an item id or --all")
    q = open_queue(settings())
    n = q.retry(None if all_ else item_id)
    echo(f"reset {n} item(s); run `kb run`")


@app.command()
def tag(item_id: int, domains: list[str]) -> None:
    """Set an item's domains (primary first). Rewrites front matter only."""
    from .ops import OpError, retag

    s = settings()
    try:
        with locked(s):
            changed = retag(s, open_queue(s), item_id, domains)
    except (OpError, GitError) as e:
        die(str(e))
    echo(f"#{item_id} -> {', '.join(domains)}; updated {len(changed)} file(s)")


@app.command()
def digest(
    day: Annotated[
        str | None,
        typer.Option(
            "--date", help="YYYY-MM-DD (default: every day with activity since the last digest, through today)."
        ),
    ] = None,
    week: Annotated[str | None, typer.Option("--week", help="Write the weekly rollup for YYYY-Www.")] = None,
    no_commit: Annotated[bool, typer.Option("--no-commit")] = False,
) -> None:
    """Write daily digest notes (and the weekly rollup when a week has closed)."""
    from . import digest as digest_mod

    s = settings()
    try:
        d = date.fromisoformat(day) if day else None
    except ValueError:
        die(f"--date must be YYYY-MM-DD, got {day!r}")
    try:
        with locked(s):
            paths = digest_mod.run(s, open_queue(s), day=d, week=week, commit=not no_commit)
    except (GitError, ValueError, FileNotFoundError) as e:
        die(str(e))
    if not paths:
        echo("nothing to digest")
    for p in paths:
        echo(f"wrote {s.vault_path / p}")


# inspection -------------------------------------------------------------------------
@app.command()
def status() -> None:
    """Counts by status, the last 10 errors, compile queue, warnings."""
    from .ops import status as get_status

    s = settings()
    st = get_status(s, open_queue(s))
    echo(f"vault: {s.vault_path}   db: {s.db_path}   model: {s.llm.model}")
    echo(
        "items: " + "  ".join(f"{k}={v}" for k, v in st.counts.items() if v)
        if any(st.counts.values())
        else "items: none yet"
    )
    if st.compile_queue:
        echo("compile queue: " + "  ".join(f"#{k}={v}" for k, v in sorted(st.compile_queue.items())))
    if st.unsorted:
        echo(
            f"unsorted ({len(st.unsorted)}): "
            + ", ".join(f"#{i.id}" for i in st.unsorted)
            + "  -> `kb tag <id> <domain>`"
        )
    echo(f"LLM (30d): {st.llm_calls_30d} calls, ${st.llm_cost_30d:.2f} list-price equivalent")
    if st.pending_git:
        echo(f"uncommitted engine writes: {len(st.pending_git)} (next run retries the commit)")
    if st.errors:
        echo("last errors:")
        for i in st.errors:
            echo(f"  #{i.id} [{i.status}] {i.error_reason or ''} {i.title or i.canonical_url}\n      {i.last_error}")
    for w in st.warnings:
        typer.secho(f"warning: {w}", fg=typer.colors.YELLOW)


@app.command()
def show(item_id: int) -> None:
    """Print one item's DB row."""
    q = open_queue(settings())
    item = q.get(item_id)
    if item is None:
        die(f"no item {item_id}")
    echo(json.dumps({k: v for k, v in vars(item).items() if k != "inline_text"}, indent=2, ensure_ascii=False))


@app.command()
def doctor() -> None:
    """Check every dependency and credential; prints what to fix."""
    from .doctor import checks

    s = settings()
    ok = True
    for name, passed, detail in checks(s):
        ok &= passed or passed is None
        mark = {True: "ok  ", False: "FAIL", None: "--  "}[passed]
        color = {True: typer.colors.GREEN, False: typer.colors.RED, None: typer.colors.YELLOW}[passed]
        typer.secho(f"[{mark}] {name}: {detail}", fg=color)
    raise typer.Exit(0 if ok else 1)


@app.command()
def version() -> None:
    """Print the engine version."""
    echo(__version__)


if __name__ == "__main__":
    app()
