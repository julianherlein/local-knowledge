"""Configuration: `$KB_HOME/config.toml` + `$KB_HOME/.env` + environment variables.

Precedence (highest first): environment (`KB_` prefix, `__` for nesting, e.g.
`KB_LLM__MODEL`), then `$KB_HOME/.env`, then `config.toml`, then defaults.
Secrets use their conventional unprefixed names (`TELEGRAM_BOT_TOKEN`, `X_CLIENT_ID`,
`X_CLIENT_SECRET`) and should live in `.env`, never in config.toml.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_MODEL = "claude-opus-5-5"


class Domain(BaseModel):
    name: str
    description: str
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("hashtags")
    @classmethod
    def _norm_tags(cls, v: list[str]) -> list[str]:
        return [t.lstrip("#").lower() for t in v]


DEFAULT_DOMAINS: list[dict[str, Any]] = [
    {
        "name": "data-engineering",
        "description": (
            "Building and operating data platforms: batch and streaming pipelines, orchestration "
            "(Airflow, Dagster, Prefect), warehouses and lakehouses (Snowflake, BigQuery, Databricks, "
            "Iceberg, Delta), dbt and SQL modeling, data quality, CDC, Kafka/Flink/Spark, and the "
            "economics of storing and moving data."
        ),
        "hashtags": ["de", "data"],
    },
    {
        "name": "ai-llms",
        "description": (
            "Machine learning and large language models: model releases and capabilities, prompting, "
            "agents and tool use, RAG, evals, fine-tuning, inference and serving, AI coding tools, "
            "AI research papers, and the AI industry."
        ),
        "hashtags": ["ai", "llm"],
    },
    {
        "name": "software",
        "description": (
            "General software engineering craft: programming languages, libraries and frameworks, "
            "code quality, testing, debugging, developer tooling, version control, career and team "
            "practices. Use when the piece is about writing or maintaining code rather than about "
            "the architecture of large systems or about data platforms."
        ),
        "hashtags": ["swe", "dev"],
    },
    {
        "name": "tennis",
        "description": (
            "Tennis: technique and biomechanics, drills, tactics and match strategy, equipment, "
            "fitness for tennis, professional tours and players, and match analysis."
        ),
        "hashtags": ["tennis"],
    },
    {
        "name": "system-design",
        "description": (
            "Architecture of large distributed systems: scalability, reliability, consistency models, "
            "databases internals, caching, queues, replication and sharding, API design, "
            "microservices, incident post-mortems, and system design interviews."
        ),
        "hashtags": ["sd", "sysdesign"],
    },
]


class LLMSettings(BaseModel):
    backend: str = "claude_code"
    model: str = DEFAULT_MODEL
    binary: str = "claude"
    effort: str | None = None
    timeout_s: float = 600.0
    max_input_tokens: int = 40_000
    """Source bodies longer than this are cut to head + tail before summarizing (§12)."""
    classifier_excerpt_tokens: int = 2_000


class TaggerSettings(BaseModel):
    threshold: float = 0.6


class RunSettings(BaseModel):
    max_items: int | None = None
    """Items processed per `kb run`. None = everything pending (manual-run default)."""
    max_attempts: int = 3


class WebSettings(BaseModel):
    timeout_s: float = 20.0
    min_chars: int = 500
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )


class YouTubeSettings(BaseModel):
    sub_langs: list[str] = Field(default_factory=lambda: ["en.*", "es.*"])
    marker_every_s: int = 60


class TelegramSettings(BaseModel):
    enabled: bool = True
    allowed_chat_ids: list[int] = Field(default_factory=list)
    stale_warning_hours: float = 20.0
    """`kb status` warns when the last successful poll is older than this (updates expire at ~24h)."""


class XSettings(BaseModel):
    enabled: bool = True
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    scopes: list[str] = Field(default_factory=lambda: ["tweet.read", "users.read", "bookmark.read", "offline.access"])
    max_bookmark_pages: int = 40
    """Safety cap on pages walked per poll (40 x 20 = 800, the API's bookmark cap)."""
    backfill_fetch_via_api: bool = False
    """Backfill items use the export's text by default (free). True re-fetches each post ($0.005)."""
    use_keyring: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KB_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    home: Path = Field(default_factory=lambda: default_home())
    vault_path: Path = Field(default_factory=lambda: Path.home() / "knowledge")
    domains: list[Domain] = Field(default_factory=lambda: [Domain(**d) for d in DEFAULT_DOMAINS])

    llm: LLMSettings = Field(default_factory=LLMSettings)
    tagger: TaggerSettings = Field(default_factory=TaggerSettings)
    run: RunSettings = Field(default_factory=RunSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    youtube: YouTubeSettings = Field(default_factory=YouTubeSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    x: XSettings = Field(default_factory=XSettings)

    telegram_bot_token: str | None = Field(
        default=None, validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "KB_TELEGRAM_BOT_TOKEN")
    )
    x_client_id: str | None = Field(default=None, validation_alias=AliasChoices("X_CLIENT_ID", "KB_X_CLIENT_ID"))
    x_client_secret: str | None = Field(
        default=None, validation_alias=AliasChoices("X_CLIENT_SECRET", "KB_X_CLIENT_SECRET")
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # init kwargs carry config.toml, so they rank below env and .env.
        return (env_settings, dotenv_settings, init_settings)

    @field_validator("home", "vault_path", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(os.path.expandvars(str(v))).expanduser()

    # Derived paths -----------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.home / "kb.sqlite"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def lock_path(self) -> Path:
        return self.home / "kb.lock"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    @property
    def domain_names(self) -> list[str]:
        return [d.name for d in self.domains]

    def hashtag_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for d in self.domains:
            out[d.name.lower()] = d.name
            for t in d.hashtags:
                out[t] = d.name
        return out


def default_home() -> Path:
    return Path(os.environ.get("KB_HOME") or (Path.home() / ".kb")).expanduser()


def load_settings(home: Path | None = None, **overrides: Any) -> Settings:
    """Load settings for `home` (default `$KB_HOME` or `~/.kb`). `overrides` beat config.toml."""
    home = Path(home).expanduser() if home else default_home()
    data: dict[str, Any] = {}
    cfg = home / "config.toml"
    if cfg.exists():
        with cfg.open("rb") as f:
            data = tomllib.load(f)
    data.update(overrides)
    data["home"] = home
    env_file = home / ".env"
    return Settings(_env_file=env_file if env_file.exists() else None, **data)


CONFIG_TEMPLATE = """\
# kb-engine configuration. Secrets go in .env next to this file, not here.

vault_path = "{vault_path}"

[llm]
backend = "claude_code"          # local Claude Code CLI; no API key needed
model = "{model}"                # e.g. "claude-haiku-4-5-20251001" to trade quality for speed/quota
max_input_tokens = 40000         # longer sources are cut to head + tail

[tagger]
threshold = 0.6                  # below this the item is tagged `unsorted`

[run]
# max_items = 10                 # per `kb run`; unset = process everything pending
max_attempts = 3

[telegram]
enabled = true
allowed_chat_ids = []            # your chat id; `kb doctor` prints it after you message the bot

[x]
enabled = true
redirect_uri = "http://127.0.0.1:8765/callback"   # must match the callback in your X app settings

# Domains: edit descriptions to steer the classifier. Hashtags are the manual override.
{domains}
"""


def render_config_template(vault_path: Path, model: str = DEFAULT_MODEL) -> str:
    blocks = []
    for d in DEFAULT_DOMAINS:
        tags = ", ".join(f'"{t}"' for t in d["hashtags"])
        blocks.append(
            f'[[domains]]\nname = "{d["name"]}"\nhashtags = [{tags}]\ndescription = """\n{d["description"]}\n"""\n'
        )
    return CONFIG_TEMPLATE.format(vault_path=vault_path.as_posix(), model=model, domains="\n".join(blocks))


ENV_TEMPLATE = """\
# kb-engine secrets. Never commit this file.
TELEGRAM_BOT_TOKEN=
X_CLIENT_ID=
# X_CLIENT_SECRET=   # only for confidential X apps; public PKCE apps leave it unset
"""
