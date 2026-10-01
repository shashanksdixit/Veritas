"""Configuration layer (T006): env vars + TOML config files.

Priority, lowest to highest:
  1. built-in defaults (constants.py)
  2. `.veritas/config.toml`            (committed, no secrets — constitution)
  3. `.veritas/config.local.toml`      (gitignored, local overrides)
  4. environment variables `VERITAS_*` (highest)

The default config file MUST NOT contain API keys/tokens (constitution Privacy &
Data Handling); the gitignored local file may.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import get_origin

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from veritas.config.constants import (
    CONFIG_PATH,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LOCAL_CONFIG_PATH,
)

_SECRET_FIELDS = frozenset({"api_key", "github_token", "gitlab_token"})

# Immutable module-level default for [review] exclude (FR-029): a pattern ending
# in "/" matches every path under that directory prefix. Copied per Settings
# instance, never shared.
DEFAULT_EXCLUDE: tuple[str, ...] = (".specify/",)


class Settings(BaseSettings):
    """Effective Veritas configuration.

    Names are the canonical (non-prefixed) forms; the matching env variables
    are shown in the field comments.
    """

    model_config = SettingsConfigDict(
        env_prefix="VERITAS_",
        env_file=None,
        extra="ignore",
        populate_by_name=True,
    )

    api_key: str | None = Field(default=None, description="VERITAS_API_KEY")
    base_url: str = Field(default=DEFAULT_BASE_URL, description="VERITAS_BASE_URL")
    model: str | None = Field(default=None, description="VERITAS_MODEL")
    zdr: bool = Field(default=False, description="VERITAS_ZDR")
    github_token: str | None = Field(default=None, description="VERITAS_GITHUB_TOKEN")
    gitlab_token: str | None = Field(default=None, description="VERITAS_GITLAB_TOKEN")
    gitlab_url: str = Field(default="https://gitlab.com", description="VERITAS_GITLAB_URL")
    provider: str = Field(default="github")  # hosting provider: github | gitlab
    # [review] section (FR-029)
    exclude: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE),
        description="VERITAS_EXCLUDE (JSON list, e.g. '[\".specify/\"]')",
    )
    batch_chars: int = Field(default=48000, ge=1000, description="VERITAS_BATCH_CHARS")
    max_batches: int = Field(default=8, ge=1, description="VERITAS_MAX_BATCHES")

    @property
    def model_runtime(self) -> str:
        """Resolved model string (defaults applied)."""
        return self.model if self.model else DEFAULT_MODEL

    @property
    def config_hash(self) -> str:
        """Deterministic hash of the effective config, excluding secrets.

        Recorded in ReviewRun.config_hash for determinism audit (FR-025).
        """
        payload = {
            key: value
            for key, value in self.model_dump().items()
            if key not in _SECRET_FIELDS
        }
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - config errors surface in CLI
        raise ValueError(f"invalid config file {path}: {exc}") from exc


def _flatten_toml(data: dict) -> dict:
    """Map the TOML sections onto Settings field names."""
    flat: dict = {}
    llm = data.get("llm", {})
    flat["api_key"] = llm.get("api_key")
    flat["base_url"] = llm.get("base_url")
    flat["model"] = llm.get("model")
    flat["zdr"] = llm.get("zdr")
    hosting = data.get("hosting", {})
    flat["provider"] = hosting.get("provider")
    flat["gitlab_url"] = hosting.get("gitlab_url")
    flat["github_token"] = hosting.get("github_token")
    flat["gitlab_token"] = hosting.get("gitlab_token")
    review = data.get("review", {})
    flat["exclude"] = review.get("exclude")
    flat["batch_chars"] = review.get("batch_chars")
    flat["max_batches"] = review.get("max_batches")
    return {k: v for k, v in flat.items() if v is not None}


def _env_overrides() -> dict:
    """Collect defined ``VERITAS_*`` env values so env beats TOML files.

    pydantic-settings lets explicit init kwargs win over env vars; by passing
    env values into the final Settings construction last, env remains the
    highest-priority layer per the documented ordering (contracts/cli.md).

    List-valued fields (``exclude``) arrive from the environment in the JSON
    array form pydantic-settings requires for complex types, and are decoded
    here so env handling matches that format rather than a bespoke syntax.
    """
    overrides: dict = {}
    for field_name, field in Settings.model_fields.items():
        env_name = f"VERITAS_{field_name.upper()}"
        value = os.environ.get(env_name)
        if value is None:
            continue
        if get_origin(field.annotation) is list and not isinstance(value, list):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError(
                    f"{env_name} must be a JSON array of strings, "
                    f"for example {env_name}='[\".specify/\"]'; got: {value}"
                ) from None
        overrides[field_name] = value
    return overrides


def load_settings(config_path: str | None = None) -> Settings:
    """Load effective settings, merging TOML files then letting env win."""
    merged: dict = {}
    if config_path:
        merged.update(_flatten_toml(_read_toml(Path(config_path))))
    else:
        merged.update(_flatten_toml(_read_toml(Path(CONFIG_PATH))))
        merged.update(_flatten_toml(_read_toml(Path(LOCAL_CONFIG_PATH))))
    merged.update(_env_overrides())
    return Settings(**merged)