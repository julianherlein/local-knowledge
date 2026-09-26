"""`kb init`: skeleton contents, idempotence, non-destructiveness, Obsidian/Bases validity, git."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from conftest import git

from kb import init as kbinit
from kb.config import Domain, load_settings
from kb.init import init_vault

# Every function and method called inside a .base expression must be one of these. Each was
# checked against https://help.obsidian.md/bases/functions (global: list, today, now;
# file: inFolder, hasTag). Fields such as `.length`, `.days` and `file.backlinks` are
# properties, not calls, and are covered by the syntax page.
VERIFIED_BASES_CALLS = {"inFolder", "hasTag", "list", "today", "now"}
REQUIRED_CORE_PLUGINS = [
    "backlink",
    "outgoing-link",
    "graph",
    "properties",
    "bases",
    "tag-pane",
    "global-search",
    "daily-notes",
    "bookmarks",
    "file-explorer",
    "page-preview",
]


def _snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


@pytest.fixture
def fresh(tmp_path):
    """Settings pointing at a vault folder that does not exist yet (path with a space)."""
    return load_settings(tmp_path / "home", vault_path=str(tmp_path / "my vault"))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One skeleton shared by the read-only inspection tests (they must not modify it)."""
    base = tmp_path_factory.mktemp("built")
    s = load_settings(base / "home", vault_path=str(base / "my vault"))
    init_vault(s, commit=False)
    return s


def test_creates_full_skeleton_without_git(fresh):
    created = init_vault(fresh, commit=False)
    root = fresh.vault_path
    for rel in [
        "CLAUDE.md",
        ".gitignore",
        ".gitattributes",
        "wiki/index.md",
        "wiki/log.md",
        "wiki/_compile-queue.md",
        "wiki/_bases/compile-queue.base",
        "wiki/_bases/recent-sources.base",
        "wiki/_bases/hot-concepts.base",
        ".obsidian/core-plugins.json",
        ".obsidian/daily-notes.json",
        ".obsidian/graph.json",
        ".obsidian/app.json",
        ".obsidian/bookmarks.json",
        ".obsidian/types.json",
        "raw/x/.gitkeep",
        "raw/youtube/.gitkeep",
        "raw/web/.gitkeep",
        "wiki/sources/.gitkeep",
        "wiki/concepts/.gitkeep",
        "wiki/entities/.gitkeep",
        "wiki/syntheses/.gitkeep",
        "digests/daily/.gitkeep",
        "digests/weekly/.gitkeep",
        "notes/.gitkeep",
    ]:
        assert rel in created, rel
        assert (root / rel).is_file(), rel
    for d in fresh.domain_names:
        assert f"wiki/domains/{d}.md" in created
        assert f"wiki/_bases/{d}.base" in created
    assert sorted(created) == sorted(_snapshot(root))  # reports exactly what it wrote
    assert not (root / ".git").exists()


def test_second_run_creates_nothing_and_changes_no_bytes(fresh):
    init_vault(fresh, commit=False)
    before = _snapshot(fresh.vault_path)
    assert init_vault(fresh, commit=False) == []
    assert _snapshot(fresh.vault_path) == before


def test_user_edits_are_never_overwritten(fresh):
    init_vault(fresh, commit=False)
    root = fresh.vault_path
    (root / "CLAUDE.md").write_text("my own rules\n", encoding="utf-8")
    (root / "wiki/domains/tennis.md").write_text("curated hub\n", encoding="utf-8")
    (root / "wiki/_compile-queue.md").unlink()
    created = init_vault(fresh, commit=False)
    assert created == ["wiki/_compile-queue.md"]  # a deleted file is restored, nothing else
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "my own rules\n"
    assert (root / "wiki/domains/tennis.md").read_text(encoding="utf-8") == "curated hub\n"


def test_sixth_domain_gets_hub_and_base_but_graph_is_left_alone(fresh):
    init_vault(fresh, commit=False)
    graph_before = (fresh.vault_path / ".obsidian/graph.json").read_bytes()
    fresh.domains.append(Domain(name="cooking", description="Recipes and technique."))
    created = init_vault(fresh, commit=False)
    assert sorted(created) == ["wiki/_bases/cooking.base", "wiki/domains/cooking.md"]
    hub = (fresh.vault_path / "wiki/domains/cooking.md").read_text(encoding="utf-8")
    assert "tags: [hub, cooking]" in hub and "> Recipes and technique." in hub
    assert (fresh.vault_path / ".obsidian/graph.json").read_bytes() == graph_before


@pytest.mark.parametrize("bad", ["Data Engineering", "ai/llms", "123", "x--y", "unsorted", 'q"uote'])
def test_domain_names_must_be_kebab_case(fresh, bad):
    fresh.domains.append(Domain(name=bad, description="x"))
    with pytest.raises(kbinit.InitError):
        init_vault(fresh, commit=False)


def test_all_written_files_use_lf(built):
    fresh = built
    for p in fresh.vault_path.rglob("*"):
        if p.is_file():
            assert b"\r\n" not in p.read_bytes(), p


