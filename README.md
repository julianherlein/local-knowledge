# kb-engine

A local personal knowledge base. It captures links from **X bookmarks, YouTube, blogs/articles and a
Telegram bot**, stores each one as immutable markdown, tags it with one or two of your **domains**,
writes an English source summary, and queues it for **Claude Code compile sessions** that build an
interlinked wiki (Karpathy's LLM Wiki pattern). You read it in **Obsidian**; a daily digest note is the
main reading surface.

Design spec: [`kb-sdd.md`](kb-sdd.md). Deviations from it are listed at the end.

```
capture ─► kb run ─► raw/<type>/…md ─► wiki/sources/…md ─► wiki/_compile-queue.md ─► digest
(Telegram, X,  (fetch, tag,    (immutable)      (auto summary)      (you + Claude Code
 kb add)        summarize,                                            compile it)
                git commit)
```

Everything runs when you type a command. There is no scheduler.

---

## 1. Install

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), git, and the
[Claude Code CLI](https://claude.com/claude-code) logged in (`claude --version` works). All model calls
go through `claude -p`, so there is no API key and usage counts against your Claude Code plan.

```bash
cd ~/repos/local-knowledge        # this repo
uv sync
uv run kb --help                  # prefix every kb command with `uv run` (or activate .venv)
```

## 2. Set up (once)

```bash
kb init                           # creates ~/.kb/{config.toml,.env} and the vault at ~/knowledge
kb init --vault "D:/My Vault"     # ...or choose where the vault lives (first run only)
kb doctor                         # checks claude, git, yt-dlp, vault, Telegram, X
```

`kb init` is safe to re-run: it only creates missing files (add a domain to `config.toml`, re-run, and
you get its hub and Bases file). The vault is a git repo from the start; commit history is how you
review what the machine did.

Open the vault in Obsidian (**Open folder as vault** -> `~/knowledge`). Plugins, graph colors, daily
notes and bookmarks are preconfigured in `.obsidian/`.

### 2.1 Telegram (capture from your phone)

1. In Telegram, message **@BotFather** -> `/newbot` -> pick a name and a username ending in `bot`.
2. Put the token in `~/.kb/.env`: `TELEGRAM_BOT_TOKEN=123456:ABC...`
3. Optional, keep the bot private: `/setjoingroups` -> Disable.
4. Send your bot any message (a link is fine).
5. `kb doctor` prints `chat 123456789 @you (not in allowed_chat_ids)`.
6. Put that id in `~/.kb/config.toml`: `allowed_chat_ids = [123456789]` under `[telegram]`.
   Nothing is consumed until then, so the message from step 4 is captured by the next `kb run`.

Send links with optional hashtags to force a domain: `https://… #de`. The bot replies with the link's place in the queue, `✓ queued (#3 in queue)`, and once the link is processed it replies to the same message again with `✓ Done! <title> (<domain>)`, or `✗ Failed: <title> (<reason>)` when it gave up (a later `kb retry` that succeeds gets its own `✓ Done!`). Sending a link that is still waiting answers `✓ already queued (#N in queue)` and that message gets the Done reply too. Done replies go out from `kb run` after the vault commit (not `--no-capture` or `--dry-run`; the next capturing run sends them).
Results show up in the digest, not in the chat.

> Telegram keeps undelivered messages for about **24 hours**. Run `kb run` at least daily;
> `kb status` warns after 20h.

### 2.2 X bookmarks

1. [developer.x.com](https://developer.x.com) -> Developer Console -> create a Project and an App.
   **Set a spending limit** (bookmark reads are $0.001, post reads $0.005).
2. App -> *User authentication settings*: permissions **Read**, type **Native App** (public PKCE
   client, no secret; "Web App" also works but then set `X_CLIENT_SECRET`).
3. Callback URI: `http://127.0.0.1:8765/callback` (must equal `x.redirect_uri` in config.toml).
   Website URL: anything.
4. *Keys and tokens* -> copy the **OAuth 2.0 Client ID** (not the API key) into `~/.kb/.env` as
   `X_CLIENT_ID=...`.
5. `kb auth x` (opens a browser; `--no-browser` prints the URL). The refresh token goes to the OS
   keyring (Windows Credential Manager), falling back to the local DB.

History (the API only returns ~800 bookmarks): install the
[twitter-web-exporter](https://github.com/prinsss/twitter-web-exporter) userscript, open your Bookmarks
page, *Export Data* -> JSON with **Include all metadata**, then:

```bash
kb backfill-x path/to/export.json
```

Backfilled posts are rendered from the export (free, no API calls). Threads older than 7 days cannot
be unrolled by the API and are marked `thread: incomplete`.

## 3. Daily use

| Command | What it does |
|---|---|
| `kb add <url> [tags…] [-n "note"]` | Queue a link. Tags: `ai`, `de`, `sd`, `swe`, `tennis` or full domain names; `#` optional (quote it in shells: `"#ai"`). |
| `kb run` | Poll Telegram + X, process everything pending (fetch, tag, write raw, summarize, compile-queue line), refresh today's digest, git commit. `--limit N`, `--no-capture`, `--no-commit`, `--dry-run`. |
| `kb digest` | Write daily digest notes for every day with activity since the last digest (and the weekly rollup once a week closes). `--date YYYY-MM-DD`, `--week 2026-W39`. |
| `kb status` | Counts by status, last 10 errors, unsorted items, compile-queue size per domain, LLM usage, warnings. |
| `kb tag <id> <domain> [domain2]` | Fix an item's domains (primary first). Rewrites front matter only; files never move. |
| `kb retry <id>` / `kb retry --all` | Re-queue failed items. |
| `kb show <id>` | Print one item's DB row. |
| `kb doctor` | Check dependencies and credentials. |

A typical morning: `kb run`, then open today's daily note in Obsidian (the Daily notes button opens
the digest).

`kb run --dry-run` copies the DB and vault to a temp dir and processes there (real LLM calls, no
capture, no commit, X API items skipped so a token refresh can never break your real auth). Use it to
preview a prompt or config change.

### Failures

Nothing is dropped silently. Transient failures (timeouts, 5xx, rate limits) retry on the next run, up
to 3 attempts. 404/410/401, paywalls/JS-only pages (`extraction_empty`), PDFs, and videos without
subtitles go straight to `failed_permanent`. All of them appear in `kb status` and the digest's
*Needs attention* section.

## 4. Compile sessions (building the wiki)

Automated steps never touch `wiki/concepts`, `wiki/entities` or `wiki/syntheses`. You grow those in
Claude Code sessions inside the vault, which follow the vault's `CLAUDE.md` (the operating manual:
page templates, citation and verification rules, hubs, log format, git discipline).

```bash
cd ~/knowledge && claude
```

Standard prompts:

- **Compile:** `Process unchecked items in wiki/_compile-queue.md tagged #data-engineering.`
- **Cross-domain:** `Find concepts shared between #system-design and #data-engineering sources that aren't linked yet.`
- **Lint:** `Find contradictions, orphan pages, concepts mentioned in >=2 sources without a page, and stale syntheses. Report first, then fix after I confirm.`
- **Query:** `Answer <question> from the wiki, citing pages; if the answer is novel, propose a syntheses/ page.`

The session shows you the diff and commits `compile: <domain> <date>`. Engine commits are `auto: …`,
so `git log --oneline` separates what the machine did from what you approved. Suggested cadence:
compile 1-2x/week per active domain, lint monthly.

## 5. Configuration

`~/.kb/config.toml` (created by `kb init`, commented) and `~/.kb/.env` (secrets). Environment
variables override both (`KB_LLM__MODEL=…`, `KB_HOME=…`).

| Key | Default | Notes |
|---|---|---|
| `vault_path` | `~/knowledge` | |
| `llm.model` | `claude-opus-5-5` | Best model by default; `claude-haiku-4-5-20251001` is faster and lighter on quota. |
| `llm.max_input_tokens` | 40000 | Longer sources are cut to head + tail (CJK counted as 1 token/char). |
| `tagger.threshold` | 0.6 | Below it, items without a hashtag are tagged `unsorted`. |
| `run.max_items` | unset (all) | Items per `kb run`. |
| `telegram.allowed_chat_ids` | `[]` | Messages from other chats are ignored. |
| `x.use_keyring` | true | |
| `x.backfill_fetch_via_api` | false | true re-fetches every backfilled post ($0.005 each). |
| `[[domains]]` | 5 defaults | `name`, `description` (steers the classifier), `hashtags`. |

State lives outside the vault: `~/.kb/kb.sqlite` (queue, tokens fallback, LLM call log),
`~/.kb/logs/kb.log` (JSON lines, rotated).

## 6. How it is built

```
services/llm/          kb-llm: the only LLM access point (claude -p wrapper + contract + fake)
src/kb/
  cli.py               typer CLI
  config.py            pydantic-settings: config.toml + .env + env
  queue.py             SQLite items/state/llm_calls/runs; status machine
  normalize.py         canonical URLs, dedup keys, URL/hashtag extraction
  pipeline.py          capture -> fetch -> tag -> write -> summarize -> queue -> commit
  fetchers/            web (httpx + trafilatura), youtube (yt-dlp + VTT cleaning), x, inline (Telegram text)
  capture/             telegram, x_auth, x_api, x_bookmarks, backfill
  tagger.py            hashtag override + LLM multi-label classifier
  summarizer.py        structured-output summary, rendered deterministically
  writer.py / fm.py    raw + summary files, flat Obsidian-safe front matter
  git_ops.py / vault.py  atomic writes; auto-commit that never includes your manual edits
  digest.py / init.py  digests; vault skeleton, vault CLAUDE.md, hubs, Bases, .obsidian
  templates/           vault templates
evals/                 paid LLM evals (classifier + summarizer)
tests/                 pytest, respx-mocked HTTP, no network
```

Guarantees worth knowing, each covered by tests:

- **Resumable:** every step persists before the next; a crash or Ctrl-C resumes where it stopped.
  File names are reserved in the DB before writing, so a retry never overwrites another item.
- **Your edits are never auto-committed:** before every write the engine compares the file with
  HEAD (git blob ids, at write time); a file you edited is left out of the `auto:` commit and named
  in the run output. The rest of the ingest is still committed.
- **Serialized:** `kb run`, `kb tag` and `kb digest` share a lock.
- **raw/ is immutable:** only `kb tag` rewrites its front matter; the body bytes never change.
  Inline `#hashtags` in source text are escaped (`\#`) so a tweet saying `#tennis` cannot tag a file.
- **Prompt-injection resistant:** source text is fenced as untrusted data in both prompts; the eval
  suite has injection canaries.

## 7. Tests and evals

```bash
uv run pytest -m "not slow and not live"   # gate lane (pre-commit hook), no git/network
uv run pytest -m "not live"                # everything deterministic, incl. real-git tests (~1 min on Windows)
KB_LIVE_NET=1 uv run pytest -m live        # real web pages / YouTube
KB_LIVE_LLM=1 uv run pytest -m live services/llm
uv run python -m evals.run                 # paid, on Haiku by default: classifier accuracy (>=85%) + summary checks
uv run python -m evals.run --model claude-opus-5-5   # before switching llm.model to another model
```

Enable the hook once per clone: `git config core.hooksPath .githooks`.

After changing a prompt or the model, run the evals; results land in `evals/results/` (git-ignored).

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `vault git repo unusable … dubious ownership` | `git config --global --add safe.directory <vault path>` |
| YouTube items fail with `ytdlp_error` | `uv lock --upgrade-package yt-dlp && uv sync` (YouTube breaks yt-dlp periodically) |
| Telegram `409` in `kb doctor` | A webhook is set: open `https://api.telegram.org/bot<token>/deleteWebhook` |
| X `re-run kb auth x` | Refresh token expired or revoked; run `kb auth x` |
| `another kb process holds …kb.lock` | Another `kb run`/`tag`/`digest` is running; wait |
| Run output says `left uncommitted, manual edits: …` | You (or Obsidian) edited that file; commit it yourself when ready |

## 9. Deviations from the SDD

| SDD | Here | Why |
|---|---|---|
| `anthropic` SDK, Haiku, `ANTHROPIC_API_KEY` | Local `claude -p` via `services/llm`, best model by default | No API key; model is one config line |
| launchd/cron every 10 min, digest at 07:00 | Manual commands; `kb run` processes all pending and refreshes today's digest; `kb digest` catches up missed days; weekly rollup written for the latest closed week | Runs are manual by choice |
| Links like `[[sources/<slug>]]` | Full vault paths `[[wiki/sources/<slug>]]` | Unambiguous without Obsidian suffix matching |
| Skip the whole commit if manual edits exist | Commit engine files, leave only the hand-edited paths uncommitted | One edited file shouldn't block an ingest |
| `telegram://<msg_id>` | `telegram://<chat_id>/<msg_id>` | Message ids are only unique per chat |
