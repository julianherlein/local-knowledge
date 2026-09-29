# Vault operating manual

You are working in a personal knowledge base: one git repo that is also one Obsidian vault.
An automated engine (`kb`) captures articles, videos and posts into `raw/`, writes one English
summary per source into `wiki/sources/`, and queues it in `wiki/_compile-queue.md`. Your job in a
Claude Code session is the part the engine never does: **compile** those sources into a network of
concept, entity and synthesis pages, keep the domain hubs navigable, and keep the wiki honest.

Read this whole file at the start of every session. It is the contract. When a request conflicts
with it, say so and ask before acting.

---

## 1. Hard rules

1. **`raw/` is read-only.** Never create, edit, rename, move or delete anything under `raw/`. It is
   the verification baseline. If a raw file looks wrong, tell the user.
2. **Every factual claim is cited.** Each claim on a concept, entity or synthesis page links to the
   `wiki/sources/` page it came from. Source pages link to their raw file in the `source:`
   property. The chain is always: wiki page -> `wiki/sources/<stem>` -> `raw/<type>/<stem>`.
3. **Links are full vault paths**, without the `.md` extension:
   `[[wiki/sources/2026-09-26-why-we-moved-off-airflow]]`, `[[wiki/concepts/idempotency]]`,
   `[[wiki/entities/dagster]]`, `[[wiki/syntheses/orchestrator-comparison]]`,
   `[[wiki/domains/data-engineering]]`, `[[raw/web/2026-09-26-why-we-moved-off-airflow]]`.
   Display text goes after a pipe: `[[wiki/concepts/idempotency|idempotent]]`.
   Never write a bare basename (`[[idempotency]]`), a relative path (`[[../concepts/x]]`) or a
   markdown link (`[x](x.md)`) to a vault page. Two pages can share a basename; full paths cannot
   be ambiguous.
4. **English only**, whatever the source language.
5. **Invent nothing.** Every number, quote and claim must be traceable to a source. When you use a
   claim from a summary, verify it against the raw file first (section 7). If the summary and the
   raw disagree, the raw wins.
6. **Front matter is flat**: strings, numbers, booleans, ISO dates (`YYYY-MM-DD`) and flat lists
   only. No nested objects. Obsidian's Properties editor cannot edit nested YAML.
7. **User notes are read-only for you.** Files under `notes/`, and any page whose `tags` contain
   `mine`, belong to the user. Read them, link to them, never edit, rename, move or delete them,
   and never cite them as evidence for a factual claim.