def test_no_em_dashes_in_generated_text(built):
    fresh = built
    for p in fresh.vault_path.rglob("*"):
        if p.is_file() and p.name != "_compile-queue.md":  # header text owned by writer.py
            assert "—" not in p.read_text(encoding="utf-8"), p


# Bases ---------------------------------------------------------------------------------
def _expressions(node) -> list[str]:
    """Every filter statement and formula in a parsed .base."""
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            out += _expressions(x)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in ("and", "or", "not", "filters"):
                out += _expressions(v)
    return out


def test_bases_are_valid_yaml_using_only_verified_functions(built):
    fresh = built
    bases = sorted((fresh.vault_path / "wiki/_bases").glob("*.base"))
    assert len(bases) == len(fresh.domains) + 3
    for path in bases:
        base = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "https://help.obsidian.md/bases/syntax" in path.read_text(encoding="utf-8")
        exprs = _expressions(base.get("filters"))
        exprs += list((base.get("formulas") or {}).values())
        formulas = set(base.get("formulas") or {})
        assert base["views"], path
        for view in base["views"]:
            assert view["type"] == "table" and view["name"] and view["order"], (path, view)
            exprs += _expressions(view.get("filters"))
            for s in view.get("sort", []):
                assert set(s) == {"property", "direction"} and s["direction"] in ("ASC", "DESC")
            if "groupBy" in view:
                assert set(view["groupBy"]) == {"property", "direction"}
            for prop in [*view["order"], *(s["property"] for s in view.get("sort", []))]:
                if prop.startswith("formula."):
                    assert prop.removeprefix("formula.") in formulas, (path, prop)
        for expr in exprs:
            assert isinstance(expr, str) and expr.count('"') % 2 == 0, (path, expr)
            calls = set(re.findall(r"([A-Za-z_]\w*)\s*\(", expr))
            assert calls <= VERIFIED_BASES_CALLS, (path, expr, calls - VERIFIED_BASES_CALLS)


def test_domain_base_filters_on_its_tag(built):
    fresh = built
    base = yaml.safe_load((fresh.vault_path / "wiki/_bases/tennis.base").read_text(encoding="utf-8"))
    assert base["filters"] == {"and": ['file.inFolder("wiki")', 'file.hasTag("tennis")']}
    assert [v["name"] for v in base["views"]] == [
        "Concepts & entities",
        "Uncompiled sources",
        "Syntheses",
        "Recent sources",
    ]


def test_hubs_and_index_embed_views_that_exist(built):
    fresh = built
    root = fresh.vault_path
    pages = [root / "wiki/index.md", *(root / f"wiki/domains/{d}.md" for d in fresh.domain_names)]
    for page in pages:
        text = page.read_text(encoding="utf-8")
        embeds = re.findall(r"!\[\[(wiki/_bases/[^#\]]+\.base)#([^\]]+)\]\]", text)
        assert embeds, page
        for base_rel, view in embeds:
            base = yaml.safe_load((root / base_rel).read_text(encoding="utf-8"))
            assert view in [v["name"] for v in base["views"]], (page, view)
    hub = (root / "wiki/domains/tennis.md").read_text(encoding="utf-8")
    assert "![[wiki/_bases/tennis.base#Concepts & entities]]" in hub
    assert hub.startswith("---\ntype: hub\ndomains: [tennis]\ntags: [hub, tennis]\n")
    for heading in ("## Overview", "## Key concepts", "## Catalog", "## Live views"):
        assert heading in hub
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    for d in fresh.domain_names:
        assert f"- [[wiki/domains/{d}]]: " in index
    assert "**Stats:** 0 sources (0 uncompiled) | 0 concepts | 0 entities | 0 syntheses | last compile: never" in index


# Obsidian config -----------------------------------------------------------------------
def test_obsidian_config(built):
    fresh = built
    obs = fresh.vault_path / ".obsidian"
    for p in obs.glob("*.json"):
        json.loads(p.read_text(encoding="utf-8"))
    core = json.loads((obs / "core-plugins.json").read_text(encoding="utf-8"))
    for plugin in REQUIRED_CORE_PLUGINS:
        assert core[plugin] is True, plugin
    assert json.loads((obs / "daily-notes.json").read_text(encoding="utf-8")) == {
        "folder": "digests/daily",
        "format": "YYYY-MM-DD",
    }
    app = json.loads((obs / "app.json").read_text(encoding="utf-8"))
    assert app["newLinkFormat"] == "absolute" and app["alwaysUpdateLinks"] is True
    graph = json.loads((obs / "graph.json").read_text(encoding="utf-8"))
    assert graph["search"] == "-path:raw -path:digests -path:wiki/domains"
    colors = {g["query"]: g["color"]["rgb"] for g in graph["colorGroups"]}
    assert colors == {
        "tag:#data-engineering": 3900150,
        "tag:#ai-llms": 10181046,
        "tag:#software": 3066993,
        "tag:#tennis": 15844367,
        "tag:#system-design": 15105570,
    }
    bookmarks = json.loads((obs / "bookmarks.json").read_text(encoding="utf-8"))["items"]
    searches = {b["title"]: b["query"] for b in bookmarks if b["type"] == "search"}
    assert searches["Concepts only"] == "path:wiki/concepts OR path:wiki/entities"
    types = json.loads((obs / "types.json").read_text(encoding="utf-8"))["types"]
    assert types["captured"] == "date"  # recent-sources.base compares it with today() - "14d"
    gitignore = (fresh.vault_path / ".gitignore").read_text(encoding="utf-8")
    for entry in (".obsidian/workspace.json", ".obsidian/workspace-mobile.json", ".trash/", ".DS_Store"):
        assert entry in gitignore.splitlines()
    assert "* text=auto eol=lf" in (fresh.vault_path / ".gitattributes").read_text(encoding="utf-8")


