"""Shared fixtures. Every test runs against a temp KB_HOME and a temp git vault; no network."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from kb_llm import FakeLLMClient, LLMRequest

from kb.config import Settings, load_settings
from kb.queue import Queue

FIXTURES = Path(__file__).parent / "fixtures"

_SECRET_ENV = ["TELEGRAM_BOT_TOKEN", "X_CLIENT_ID", "X_CLIENT_SECRET", "KB_HOME"]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith("KB_") or k in _SECRET_ENV:
            monkeypatch.delenv(k, raising=False)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture(scope="session")
def _vault_template(tmp_path_factory) -> Path:
    # git subprocesses are slow on Windows; build the repo once and copy it per test.
    v = tmp_path_factory.mktemp("template") / "vault"
    v.mkdir()
    git(v, "init", "-q", "-b", "main")
    git(v, "config", "user.email", "test@example.com")
    git(v, "config", "user.name", "Test")
    git(v, "config", "core.autocrlf", "false")
    (v / "README.md").write_text("vault\n", encoding="utf-8")
    git(v, "add", "README.md")
    git(v, "commit", "-qm", "init")
    return v


@pytest.fixture
def vault_dir(tmp_path, _vault_template) -> Path:
    v = tmp_path / "vault"
    shutil.copytree(_vault_template, v)
    return v


@pytest.fixture
def settings(tmp_path, vault_dir) -> Settings:
    return load_settings(
        tmp_path / "home",
        vault_path=str(vault_dir),
        telegram={"enabled": False},
        x={"enabled": False},
    )


@pytest.fixture
def queue(settings) -> Queue:
    q = Queue(settings.db_path)
    yield q
    q.close()


def classifier_or_summary(domains=("data-engineering",), confidence=0.9, language="en"):
    """A FakeLLMClient responder that answers both pipeline prompts."""

    def responder(req: LLMRequest):
        props = (req.json_schema or {}).get("properties", {})
        if "confidence" in props:
            return {"domains": list(domains), "confidence": confidence, "language": language, "reason": "test"}
        return {
            "title": "Why we moved off Airflow",
            "tldr": "The team replaced Airflow with Dagster.  It cut failures by 40%.",
            "key_points": ["Asset-based orchestration", "Backfills got simpler"],
            "claims": ["Failures dropped 40% [Results]"],
            "concepts": ["orchestration", "backfills"],
            "entities": ["Airflow", "Dagster"],
            "open_questions": [],
        }

    return responder


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient(classifier_or_summary())


@pytest.fixture
def http() -> httpx.Client:
    c = httpx.Client()
    yield c
    c.close()