8. **Domains of sources change only through `kb tag`.** Never edit `domains` or `tags` in
   `wiki/sources/*` or `raw/*`. If a source is mis-tagged, tell the user the exact command:
   `kb tag <item_id> <domain> [<second-domain>]` (the `item_id` is in the summary's front matter).
   The command keeps the engine database, the files and the compile queue in sync.
9. **Log every operation** in `wiki/log.md` (section 10), including read-only queries.
10. **Never commit without showing the diff first** (section 12). The one exception is a commit
    that contains only your new `wiki/log.md` line (lint phase 1, read-only queries). Never commit
    anything under `raw/`, `digests/`, `notes/` or `.obsidian/`.
11. **Do not run engine commands that write** (`kb run`, `kb digest`, `kb tag`, `kb retry`) during
    a session. Suggest them to the user instead. Read-only `kb status` is fine.
12. **Get dates and times from the shell** (`date +%F` for dates, `date +"%F %H:%M"` for log
    lines). Never guess today's date.

## 2. Who writes what

| Path | Written by | What you may do |
|---|---|---|
| `raw/` | engine, once | read only |
| `wiki/sources/` | engine | read; set `status: compiled` and `compiled: <date>`; fix a factual error only after verifying it against raw (log it as `fix-summary`) |
| `wiki/_compile-queue.md` | engine appends lines | tick `- [ ]` to `- [x]`; nothing else |
| `wiki/concepts/`, `wiki/entities/`, `wiki/syntheses/` | you | create and edit, following this manual |
| `wiki/domains/` (hubs and sub-hubs) | `kb init` creates, you maintain | edit the curated sections; leave "Live views" alone |
| `wiki/index.md` | `kb init` creates, you maintain | domain list, stats line |
| `wiki/log.md` | you | append one line per operation; never edit past lines |
| `wiki/_bases/`, `.obsidian/` | `kb init` | do not touch unless the user asks |
| `digests/` | engine | read only |
| `notes/`, pages tagged `mine` | the user | read only |

## 3. Navigating without reading everything

The wiki grows past what fits in one context. Navigate top-down and stop as soon as you have what
you need:

1. `wiki/index.md`: the domain hubs and global stats.
2. The hub of each relevant domain, `wiki/domains/<domain>.md`: overview, key concepts, and the
   catalog (one line per page). Follow a sub-hub link when the hub has been split.
3. Only the pages the catalog points you to.

Never list or read all of `wiki/concepts/` or `wiki/entities/`. To find a page by name, grep file
names and `aliases` instead (section 6). The "Live views" sections of hubs and the index embed
Obsidian Bases tables; they render only inside Obsidian and contain no information for you.

## 4. Page types and templates

Every page you create uses exactly one of these templates. Property order as shown. `created` is
set once; `updated` changes whenever you change the page's content.

### 4.1 Source summary (engine-made, for reference)

```markdown
---
source: "[[raw/web/2026-09-26-why-we-moved-off-airflow]]"
type: source-summary
status: uncompiled            # you set: compiled
item_id: 142                  # engine id, used by `kb tag` / `kb retry`
source_type: web              # x | youtube | web
url: https://example.com/post
author: Jane Doe
published: 2026-09-20
captured: 2026-09-26
domains: [data-engineering]   # primary first; never edit (rule 8)
tags: [source, data-engineering]
---
# Why we moved off Airflow

**TL;DR:** ...
## Key points
## Notable claims / numbers
## Candidate concepts & entities   <- hints only; you decide what becomes a page
## Open questions
```

When you compile it, add `compiled: <date>` after `status` and change `status` to `compiled`.
Do not touch the other properties or the body (unless fixing a verified error).

### 4.2 Concept page: `wiki/concepts/<name>.md`

An idea, technique, pattern or principle. One concept per page.

```markdown
---
type: concept
domains: [data-engineering, system-design]
tags: [concept, data-engineering, system-design]
aliases: [idempotent operations, idempotence]
sources: ["[[wiki/sources/2026-09-26-why-we-moved-off-airflow]]", "[[wiki/sources/2026-10-02-exactly-once-myths]]"]
created: 2026-09-26
updated: 2026-10-02
---
# Idempotency

One or two sentences that define the concept in plain words.

## How it works
The mechanism, in your own words, with cited specifics. A claim ends with its citation
([[wiki/sources/2026-09-26-why-we-moved-off-airflow|Why we moved off Airflow]]).

## Trade-offs
When it helps, what it costs, where it breaks. Cited.

## In practice
Concrete examples and numbers from the sources, cited. Video claims keep their timestamp,
e.g. "retries tripled at peak [14:20] ([[wiki/sources/2026-09-28-scaling-talk|Scaling talk]])".

## Related
- [[wiki/concepts/exactly-once-delivery]]: how the two guarantees combine
- [[wiki/entities/dagster]]: implements it for asset materializations

## Sources
- [[wiki/sources/2026-09-26-why-we-moved-off-airflow|Why we moved off Airflow]]: migration case study, failure numbers
- [[wiki/sources/2026-10-02-exactly-once-myths|Exactly-once myths]]: formal definition
```

### 4.3 Entity page: `wiki/entities/<name>.md`

A tool, library, product, company, person, project, organization or standard.

```markdown
---
type: entity
entity_kind: tool             # tool | library | product | company | person | project | organization | standard
domains: [data-engineering]
tags: [entity, data-engineering]
aliases: [Dagster Cloud]
url: https://dagster.io
sources: ["[[wiki/sources/2026-09-26-why-we-moved-off-airflow]]"]
created: 2026-09-26
updated: 2026-09-26
---
# Dagster

One sentence: what it is and who makes it.

## What it is used for
## Notable facts and claims
Cited, dated when the source dates them ("as of September 2026, ...").
## Related
## Sources
```

Omit `url` when no source gives one. Never look up facts outside the sources.

### 4.4 Synthesis page: `wiki/syntheses/<name>.md`

A comparison, a "state of X", or an answer that combines several pages. Created only when the user
agrees (section 11.4).

```markdown
---
type: synthesis
domains: [data-engineering]
tags: [synthesis, data-engineering]
aliases: []
question: "Which orchestrator fits a small data team?"
as_of: 2026-10-05             # date of the newest source considered
sources: ["[[wiki/sources/...]]", "[[wiki/sources/...]]"]
created: 2026-10-05
updated: 2026-10-05
---
# Orchestrator comparison for small teams

**Answer:** two or three sentences.

## Evidence
Claims grouped by theme, each cited. Link the concept and entity pages you build on.

## Where the sources disagree
## What would change this answer
## Sources
```

### 4.5 Property rules for every page you write

- `type` is one of `concept`, `entity`, `synthesis`, `hub`.
- `domains` is derived from the page's sources, never chosen by taste:
  1. Take every source in `sources` that **substantively discusses** the page's subject (a claim
     on the page cites it for more than a passing mention).
  2. The page's domains are the union of those sources' `domains` (from each source summary's
     front matter, both primary and secondary), minus `unsorted`, limited to domains that have a
     top-level hub in `wiki/domains/`.
  3. Order: by the number of such sources carrying the domain, descending; ties keep the order in
     which the domains were first added to the page (for a new page: the order they appear in its
     first source's `domains`).
  4. Re-evaluate `domains` (and therefore `tags` and the hub catalogs) every time you add a source
     to the page. A domain is removed only when no substantive source carries it any more.
  Worked example: a page cites A `[data-engineering]`, B `[system-design, data-engineering]`,
  C `[system-design]` (passing mention only). Substantive: A, B. Counts: data-engineering 2,
  system-design 1, so `domains: [data-engineering, system-design]`.
- `tags` = the type tag, then exactly the `domains` values, in the same order. No other tags
  unless the user asks (the graph colors and Bases views depend on this).
- `sources` lists every source page cited in the body, as quoted wikilinks, oldest first: sorted by
  the `YYYY-MM-DD` prefix of the source's file name (its capture date), ties by file name. The
  `## Sources` section uses the same order.
- `aliases` holds other names people use: acronyms (`CDC`), expansions, alternate spellings,
  former names. Use `[]` when there are none.
- Dates are `YYYY-MM-DD`, unquoted.

## 5. Naming

- Kebab-case, lowercase ASCII, singular: `idempotency`, `change-data-capture`, `slowly-changing-dimension`.
- Concepts: the most common English name of the idea. Expand acronyms in the file name
  (`change-data-capture`) and put the acronym in `aliases` (`CDC`).
- Entities: the official name without legal suffixes: `dagster`, `apache-kafka`, `anthropic`.
  People: `first-last` (`martin-kleppmann`). Add a qualifier only to disambiguate
  (`delta-lake`, not `delta`).
- Syntheses: a short description of the question: `orchestrator-comparison-small-teams`.
- The H1 is the human-readable name ("Change data capture"); the file name never changes after
  creation (links depend on it). If a better name appears later, add it to `aliases`.

## 6. Before you create a page: search first

A duplicate page splits knowledge in two. Before creating `wiki/concepts/<name>.md` or
`wiki/entities/<name>.md`:

1. Check the catalog of each relevant hub.
2. Search file names: glob `wiki/concepts/*<keyword>*` and `wiki/entities/*<keyword>*`.
3. Search aliases and titles: grep `wiki/concepts` and `wiki/entities` (case-insensitive) for the
   name, its acronym and its expansion, e.g. `grep -ril -e "change data capture" -e "\bCDC\b" wiki/concepts wiki/entities`.
4. If a page exists under another name, update that page and add the new name to its `aliases`.
5. If the idea is a narrower case of an existing page, add a section to that page instead of a new
   page, unless the narrower idea has at least two sources of its own.

Create a page only when the concept or entity is **central to at least one source** (the source's
key points depend on it) **or is discussed in at least two sources**. A passing mention does not
earn a page. Never create an empty stub: a new page has at least one cited claim.

## 7. Verifying claims against raw

Summaries are machine-written and can be wrong. For every claim you put on a concept, entity or
synthesis page:

1. Open the raw file named in the summary's `source:` property.
2. Find the passage (search for the number, name or keyword; do not read a 40k-token transcript
   end to end). For videos, the raw transcript has `[mm:ss]` markers.
3. Confirm the claim, the number and its context (who says it, about what, when).
4. If the raw contradicts the summary, use the raw version, fix the summary's line, and log a
   `fix-summary` line. If the raw is ambiguous, leave the claim out or state the ambiguity.
5. Keep hedges: "the author reports", "in their benchmark", "claims". Do not upgrade an opinion to
   a fact.

When two sources disagree, keep both, cite both, and flag it on the page:

```markdown
> [!warning] Conflicting claims
> [[wiki/sources/a|Source A]] reports a 40% drop in failures; [[wiki/sources/b|Source B]] saw no change. Unresolved.
```

## 8. Hubs

Each domain has one hub, `wiki/domains/<domain>.md`, created by `kb init` from `config.toml`.
Structure (keep the headings exactly):

- `## Overview`: one or two paragraphs on what this domain covers here and its main threads.
  Rewrite it when the domain's shape changes, not on every compile.
- `## Key concepts`: the 5 to 10 most important pages, most central first (most citing sources,
  then most links from other pages). With fewer than 5 pages in the domain, list all of them.
  Line format, exactly:
  `- [[wiki/concepts/idempotency]]: at most 15 words on why it matters in this domain`
- **Placeholders.** A new hub holds italic placeholder lines (`_Not written yet..._`,
  `_None yet..._`). Whichever session first adds a page to the hub replaces them, even when it
  is compiling another domain and only reached this hub through a page's secondary domain:
  write a one- or two-sentence Overview from what the wiki holds so far, list the pages in Key
  concepts, and delete the placeholder lines. Later sessions grow them under the rules above.
- `## Catalog`: every concept, entity and synthesis page whose `domains` include this domain and
  that is **not listed in one of this hub's sub-hubs**, one line each, alphabetical by file name
  inside `### Concepts`, `### Entities`, `### Syntheses`. Line format, exactly:
  `- [[wiki/concepts/idempotency]]: at most 15 words on what the page covers`
- `## Sub-hubs`: present only after a split, placed between `## Catalog` and `## Live views`. One
  line per sub-hub, alphabetical: `- [[wiki/domains/<domain>/<subtopic>]]: what it covers (N pages)`.
- `## Live views`: Obsidian Bases embeds under `### Live: ...` headings. Never edit or remove.

A page with two domains is listed in both hubs. That is the point of a single vault.

**Where a page's catalog line goes.** For each domain in the page's `domains`: if that hub has
sub-hubs and one sub-hub's subtopic fits the page, the line goes in that sub-hub's catalog (and
its `(N pages)` count in the parent is updated); otherwise it goes in the hub's own `## Catalog`.
A page is listed exactly once per domain. Its `domains` always name the top-level domain
(`data-engineering`), never a sub-hub.

**Split rule.** Count a hub's catalog lines with
`grep -c '^- \[\[wiki/\(concepts\|entities\|syntheses\)/' wiki/domains/<domain>.md` (this includes
Key concepts lines, which is fine as an estimate). Past about 150, propose a split to the user:

1. Group the catalog entries into 3 to 8 subtopics of 20 to 80 pages each.
2. Create `wiki/domains/<domain>/<subtopic>.md` for each: sub-hub front matter below, then
   `# <Subtopic title>`, one overview sentence, and a `## Catalog` with the same three `###`
   groups and line format.
3. Move the entries: delete them from the parent's `## Catalog`, add them to the sub-hub's.
   Entries that fit no subtopic stay in the parent's `## Catalog`.
4. Add the parent's `## Sub-hubs` section with one line per sub-hub.
5. Key concepts stay on the parent hub.

Split only after the user agrees, and log it as `hub-split`.

Sub-hub front matter:

```yaml
---
type: hub
domains: [data-engineering]
tags: [hub, data-engineering]
parent: "[[wiki/domains/data-engineering]]"
created: 2026-11-01
updated: 2026-11-01
---
```

## 9. The index

`wiki/index.md` holds the domain list and one stats line. Keep both current:

- Every top-level hub in `wiki/domains/*.md` has exactly one line under `## Domains`:
  `- [[wiki/domains/<domain>]]: <one-line description>`. A user who adds a domain to `config.toml`
  and re-runs `kb init` gets a new hub but no index line; add it the next time you see it missing.
- The stats line is computed, never estimated. Run:

  ```bash
  for d in sources concepts entities syntheses; do printf '%s=' "$d"; find "wiki/$d" -name '*.md' | wc -l; done
  grep -c '^- \[ \]' wiki/_compile-queue.md
  ```

  and rewrite the line, keeping this exact format:
  `**Stats:** 42 sources (7 unchecked in queue, incl. unsorted) | 18 concepts | 11 entities | 2 syntheses | last compile: 2026-10-05`
- `last compile` is the date (`date +%F`) of the latest session that compiled at least one source
  (section 11.1). Other sessions leave it unchanged.
- Set the index's `updated` property to today whenever you change the domain list or the stats line.

## 10. The log

`wiki/log.md` is append-only: one line per operation, at the bottom, never edited afterwards.

Format, exactly (pipe-separated, so it can be grepped and parsed):

```
- YYYY-MM-DD HH:MM | <op> | <scope> | <details>
```

- `<op>` is one of: `compile`, `cross-domain`, `lint`, `lint-fix`, `query`, `synthesis`,
  `hub-split`, `fix-summary`, `manual`.
- `<scope>` is the domain (`data-engineering`), a domain pair (`system-design+data-engineering`),
  `all`, or a page path for single-page operations.
- `<details>` says what changed, with paths: `4 sources; created wiki/concepts/idempotency, wiki/entities/dagster; updated wiki/concepts/backfill; hubs data-engineering, system-design`.
  For read-only operations say what was asked and found: `asked "which orchestrator?"; answered from 3 pages; no synthesis proposed`.

Examples:

```
- 2026-10-05 14:05 | compile | data-engineering | 3 sources; created wiki/concepts/idempotency; updated wiki/entities/dagster; hubs data-engineering
- 2026-10-05 15:20 | fix-summary | wiki/sources/2026-09-26-why-we-moved-off-airflow | "40% cheaper" corrected to "40% fewer failures" per raw
- 2026-11-01 10:00 | lint | all | 7 findings reported (2 orphans, 3 missing pages, 1 conflict, 1 stale synthesis)
```

Get the timestamp from `date +"%F %H:%M"`.

## 11. Standard sessions

The user starts a session with one of these prompts (or something close). Follow the matching
procedure. Every procedure starts with the preflight in section 12.

### 11.1 Compile a domain

> Process unchecked items in `wiki/_compile-queue.md` tagged `#<domain>`.

1. List the work: `grep -nE '^- \[ \] \[\[[^]]+\]\]( #[a-z0-9-]+)* #<domain>( |$)' wiki/_compile-queue.md`.
   The pattern only looks at the run of hashtags right after the link, so a `#word` inside the
   TL;DR never matches, and the trailing `( |$)` keeps `#data` from matching `#data-engineering`.
   The first hashtag on a line is the source's primary domain; a line matches if either tag is
   `#<domain>`.
   Work oldest first (file order), at most 10 sources per session; tell the user how many remain.
2. Read `wiki/index.md` and the hub of `<domain>` (plus the hub of any secondary domain you touch).
3. For each source:
   1. Read the summary. Open the raw file for every claim you will use (section 7).
   2. For each candidate concept and entity, and anything else central to the source: search for an
      existing page (section 6), then update it or create it from the template (section 4). Weave
      the new claims into the right sections with citations; do not append a "from source X" block.
   3. Add the source to the page's `sources` list and `## Sources` section (date order, section
      4.5), re-derive the page's `domains` and `tags` from its sources (section 4.5), and set
      `updated`.
   4. If the source adds nothing new (already covered, or too thin), compile it anyway and say so
      in the log details.