def test_template_inserts_values_literally_and_rejects_missing_ones():
    hub = kbinit.render_hub(Domain(name="chess", description="Openings {{TODAY}} and {{NOPE}}."), date(2026, 9, 26))
    assert "> Openings {{TODAY}} and {{NOPE}}." in hub and "created: 2026-09-26" in hub
    with pytest.raises(KeyError, match="DOMAIN"):
        kbinit.template("hub.md", TITLE="x", DESCRIPTION="x", TODAY="x")


def test_extra_domain_color_is_deterministic_and_distinct():
    c = kbinit.domain_color("cooking")
    assert c == kbinit.domain_color("cooking")
    assert 0 <= c <= 0xFFFFFF and c not in kbinit.SDD_COLORS.values()
    assert kbinit.domain_color("cooking") != kbinit.domain_color("chess")


# Vault CLAUDE.md -----------------------------------------------------------------------
def test_vault_claude_md_states_the_key_rules(built):
    fresh = built
    text = (fresh.vault_path / "CLAUDE.md").read_text(encoding="utf-8")
    required = [
        "**`raw/` is read-only.**",
        "[[wiki/sources/2026-09-26-why-we-moved-off-airflow]]",
        "[[wiki/concepts/idempotency]]",
        "Never write a bare basename",
        "- YYYY-MM-DD HH:MM | <op> | <scope> | <details>",
        "### 11.1 Compile a domain",
        "### 11.2 Cross-domain links",
        "### 11.3 Lint",
        "### 11.4 Query",
        "Report first, then fix after I confirm",
        'git commit -m "compile: <domain> <YYYY-MM-DD>"',
        "`kb tag <item_id> <domain>`",
        "status: compiled",
        "- [x]",
        "about 150 entries",
        "`mine`",
        "type: concept",
        "type: entity",
        "type: synthesis",
        "aliases:",
        "Stage exactly the files you changed",
    ]
    for phrase in required:
        assert phrase in text, phrase
    # The template is not named CLAUDE.md inside the engine repo, so engine sessions never load it.
    assert not (Path(kbinit.__file__).parent / "templates" / "CLAUDE.md").exists()


def test_log_template_line_matches_documented_format(built):
    fresh = built
    log = (fresh.vault_path / "wiki/log.md").read_text(encoding="utf-8")
    entries = [ln for ln in log.splitlines() if ln.startswith("- 20")]
    assert len(entries) == 1
    assert re.fullmatch(r"- \d{4}-\d{2}-\d{2} \d{2}:\d{2} \| manual \| all \| .+", entries[0])


# Git -----------------------------------------------------------------------------------
def test_git_new_repo_one_commit_with_fallback_author(fresh, tmp_path, monkeypatch):
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))  # no user.email anywhere
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    created = init_vault(fresh)
    root = fresh.vault_path
    assert git(root, "log", "--format=%s|%an").splitlines() == ["init: vault skeleton|kb-engine"]
    assert git(root, "branch", "--show-current").strip() == "main"
    committed = git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == sorted(created)
    assert git(root, "status", "--porcelain") == ""
    assert init_vault(fresh) == []
    assert len(git(root, "log", "--format=%s").splitlines()) == 1


def test_git_existing_repo_commits_only_created_files(settings, vault_dir):
    (vault_dir / "README.md").write_text("changed by the user\n", encoding="utf-8")
    (vault_dir / "notes").mkdir()
    (vault_dir / "notes" / "draft.md").write_text("mine\n", encoding="utf-8")
    (vault_dir / ".gitignore").write_text(".obsidian/\n", encoding="utf-8")  # the user ignores .obsidian
    git(vault_dir, "add", ".gitignore")  # staged but not committed: must not ride along
    created = init_vault(settings)
    assert ".gitignore" not in created and "notes/.gitkeep" not in created
    assert ".obsidian/app.json" in created  # written to disk...
    log = git(vault_dir, "log", "--format=%s").splitlines()
    assert log == ["init: vault skeleton", "init"]
    committed = set(git(vault_dir, "show", "--name-only", "--format=", "HEAD").split())
    assert committed == {p for p in created if not p.startswith(".obsidian/")}  # ...but ignored, so not committed
    status = git(vault_dir, "status", "--porcelain", "-uall").splitlines()
    assert " M README.md" in status and "?? notes/draft.md" in status and "A  .gitignore" in status
