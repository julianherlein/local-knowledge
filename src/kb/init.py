"""`kb init`: vault skeleton, vault CLAUDE.md, hubs, Bases, .obsidian config (SDD §5, §9, §11).

Two properties matter more than anything else here:

* **Never destructive.** Every file is created only if it does not exist. Once the vault has
  been lived in, the user and compile sessions own these files (a curated hub, a tweaked
  .base, a CLAUDE.md rule they added), so re-running `kb init` must not change a byte of
  them. That also makes it the upgrade path: add a domain to config.toml, re-run, and only
  the new hub and .base appear.
* **Commit only what it created.** The vault may already hold uncommitted user work; `kb
  init` commits its own files with `git commit --only`-style pathspecs and nothing else.

graph.json is the one file that depends on the whole domain list. It is created once and
never merged afterwards (a JSON the user may have re-styled in Obsidian cannot be merged
safely), so a domain added later gets its hub and base but no graph color group; add it in
Obsidian's graph settings.

The text lives in `kb/templates/` (loaded with importlib.resources) so it can be read and
reviewed as the documents they are, not as Python string literals.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import re
import subprocess
from datetime import date, datetime
from importlib import resources
from pathlib import Path

from . import git_ops
from .config import Domain, Settings
from .textutil import one_line
from .vault import COMPILE_QUEUE, DIGEST_DAILY, DIGEST_WEEKLY, LOG, RAW_DIRS, SOURCES_DIR
from .writer import COMPILE_QUEUE_HEADER

INIT_COMMIT_MESSAGE = "init: vault skeleton"

DIRS = [
    *RAW_DIRS.values(),
    "wiki/domains",
    SOURCES_DIR,
    "wiki/concepts",
    "wiki/entities",
    "wiki/syntheses",
    "wiki/_bases",
    DIGEST_DAILY,
    DIGEST_WEEKLY,
    "notes",
]
BASES_DIR = "wiki/_bases"
GLOBAL_BASES = ["compile-queue.base", "recent-sources.base", "hot-concepts.base"]
OBSIDIAN_STATIC = ["core-plugins.json", "daily-notes.json", "app.json", "bookmarks.json", "types.json"]

# SDD §11.2 colors for the default domains; any other domain gets a hashed hue.
SDD_COLORS = {
    "data-engineering": 3900150,
    "ai-llms": 10181046,
    "software": 3066993,
    "system-design": 15105570,
    "tennis": 15844367,
}
DOMAIN_TITLES = {
    "data-engineering": "Data engineering",
    "ai-llms": "AI and LLMs",
    "software": "Software",
    "system-design": "System design",
    "tennis": "Tennis",
}
# The compile queue links every source, so it would be a giant hub node in the graph.
GRAPH_SEARCH = "-path:raw -path:digests -path:wiki/domains -file:_compile-queue"

# A domain becomes an Obsidian tag, a file name and a string inside .base expressions, so it
# must be kebab-case: no spaces, quotes or slashes, and not purely numeric (not a valid tag).
_DOMAIN_NAME = re.compile(r"^(?=.*[a-z])[a-z0-9]+(?:-[a-z0-9]+)*$")
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


class InitError(ValueError):
    pass


# Templates -----------------------------------------------------------------------------
def template(name: str, **values: str) -> str:
    """Load `kb/templates/<name>` and substitute `{{KEY}}` placeholders. Always LF."""
    text = resources.files("kb").joinpath("templates", *name.split("/")).read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n")  # a CRLF checkout of the engine must not leak into the vault

    def sub(m: re.Match[str]) -> str:
        if m.group(1) not in values:
            raise KeyError(f"template {name}: no value for {m.group(0)}")
        return values[m.group(1)]

    # One pass, so a value that happens to contain "{{...}}" is inserted literally.
    return _PLACEHOLDER.sub(sub, text)


def domain_title(name: str) -> str:
    return DOMAIN_TITLES.get(name) or name.replace("-", " ").capitalize()


def domain_color(name: str) -> int:
    """SDD color for the defaults; otherwise a stable hue from the name (same on every machine)."""
    if name in SDD_COLORS:
        return SDD_COLORS[name]
    hue = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.85)
    return (round(r * 255) << 16) | (round(g * 255) << 8) | round(b * 255)


def validate_domains(domains: list[Domain]) -> None:
    seen: set[str] = set()
    for d in domains:
        if not _DOMAIN_NAME.match(d.name):
            raise InitError(
                f"domain name {d.name!r} must be kebab-case (lowercase letters, digits, single dashes), "
                "because it becomes a tag and a file name; fix it in config.toml"
            )
        if d.name == "unsorted":
            raise InitError("'unsorted' is reserved for low-confidence items; pick another domain name")
        if d.name in seen:
            raise InitError(f"domain {d.name!r} is configured twice")
        seen.add(d.name)


def render_hub(d: Domain, today: date) -> str:
    return template(
        "hub.md",
        DOMAIN=d.name,
        TITLE=domain_title(d.name),
        DESCRIPTION=one_line(d.description),
        TODAY=today.isoformat(),
    )


def render_index(domains: list[Domain], today: date) -> str:
    lines = "\n".join(f"- [[wiki/domains/{d.name}]]: {one_line(d.description, 160)}" for d in domains)
    return template("index.md", DOMAIN_LINES=lines, TODAY=today.isoformat())


def render_graph(domains: list[Domain]) -> str:
    graph = {
        "collapse-filter": False,
        "search": GRAPH_SEARCH,
        "showTags": False,
        "showAttachments": False,
        "hideUnresolved": False,
        "showOrphans": False,
        "collapse-color-groups": False,
        "colorGroups": [{"query": f"tag:#{d.name}", "color": {"a": 1, "rgb": domain_color(d.name)}} for d in domains],
        "collapse-display": True,
        "showArrow": False,
        "textFadeMultiplier": 0,
        "nodeSizeMultiplier": 1,
        "lineSizeMultiplier": 1,
        "collapse-forces": True,
        "centerStrength": 0.5,
        "repelStrength": 10,
        "linkStrength": 1,
        "linkDistance": 250,
        "scale": 1,
        "close": False,
    }
    return json.dumps(graph, indent=2) + "\n"


def planned_files(settings: Settings, today: date, now: datetime) -> dict[str, str]:
    """Every file `kb init` would create, vault-relative path -> content, in creation order."""
    domains = settings.domains
    files: dict[str, str] = {
        "CLAUDE.md": template("vault-CLAUDE.md"),
        ".gitignore": template("gitignore"),
        ".gitattributes": template("gitattributes"),
        "wiki/index.md": render_index(domains, today),
        LOG: template("log.md", NOW=now.strftime("%Y-%m-%d %H:%M")),
        COMPILE_QUEUE: COMPILE_QUEUE_HEADER,
    }
    for d in domains:
        files[f"wiki/domains/{d.name}.md"] = render_hub(d, today)
    for d in domains:
        files[f"{BASES_DIR}/{d.name}.base"] = template("domain.base", DOMAIN=d.name)
    for name in GLOBAL_BASES:
        files[f"{BASES_DIR}/{name}"] = template(name)
    for name in OBSIDIAN_STATIC:
        files[f".obsidian/{name}"] = template(f"obsidian/{name}")
    files[".obsidian/graph.json"] = render_graph(domains)
    return files


# Git -----------------------------------------------------------------------------------
def commit_created(repo: Path, paths: list[str]) -> None:
    """Commit exactly `paths` (never anything else the user has staged or modified).

    Paths the repo's own .gitignore excludes (say the user ignores `.obsidian/`) are left out:
    `git add` refuses ignored paths, and the user's ignore rules win over ours.
    """
    ignored = (
        set(git_ops.run_git(repo, "check-ignore", "--", *paths, check=False).stdout.splitlines()) if paths else set()
    )
    paths = [p for p in paths if p not in ignored]
    if not paths:
        return
    git_ops.run_git(repo, "add", "--", *paths)
    author: list[str] = []
    if not git_ops.run_git(repo, "config", "user.email", check=False).stdout.strip():
        author = ["-c", f"user.name={git_ops.AUTHOR[0]}", "-c", f"user.email={git_ops.AUTHOR[1]}"]
    proc = subprocess.run(
        ["git", "-C", str(repo), *author, "commit", "-q", "-m", INIT_COMMIT_MESSAGE, "--", *paths],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise git_ops.GitError(f"git commit failed: {proc.stderr.strip() or proc.stdout.strip()}")


# Entry point ---------------------------------------------------------------------------
def init_vault(settings: Settings, *, today: date | None = None, commit: bool = True) -> list[str]:
    """Create whatever part of the vault skeleton is missing. Returns the vault-relative paths created."""
    validate_domains(settings.domains)
    root = settings.vault_path
    now = datetime.now().astimezone()
    today = today or now.date()
    root.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    for rel, text in planned_files(settings, today, now).items():
        path = root / rel
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # bytes: no newline translation on Windows
        created.append(rel)
    for rel in DIRS:
        folder = root / rel
        folder.mkdir(parents=True, exist_ok=True)
        if not any(folder.iterdir()):  # git does not track empty folders
            (folder / ".gitkeep").write_bytes(b"")
            created.append(f"{rel}/.gitkeep")

    if commit:
        git_ops.init_repo(root)
        commit_created(root, created)
    return created