4. Update the hubs: for every created page, and every page whose `domains` changed in step 3,
   make the catalogs match its `domains`: add a line in each hub (or fitting sub-hub, section 8)
   of a domain it now has, remove it from hubs of a domain it lost. Revise Key concepts or
   Overview if the domain's shape changed; set `updated` on every hub you edited.
5. Close each source: tick its queue line (`- [ ]` to `- [x]`, the rest of the line unchanged;
   never delete or reorder lines) and set `status: compiled` plus `compiled: <date>` in its summary.
   A source with two domains is compiled once and ticked once, and its pages are linked from both hubs.
6. Update the index stats line, with `last compile` = today (section 9).
7. Append the log line: `op = compile`, `scope = <domain>`.
8. Finish with section 12: diff review, then `git commit -m "compile: <domain> <YYYY-MM-DD>"`.

Items tagged `#unsorted` are never compiled. List them at the end of the session with a suggested
domain and the exact command for the user: `kb tag <item_id> <domain>`. After the user runs it, the
queue line carries the new hashtag and the item is compiled with its domain.

### 11.2 Cross-domain links

> Find concepts shared between `#<domain-a>` and `#<domain-b>` sources that aren't linked yet.

1. Read both hubs' catalogs.
2. For each concept and entity page in one domain but not the other, grep the other domain's
   source summaries (`grep -lE '^domains: \[(.*, )?<domain-b>(,|\])' wiki/sources/*.md`, then search
   those for the page's name and aliases).
3. A page gains a domain only through the rule in section 4.5: cite a source of that domain that
   substantively discusses the page's subject (not a passing mention), then re-derive `domains`
   and `tags` from the sources (which also fixes their order), add the catalog line to the second
   hub (or fitting sub-hub), and set `updated`.
4. Add `## Related` links between pages of the two domains that describe the same mechanism,
   with one line on how they relate.
5. Report what you linked and why, log `op = cross-domain`, `scope = <domain-a>+<domain-b>`,
   and commit after review: `compile: cross-domain <domain-a>+<domain-b> <YYYY-MM-DD>`.

### 11.3 Lint

> Find contradictions, orphan pages, concepts mentioned in >=2 sources without a page, and stale syntheses. Report first, then fix after I confirm.

**Phase 1, report only. Change nothing.** Check, and report as a numbered list of
`<check> | <path> | <problem> | <proposed fix>`:

1. Contradictions: `> [!warning] Conflicting claims` callouts, plus claims on different pages
   about the same thing with different numbers.
2. Orphans: concept, entity or synthesis pages that no other wiki page links to (hubs count), and
   pages missing from the catalog of any hub in their `domains`.
3. Missing pages: terms that appear under "Candidate concepts & entities" in at least two source
   summaries, or are linked but do not exist (broken links), with no page (search aliases first).
4. Stale syntheses: `as_of` older than the newest source that cites a page the synthesis builds on,
   or older than 90 days while its domains gained sources.
5. Unsupported claims: sentences with numbers or strong statements and no citation.
6. Schema violations: missing required properties, `tags` not equal to type tag plus `domains`,
   non-ISO dates, nested front matter, non-kebab-case file names, links that are not full vault paths.
7. Bookkeeping drift: ticked queue lines whose summary still says `uncompiled` (or the reverse),
   hubs over the split threshold, hubs missing from `wiki/index.md`, a stale stats line.

Log `op = lint` with the counts, commit only `wiki/log.md` (`compile: lint <YYYY-MM-DD>`), and stop.

**Phase 2, only after the user confirms** (all findings, or the numbers they name): apply the
fixes, log `op = lint-fix` listing what was fixed, then diff review and
`git commit -m "compile: lint-fix <YYYY-MM-DD>"`. Never delete a page during lint without explicit
confirmation for that page; prefer merging it into the surviving page and leaving the file name as
an alias.

### 11.4 Query

> Answer <question> from the wiki, citing pages; if the answer is novel, propose a `syntheses/` page.

1. Navigate index -> hubs -> pages (section 3). Open source summaries or raw only to confirm
   specifics.
2. Answer with citations: link the wiki pages you used and the source pages for specific claims.
   Say plainly what the wiki does not cover; do not fill gaps from general knowledge without
   labelling it as such.
3. If the answer combines two or more pages into something no single page says, end with
   `Proposed synthesis: wiki/syntheses/<name>` and a 3 to 5 line outline. Create it only if the
   user says yes (template 4.4, catalog lines in its hubs, log `op = synthesis`,
   commit `compile: synthesis <name> <YYYY-MM-DD>`).
4. Log the query (`op = query`, scope = the domains consulted). If nothing else changed, commit
   only `wiki/log.md` with `compile: query <YYYY-MM-DD>`; no diff review needed for a log-only commit.

### 11.5 Anything else

Any other request ("add a page about X from these sources", "merge these two pages", "rename this
concept") follows the same rules: sources only, templates, hubs, one log line with `op = manual`,
diff review, and `git commit -m "compile: manual <YYYY-MM-DD>"`. A rename keeps the old file name
in `aliases` and updates every link to the page (grep for `[[wiki/concepts/<old-name>` first).

## 12. Git discipline

**Preflight (start of every session):**

1. Snapshot the tree before you touch anything, and show it:
   `git status --porcelain --untracked-files=all > "${TMPDIR:-/tmp}/kb-preflight.txt"`
2. Classify what it lists:
   - Under `digests/`, `notes/` or `.obsidian/`: expected. The user writes in daily notes and in
     their own notes, and Obsidian rewrites its config; the engine deliberately never auto-commits
     those edits. Mention them once and carry on. Never stage, commit, stash or discard them, and
     never open them for writing.
   - Anywhere else (`wiki/`, `raw/`, root files): someone has uncommitted work where you are about
     to write. List it and ask the user whether to continue. Do not stash, commit, discard or
     "clean up" changes you did not make.
3. `git log --oneline -5` to see the latest engine (`auto:`) and session (`compile:`) commits.

**End of session:**

1. Guard: no new changes under the protected folders. Run
   `git status --porcelain --untracked-files=all -- raw digests notes .obsidian | grep -vxFf "${TMPDIR:-/tmp}/kb-preflight.txt"`
   Every line it prints is a protected path that changed during the session. Sort each one by
   who changed it, using your own record of the files you wrote (not the path's location):
   - **You wrote it** (a tracked file you edited): restore it with `git checkout -- <path>`.
   - **You created it** (shown as `??`, and you created it this session): delete it with
     `rm -- <path>` (checkout cannot restore a file that never existed).
   - **You did not touch it**: the user (a daily-note edit in Obsidian) or the engine (a `kb run`
     in another terminal) changed it while you worked. Leave it exactly as it is: never check
     out, delete, stage or commit it. Just list it for the user.
   If you are not certain you wrote a file, treat it as not yours. Tell the user about any file
   you restored or deleted: that means you broke the protected-folders rule.
2. Show `git diff --stat` and a short summary of every created and changed page. Wait for the
   user's approval. Apply requested changes, then show the diff again.
3. Stage exactly the files you changed, by path (`git add -- <path> <path> ...`). Never
   `git add -A`, `git add .` or `git commit -a`: the engine or the user may have written other files
   meanwhile.
4. Commit with the message for the session type:
   - `compile: <domain> <YYYY-MM-DD>`
   - `compile: cross-domain <domain-a>+<domain-b> <YYYY-MM-DD>`
   - `compile: lint <YYYY-MM-DD>` / `compile: lint-fix <YYYY-MM-DD>`
   - `compile: synthesis <name> <YYYY-MM-DD>`
   - `compile: query <YYYY-MM-DD>`
   - `compile: manual <YYYY-MM-DD>`
5. Never push, amend, rebase, reset, force, or pass `--no-verify`. Pushing is the user's call.

## 13. End-of-session checklist

- [ ] No new changes under `raw/`, `digests/`, `notes/`, `.obsidian/` compared with the preflight snapshot.
- [ ] Every new or changed claim cites a `wiki/sources/` page, verified against raw.
- [ ] Every link is a full vault path; no bare basenames.
- [ ] New pages: template followed, `tags` = type tag + `domains`, listed in every matching hub.
- [ ] Compiled sources: queue line ticked, `status: compiled`, `compiled:` date set.
- [ ] Index stats line recomputed; any missing hub added to the index.
- [ ] One log line appended.
- [ ] Diff shown, user approved, commit made with the right message.
