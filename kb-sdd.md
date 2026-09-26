# Personal Knowledge Base — Software Design Document (v1)

| | |
|---|---|
| **Owner** | Julian |
| **Status** | Draft v1.2 (single vault + Obsidian integration) |
| **Date** | 2026-09-26 |
| **Scope** | Initial buildable version (local, single user) |

---

## 1. Summary

A local system that captures content from **X bookmarks, YouTube, blogs/articles and a Telegram inbox**, stores it as immutable markdown, tags each item with one or more of **five domains** inside a **single vault**, and produces an automated per-source summary. Once that is done, **Claude Code sessions** compile the sources into an interlinked wiki, following Karpathy's LLM Wiki pattern. A daily/weekly **digest note** is the main reading surface.

The design principle: **capture must be frictionless and cheap; compilation must be deliberate and reviewable.** Fetching, routing and summarizing run automatically. Synthesis across sources (concept pages, entity pages, cross-links) runs in supervised sessions whose edits are reviewed through git diffs.

## 2. Goals and non-goals

### Goals (v1)
- G1. Capture an item in ≤ 2 actions from phone or desktop: bookmark on X, or send a link to Telegram.
- G2. Every captured item ends up as a clean markdown file in `raw/`, with a source summary page in `wiki/sources/`.
- G3. Nothing is lost silently. Failures are visible in `kb status` and in the digest.
- G4. Items are tagged with domains automatically (multi-label), with a manual hashtag override.
- G5. A daily digest (and weekly rollup), grouped by domain, lists what was ingested, with summaries.
- G6. Every change to the vault is versioned in git.
- G7. Running cost stays under $10/month (LLM + X API).

### Non-goals (v1)
- Vector search / RAG, graph databases, embeddings.
- Automatic cross-source synthesis without review. Concept and entity pages are built in Claude Code sessions.
- Multi-user use, a web UI, or a mobile app.
- Real-time ingestion. Minutes of latency are fine.
- Local speech-to-text (Whisper) for videos without subtitles. This is deferred to Phase 2.

## 3. Key decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Runs on the local machine, scheduled with cron/launchd | Simplest setup, and the vault lives there. All inputs are pull-based, so no public endpoint is needed. |
| D2 | All inputs are **polled**: X bookmarks API + Telegram `getUpdates` | The X Activity API has no bookmark event. Telegram holds undelivered updates for ~24h, so the machine only needs to be awake daily. |
| D3 | Queue and state live in **SQLite** | Zero ops, transactional, trivially inspectable. |
| D4 | **Hybrid compilation**: automated per-source summary + manual Claude Code sessions for synthesis/lint | Keeps cost low and quality high, and keeps the LLM from silently rewriting the wiki. |
| D5 | **English** for all wiki output, regardless of source language | Consistency for linking and search. |
| D6 | **One vault for everything**; domains (`data-engineering`, `ai-llms`, `software`, `tennis`, `system-design`) are **tags + hub pages**, not folders | Concept pages are shared across domains, which is where the valuable links are. Index size is handled with per-domain hub pages (§9) and a later search layer. |
| D7 | Domain tagging is **LLM multi-label classification** (1–2 domains), with a **hashtag override** | Low friction. An item can legitimately belong to two domains, so there is no misrouting, only imperfect tags. |
| D8 | **Git** repo for the vault; automated and manual changes committed separately | Lets you review and roll back what the LLM did. |
| D9 | Automated LLM calls use a small, cheap model (Haiku-class), configurable | Budget below $10/month (see §12). |

## 4. Architecture

```
 ┌──────────────── CAPTURE ────────────────┐
 │  X bookmark ──► X API poller            │
 │  Telegram msg ─► Telegram poller        │──► enqueue(url, hint_tag)
 │  Terminal ────► `kb add <url> [#tag]`   │
 └─────────────────────────────────────────┘
                     │
                     ▼
            ┌──────────────────┐
            │ SQLite: items    │  status machine (§6)
            └──────────────────┘
                     │  `kb run` (cron, every 10 min)
                     ▼
 ┌──────────── PROCESS (automated) ────────────┐
 │ 1. Normalize URL + dedup                    │
 │ 2. Fetch  → x | youtube | web fetcher        │
 │ 3. Tag    → hashtag? else LLM classifier     │
 │ 4. Write  → raw/<type>/…md                   │
 │ 5. Summarize → wiki/sources/…md              │
 │ 6. Append to wiki/_compile-queue.md          │
 │ 7. git commit "auto: ingest N items"         │
 └─────────────────────────────────────────────┘
                     │
         ┌───────────┴────────────┐
         ▼                        ▼
 `kb digest` (daily 07:00)   Claude Code session (manual)
 digests/…md                 "compile the queue" / "lint"
                             → concepts/, entities/, syntheses/
                             → git commit "compile: …"
```

### Components

| Component | Responsibility |
|---|---|
| `capture/x_bookmarks.py` | Polls `GET /2/users/{id}/bookmarks` and enqueues new post IDs |
| `capture/telegram.py` | Long-polls `getUpdates`, extracts URLs + hashtags, and replies with a short ack |
| `cli.py` | `kb add / run / digest / status / retry / backfill-x` |
| `queue.py` | SQLite access, status transitions, retries |
| `normalize.py` | Canonical URLs, source-type detection, dedup keys |
| `fetchers/{x,youtube,web}.py` | Turn a URL into a `FetchedItem` (text + metadata) |
| `tagger.py` | Hashtag map first, then multi-label classifier; `unsorted` tag if confidence is low |
| `writer.py` | Renders raw markdown with front matter |
| `summarizer.py` | One LLM call per item that produces the source summary page |
| `init.py` | `kb init`: creates the vault skeleton, `CLAUDE.md`, hub pages, `.base` files and `.obsidian/` config |
| `digest.py` | Builds daily/weekly digest notes from the DB (no LLM needed) |
| `git_ops.py` | Commits to the vault repo after each run |

## 5. Repository and vault layout

Two repos: **engine** (code) and **knowledge** (the vault), so code history and content history stay apart.

```
~/code/kb-engine/                  # Python package
  pyproject.toml   .env.example   kb/…

~/knowledge/                       # git repo = the single Obsidian vault
  CLAUDE.md                        # schema + rules for compile sessions (§9)
  raw/
    x/  youtube/  web/             # immutable sources
  wiki/
    index.md                       # top level: links to domain hubs + global stats
    log.md                         # append-only operations log
    _compile-queue.md              # sources summarized but not yet compiled
    domains/                       # one hub page per domain (catalog of its pages)
      data-engineering.md  ai-llms.md  software.md  tennis.md  system-design.md
    sources/                       # 1 page per source (automated)
    concepts/  entities/  syntheses/   # shared across domains; compile sessions only
  digests/
    daily/2026-09-26.md
    weekly/2026-W39.md
```

The engine DB lives outside the vault repo, at `~/.kb/kb.sqlite`, so machine state doesn't pollute the content history.

## 6. Data model

### 6.1 `items` table

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `url` | TEXT | as captured |
| `canonical_url` | TEXT UNIQUE | dedup key (§7.1) |
| `source_type` | TEXT | `x` \| `youtube` \| `web` |
| `origin` | TEXT | `x_bookmark` \| `telegram` \| `cli` \| `backfill` |
| `hint_tag` | TEXT NULL | hashtag given at capture |
| `status` | TEXT | see state machine |
| `domains` | TEXT NULL | JSON array, primary first, e.g. `["system-design","data-engineering"]` |
| `tag_method` | TEXT NULL | `hashtag` \| `classifier` \| `fallback` |
| `tag_confidence` | REAL NULL | |
| `title` | TEXT NULL | |
| `raw_path` | TEXT NULL | relative to knowledge repo |
| `summary_path` | TEXT NULL | |
| `content_hash` | TEXT NULL | sha256 of raw body |
| `attempts` | INTEGER | default 0 |
| `last_error` | TEXT NULL | |
| `captured_at`, `updated_at`, `done_at` | TEXT (ISO 8601) | |

### 6.2 `state` table (key/value)
- `x.last_bookmark_id` is the newest bookmark already enqueued.
- `x.refresh_token` holds the rotated OAuth token. Keep it in the OS keyring if possible, otherwise here.
- `telegram.offset` is the last `update_id + 1`.

### 6.3 State machine

```
queued ─► fetched ─► tagged ─► written ─► summarized ─► done
   │         │          │          │            │
   └─────────┴──────────┴──────────┴────────────┴─► failed (attempts < 3 → retried next run)
                                                    failed_permanent (attempts ≥ 3, or 404/paywall)
```

Each step is idempotent and persisted before the next one begins, so a crash resumes from the last completed step.

## 7. Processing pipeline

### 7.1 Normalization and dedup
- Hosts: `twitter.com`/`mobile.twitter.com` → `x.com`, `youtu.be/<id>` and `youtube.com/shorts/<id>` → `youtube.com/watch?v=<id>`.
- Strip `utm_*`, `si`, `s`, `t` (except YouTube `t` is dropped too), `ref`, and fragments.
- X canonical key: `x.com/i/status/<id>`, which is independent of the handle.
- If `canonical_url` already exists, skip it. If the earlier row came from a different origin, append the new hashtag as a note.

### 7.2 Fetchers

**X (`fetchers/x.py`)**
- `GET /2/tweets/:id` with `tweet.fields=created_at,conversation_id,note_tweet,entities,referenced_tweets,author_id`, `expansions=author_id,attachments.media_keys,referenced_tweets.id`, `user.fields=username,name`.
- Use `note_tweet.text` when it is present, because long posts come back truncated otherwise.
- **Thread unrolling:** if the post starts a self-thread (the author replies to themselves), call `GET /2/tweets/search/recent?query=conversation_id:<cid> from:<author>` and sort by `created_at`.
  - ⚠️ Recent search covers only ~7 days. New bookmarks are fetched within minutes, so this is fine. For the historical backfill, threads older than 7 days are stored as the single post plus a `thread: incomplete` flag.
- Quoted posts are included as a blockquote. Links inside a post are listed in front matter as `outlinks`; they are **not** auto-enqueued in v1.

**YouTube (`fetchers/youtube.py`)**
- `yt-dlp --skip-download --write-subs --write-auto-subs --sub-langs "en.*,es.*" --sub-format vtt --dump-json`.
- Preference order: manual subs, then auto subs. The VTT is cleaned by deduplicating rolling captions and merging into paragraphs, with a `[mm:ss]` marker every ~60s.
- Metadata: title, channel, upload date, duration, description (chapters when present).
- No subtitles → `failed_permanent` with reason `no_subtitles`. Whisper support is Phase 2.
- Keep `yt-dlp` updated with a `uv lock --upgrade-package yt-dlp` step, since YouTube breaks it periodically.

**Web (`fetchers/web.py`)**
- `httpx` GET with a browser-like UA and 20s timeout → `trafilatura.extract(output_format="markdown", include_links=True)`.
- If the extracted text is under 500 characters, treat it as a paywall or JS-only page and mark it `failed_permanent` with reason `extraction_empty`. Phase 2 falls back to the built-in browser or to manual clipping.

### 7.3 Domain tagging

1. **Hashtag override** (from Telegram or CLI):

   | Tag | Domain |
   |---|---|
   | `#de` `#data` | data-engineering |
   | `#ai` `#llm` | ai-llms |
   | `#swe` `#dev` | software |
   | `#tennis` | tennis |
   | `#sd` `#sysdesign` | system-design |

2. **Classifier**: one LLM call with the title plus the first ~2k tokens and a one-paragraph description of each domain, from `config.toml`. It returns JSON `{domains: [primary, secondary?], confidence, reason}`, with at most 2 domains.
3. If `confidence < 0.6`, the item is tagged `unsorted` and shows up in the digest. `kb tag <id> <domain...>` fixes it by rewriting front matter only; no files move.

A hashtag sets the primary domain; the classifier may still add a secondary one.

X bookmarks have no hashtags, so they always go through the classifier.

### 7.4 Raw file format

Path: `raw/<type>/<YYYY-MM-DD>-<slug>.md`. The slug comes from the title, max 60 characters.

```markdown
---
id: 142
title: "Why we moved off Airflow"
source_type: web            # x | youtube | web
url: https://example.com/…
author: Jane Doe
published: 2026-09-20
captured: 2026-09-26T10:14:00-03:00
origin: telegram
domains: [data-engineering]
tag_method: classifier
language: en
thread: complete           # x only
outlinks: []
tags: [raw, data-engineering]
---

<clean body>
```

The body is written once and never edited afterwards. Compile sessions treat `raw/` as read-only (enforced in `CLAUDE.md`).

### 7.5 Source summary (automated)

Path: `wiki/sources/<same-slug>.md`. There is one LLM call per item, **always in English**.

```markdown
---
source: "[[raw/web/2026-09-26-why-we-moved-off-airflow]]"
type: source-summary
status: uncompiled
source_type: web
published: 2026-09-20
domains: [data-engineering]
tags: [source, data-engineering]
---
# Why we moved off Airflow

**TL;DR:** 2–3 sentences.

## Key points
- 3–7 bullets

## Notable claims / numbers
- claim — with timestamp or section reference

## Candidate concepts & entities
- concepts: orchestration, backfills, …
- entities: Airflow, Dagster, …

## Open questions
- …
```

The "candidate concepts & entities" section is only a hint for the compile session. The automated step **never** creates or edits concept or entity pages.

After writing, one line is appended to `wiki/_compile-queue.md`: `- [ ] [[sources/<slug>]] #<domain> — <tl;dr>`, so sessions can filter the queue by domain.

Summarization prompt rules:
- Translate faithfully into English.
- Invent no facts.
- Preserve numbers exactly.
- For videos, cite `[mm:ss]` timestamps.
- If the source is thin (e.g. a single post), keep the summary proportionally short.

## 8. Capture adapters

### 8.1 X bookmarks
- Uses OAuth 2.0 Authorization Code with PKCE and scopes `tweet.read users.read bookmark.read offline.access`. `kb auth x` runs the one-time browser flow on `localhost`.
- Refresh tokens rotate on every refresh, so the new one must be persisted right away (§6.2).
- Each run requests `GET /2/users/{me}/bookmarks?max_results=20` and walks forward until it reaches `x.last_bookmark_id`, enqueuing everything newer.
- Cost: bookmark reads are Owned Reads, at $0.001 per resource, and repeat reads are deduplicated per UTC day. Fetching each post's full content is a normal post read at $0.005.
- Backfill: `kb backfill-x <export.json>` imports a twitter-web-exporter JSON dump. It is run once and bypasses the ~800 API cap.
- A **spending limit** is set in the X Developer Console as a safety net.

### 8.2 Telegram
- A private bot is created with @BotFather. `config.toml` holds `allowed_chat_ids = [<yours>]`, and messages from any other chat are ignored.
- The adapter long-polls `getUpdates(offset, timeout=0)` on every run, with no webhook.
- It extracts every URL in the message plus hashtags (§7.3). Free text becomes a `note` field in the raw front matter.
- It replies with one line, `✓ queued (2)` or `✗ no URL found`, so you know capture worked. Processing results arrive in the digest, not the chat.
- Forwarded messages without a URL (e.g. a forwarded X post in some clients): the text is stored as a `web`-type item with `url: telegram://<msg_id>`. This is a known v1 edge case.

### 8.3 CLI
`kb add <url> [#tag] [--note "…"]`

## 9. Compile workflow (manual, Claude Code)

The vault's `CLAUDE.md` defines the schema. The rules include:
- `raw/` is read-only. Every wiki claim links to a `sources/` page, which links to `raw/`.
- Page types: `concepts/` (ideas and techniques), `entities/` (tools, people, companies), `syntheses/` (comparisons, "state of X").
- Naming uses kebab-case and one concept per page, and existing pages are searched before a new one is created.
- Concept, entity and synthesis pages carry a `domains:` front matter list and are linked from each matching hub in `wiki/domains/`. A page can belong to several hubs; that is the point of a single vault.
- **Scaling the index:** sessions read `wiki/index.md`, then only the relevant hub pages, never the full page list. Hubs stay under ~150 entries; beyond that, a hub is split into sub-hubs (e.g. `data-engineering/orchestration`).
- The session updates the affected hubs and `wiki/index.md`, and appends to `wiki/log.md` on every operation.
- Once a queue item is compiled, it is ticked in `_compile-queue.md` and the source summary's `status` is set to `compiled`.

Standard session prompts:
- **Compile:** "Process unchecked items in `wiki/_compile-queue.md` tagged `#<domain>`." Scoping by domain keeps each session's context focused. An item with two domains is compiled once and linked from both hubs.
- **Cross-domain:** "Find concepts shared between `#system-design` and `#data-engineering` sources that aren't linked yet."
- **Lint:** "Find contradictions, orphan pages, concepts mentioned in ≥2 sources without a page, and stale syntheses. Report first, then fix after I confirm."
- **Query:** "Answer X from the wiki, citing pages; if the answer is novel, propose a `syntheses/` page."

Suggested cadence: compile 1–2×/week per active domain and lint monthly. Each session ends with `git commit -m "compile: <domain> <date>"`, and the diff is reviewed before committing.

## 10. Digest

`kb digest` runs daily at 07:00 and builds the reports from the DB alone, so it costs no LLM tokens.

- Daily note `digests/daily/YYYY-MM-DD.md`, created only if something was ingested, with one section per domain:
  - For each item: title, source type, link to the summary, and TL;DR. Items with two domains appear under their primary domain, with the secondary shown as a tag.
  - "Needs attention": failed items with their reason, and `unsorted` items.
  - Compile queue length per domain.
- On Monday it also writes a weekly rollup `digests/weekly/YYYY-Www.md`: counts by source and domain, all TL;DRs, and the oldest uncompiled items.

## 11. Obsidian integration

Obsidian is the **reading and navigation layer only**. The engine and Claude Code write plain markdown; Obsidian opens `~/knowledge/` as a vault and picks up file changes live. Nothing in the pipeline depends on Obsidian running.

### 11.1 Conventions the engine and compile sessions must follow

| Convention | Why |
|---|---|
| Links use vault-relative paths: `[[sources/<slug>]]`, `[[concepts/idempotency]]`, `[[raw/web/<file>]]` | No ambiguity when two pages share a basename |
| Every page has `tags` that include its domains (e.g. `tags: [concept, data-engineering]`), plus a `domains` list | Graph color groups and the tag pane read `tags`; Bases/Dataview filter on `domains` |
| Page-type tag on every generated page: `raw`, `source`, `concept`, `entity`, `synthesis`, `digest`, `hub` | Lets graph and queries filter by page type |
| Dates in properties are ISO `YYYY-MM-DD` | Obsidian types them as dates, so they sort and filter correctly |
| Front matter stays flat (strings, lists, dates), with no nested objects | Obsidian's Properties UI can't edit nested YAML |
| `raw/` is never edited, including by hand in Obsidian | It's the verification baseline (§9) |

### 11.2 Vault configuration (committed to git)

Commit `.obsidian/` so the setup is reproducible, but ignore the volatile files:

```gitignore
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.trash/
```

Core plugins to enable: **Backlinks, Outgoing links, Graph view, Properties, Bases, Tags, Search, Daily notes, Bookmarks**.

**Daily notes** (`.obsidian/daily-notes.json`) point at the digest folder, so "Open today's daily note" opens the digest:

```json
{ "folder": "digests/daily", "format": "YYYY-MM-DD" }
```

On a day with no ingests, Obsidian creates an empty note. That's harmless: `kb digest` fills it on its next run if anything arrives.

**Graph view** (`.obsidian/graph.json`, relevant keys) hides raw sources and digests and colors nodes by domain:

```json
{
  "search": "-path:raw -path:digests -path:wiki/domains",
  "showOrphans": false,
  "colorGroups": [
    { "query": "tag:#data-engineering", "color": { "a": 1, "rgb": 3900150 } },
    { "query": "tag:#ai-llms",          "color": { "a": 1, "rgb": 10181046 } },
    { "query": "tag:#software",         "color": { "a": 1, "rgb": 3066993 } },
    { "query": "tag:#system-design",    "color": { "a": 1, "rgb": 15105570 } },
    { "query": "tag:#tennis",           "color": { "a": 1, "rgb": 15844367 } }
  ]
}
```

Two graph searches worth bookmarking: **Concepts only** (`path:wiki/concepts OR path:wiki/entities`) shows the idea network, and **Everything** (no filter) helps debug links.

### 11.3 Hub pages with live tables (Bases)

Each hub `wiki/domains/<domain>.md` has two parts:
- A short **hand-curated section**, maintained by compile sessions: an overview paragraph and the 5–10 key concepts.
- **Embedded Bases views** that list pages automatically. The hub never goes stale, and compile sessions don't have to maintain long lists.

Example `wiki/_bases/system-design.base`:

```yaml
filters:
  and:
    - file.inFolder("wiki")
    - file.hasTag("system-design")
views:
  - type: table
    name: Concepts & entities
    filters:
      or:
        - file.hasTag("concept")
        - file.hasTag("entity")
    order:
      - file.name
      - tags
      - file.mtime
  - type: table
    name: Uncompiled sources
    filters:
      and:
        - file.hasTag("source")
        - status == "uncompiled"
    order:
      - file.name
      - source_type
      - published
```

The hub embeds it with `![[_bases/system-design.base]]`. `kb init` generates one `.base` per domain from a template.

Global views in `wiki/_bases/`:

| Base | Shows |
|---|---|
| `compile-queue.base` | All `status == "uncompiled"` sources, grouped by domain; a structured view over `_compile-queue.md` |
| `recent-sources.base` | Sources captured in the last 14 days, by `source_type` |
| `hot-concepts.base` | Concepts sorted by number of backlinks, showing where knowledge is converging |

Failures are deliberately not a Base: they live in the DB and the digest, not in the vault.

Bases syntax is still evolving, so check filter functions against the current Obsidian docs when implementing. **Fallback:** the Dataview community plugin covers the same queries, for example:

````markdown
```dataview
TABLE source_type, published
FROM "wiki/sources"
WHERE status = "uncompiled" AND contains(domains, "system-design")
SORT published DESC
```
````

### 11.4 Reading workflow

| When | Where in Obsidian | What |
|---|---|---|
| Daily | Daily note (today's digest) | Skim TL;DRs, open the summaries that catch your eye, follow backlinks to see what they connect to |
| While reading | Backlinks and Outgoing links panes | Move source → concept → other sources; add your own notes anywhere outside `raw/` |
| Weekly | `digests/weekly/…`, then the hubs | Choose which domain to compile from the hubs' "Uncompiled sources" views |
| After a compile session | "Concepts only" graph + git diff | See new links and review what the LLM changed before committing |
| Anytime | Search, tag pane | Full-text search, narrowed with `tag:#<domain>` or `path:wiki/concepts` |

**Your own notes** go in `notes/` or anywhere in `wiki/`, tagged `mine`. Compile sessions read them as input but never rewrite them; this rule goes in `CLAUDE.md`.

### 11.5 Optional plugins

| Plugin | Use |
|---|---|
| **Obsidian Git** | Browse automated and compile commits and their diffs inside Obsidian. Turn off auto-commit and auto-pull so it never races the engine's commits (§13) |
| **Dataview** | Fallback, or extra queries Bases can't express |
| **Marp Slides** | Render `syntheses/` pages written in Marp format as decks, as Karpathy does |

### 11.6 Mobile

- Capture needs no vault on the phone: it goes through Telegram and X bookmarks.
- Reading on the phone needs the vault synced there. **Obsidian Sync** is the low-friction option; Git on mobile works but is clunky.
- Treat the phone as read-mostly. Editing a file on the phone while the engine writes the same file can create sync conflicts.

## 12. Cost estimate

Assumptions: ≤5 items/day, about 150 items/month, a mix of 40% X, 30% web and 30% YouTube.

| Item | Estimate |
|---|---|
| X: bookmark polling (Owned Reads, deduped daily) | < $0.50 |
| X: post fetch + thread search (~60 items × ~3 reads × $0.005) | ~ $1 |
| LLM classifier (~150 × ~2.5k tokens) | small |
| LLM summaries (~150 × avg ~8k in / ~800 out; 1h video ≈ 15k tokens) | ~1.3M in / 0.12M out |
| **Total** | **well under $10/month** with a Haiku-class model |

The model is configurable (`llm.model = "claude-haiku-4-5-20251001"`). Check current API pricing before committing to it. Transcripts are truncated at a configurable max (default 40k tokens) using head + tail chunks. Compile sessions in Claude Code are not included in this budget.

## 13. Scheduling and operations

- **Scheduler:** launchd on macOS (it runs missed jobs after the machine wakes) or cron on Linux.
  - `kb run` every 10 min: capture from Telegram and X, process the queue (max 10 items per run), then git commit.
  - `kb digest` daily at 07:00.
- **Locking:** a file lock keeps two overlapping runs from happening.
- **Retries:** up to 3 attempts per item with the next scheduled run acting as backoff. HTTP 404, 410 and 401-on-content, plus empty extraction, go straight to `failed_permanent`.
- **Observability:** `kb status` shows counts by status plus the last 10 errors. Logs go to `~/.kb/logs/kb.log` (rotated) as structured JSON lines.
- **Secrets:** `.env` (git-ignored) or the OS keyring holds `ANTHROPIC_API_KEY`, `X_CLIENT_ID`, the X refresh token, and `TELEGRAM_BOT_TOKEN`.
- **Git:** after each run, the engine commits only paths it wrote, with message `auto: ingest <n> items [<ids>]`. It never commits while there are unstaged manual edits in the same files; in that case it skips the commit and logs a warning. Pushing to a private remote is manual or a daily cron.

## 14. Tech stack

- Python 3.12, `uv`, `typer` (CLI), `pydantic-settings` (config), `httpx`, `sqlite3` (stdlib)
- `yt-dlp`, `webvtt-py`, `trafilatura`
- `anthropic` SDK, `python-frontmatter`, `python-slugify`
- `python-telegram-bot` (or raw `httpx` calls, which is enough for `getUpdates` and `sendMessage`)
- Tests: `pytest` with recorded HTTP fixtures (`respx`) and a `--dry-run` flag that writes to a temp knowledge dir.

## 15. Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | Skeleton: repo, config, SQLite schema, `kb add` / `kb status`, normalization + dedup | URLs can be queued and listed |
| M1 | Web + YouTube fetchers, raw writer, git commit | `kb run` produces raw files for web and YouTube links |
| M2 | Tagger + summarizer + compile queue | Items are tagged with domains and get a summary page |
| M3 | Digest (daily + weekly) | The morning note is readable and failed items are visible |
| M4 | Telegram adapter | Sending a link from the phone yields a summary in the next digest |
| M5 | X: OAuth, bookmark poller, X fetcher with threads; backfill import | Bookmarking a post yields a summary in the next digest |
| M6 | `CLAUDE.md`, domain hubs, Obsidian config (§11) + first compile & lint sessions | Hubs show live Bases tables, graph is colored by domain, first shared concept/entity pages exist, lint is clean |
| M7 | Scheduling (launchd/cron), locking, log rotation | Runs unattended for a week without intervention |

## 16. Risks and open questions

| Risk / question | Mitigation / decision needed |
|---|---|
| **Index outgrows a compile session's context** in a single vault | Hierarchical index (index → domain hubs → sub-hubs), domain-scoped compile sessions, and the FTS search layer at ~300 sources. |
| Unrelated domains add noise to each other (e.g. tennis vs. the rest) | Domain-scoped sessions and hubs; tennis forms its own cluster in the graph. |
| Classifier mis-tags | Hashtag override, `kb tag`, and the `unsorted` threshold. Tags are reviewed weekly and domain descriptions refined. |
| Thread unrolling limited to recent search (~7 days) | New bookmarks are fetched quickly. Backfilled old threads are flagged `incomplete`. |
| YouTube / yt-dlp breakage | Pin `yt-dlp` plus a regular upgrade. Failures surface in the digest. |
| Paywalled or JS-heavy pages | Marked `failed_permanent` in v1. Browser-based or manual clip fallback in Phase 2. |
| X API pricing or endpoint changes | Spending limit in the console and a monthly check of the usage endpoint. |
| LLM hallucination in summaries | Summaries link to raw. Compile sessions verify claims against raw. The lint looks for unsupported claims. |
| Laptop asleep for > 24h loses Telegram updates | Acceptable for v1. Phase 2 could move capture to a tiny always-on host. |

## 17. Future phases
- Whisper transcription for videos without subtitles; podcast support.
- Auto-enqueue selected `outlinks`.
- Search layer (SQLite FTS5, then optional embeddings) exposed to compile sessions as a tool; trigger at ~300 sources.
- X bookmark folders as a tagging signal, if the API exposes them.
