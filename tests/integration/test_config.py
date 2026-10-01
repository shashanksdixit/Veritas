"""Integration tests — configuration precedence (T006)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from veritas.config.settings import Settings, load_settings

# Every VERITAS_* env var `load_settings` reads, in `Settings.model_fields`
# order (see veritas/config/settings.py: `_env_overrides` and the
# `env_prefix="VERITAS_"` model config). Cleared before every test so a
# developer's shell cannot leak into these precedence assertions; tests that
# set a var deliberately do so after this fixture runs, via monkeypatch.
_VERITAS_ENV_VARS = (
    "VERITAS_API_KEY",
    "VERITAS_BASE_URL",
    "VERITAS_MODEL",
    "VERITAS_ZDR",
    "VERITAS_GITHUB_TOKEN",
    "VERITAS_GITLAB_TOKEN",
    "VERITAS_GITLAB_URL",
    "VERITAS_PROVIDER",
    "VERITAS_EXCLUDE",
    "VERITAS_BATCH_CHARS",
    "VERITAS_MAX_BATCHES",
)


@pytest.fixture(autouse=True)
def _clean_veritas_env(monkeypatch):
    for name in _VERITAS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\napi_key = "toml-key"\nmodel = "openai:toml/model"\nbase_url = "https://example.com/v1"\nzdr = true\n'
        '[hosting]\nprovider = "gitlab"\ngitlab_url = "https://gitlab.example.com"\n',
        encoding="utf-8",
    )
    return path


def test_file_values_loaded(tmp_path):
    path = _write_config(tmp_path)
    settings = load_settings(str(path))
    assert settings.api_key == "toml-key"
    assert settings.model_runtime == "openai:toml/model"
    assert settings.base_url == "https://example.com/v1"
    assert settings.zdr is True
    assert settings.provider == "gitlab"
    assert settings.gitlab_url == "https://gitlab.example.com"


def test_env_overrides_file(tmp_path, monkeypatch):
    path = _write_config(tmp_path)
    monkeypatch.setenv("VERITAS_MODEL", "openai:env/model")
    monkeypatch.setenv("VERITAS_ZDR", "false")
    settings = load_settings(str(path))
    assert settings.model_runtime == "openai:env/model"
    assert settings.zdr is False


def test_defaults_apply():
    settings = Settings()
    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.model_runtime == "openai:openai/gpt-4o-mini"
    assert settings.zdr is False
    assert settings.provider == "github"


def test_config_hash_excludes_secrets():
    a = Settings(api_key="secret-a")
    b = Settings(api_key="different-secret")
    assert a.config_hash == b.config_hash


def test_config_hash_changes_with_model():
    a = Settings(model="openai:model-a")
    b = Settings(model="openai:model-b")
    assert a.config_hash != b.config_hash


def test_missing_config_ignored(tmp_path):
    settings = load_settings(str(tmp_path / "nope.toml"))
    assert settings.model_runtime == "openai:openai/gpt-4o-mini"


def _write_review_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "review-config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_review_defaults_apply():
    settings = Settings()
    assert settings.exclude == [".specify/"]
    assert settings.batch_chars == 48000
    assert settings.max_batches == 8


def test_review_section_overrides_defaults(tmp_path):
    path = _write_review_config(
        tmp_path,
        '[review]\nexclude = ["vendor/", "*.min.js"]\nbatch_chars = 12000\nmax_batches = 4\n',
    )
    settings = load_settings(str(path))
    assert settings.exclude == ["vendor/", "*.min.js"]
    assert settings.batch_chars == 12000
    assert settings.max_batches == 4


def test_review_empty_exclude_list_disables_exclusion(tmp_path):
    """An explicit empty list in the file is not treated as absent (FR-029)."""
    path = _write_review_config(tmp_path, "[review]\nexclude = []\n")
    assert load_settings(str(path)).exclude == []


def test_default_exclude_list_not_shared_between_instances():
    first, second = Settings(), Settings()
    assert first.exclude is not second.exclude
    first.exclude.append("mutated/")
    assert second.exclude == [".specify/"]
    assert Settings().exclude == [".specify/"]


def test_env_overrides_review_section(tmp_path, monkeypatch):
    path = _write_review_config(
        tmp_path,
        '[review]\nexclude = ["from-file/"]\nbatch_chars = 12000\nmax_batches = 4\n',
    )
    # A list field reads from the environment in the JSON array form
    # pydantic-settings requires for complex types.
    monkeypatch.setenv("VERITAS_EXCLUDE", '["from-env/", "*.min.js"]')
    monkeypatch.setenv("VERITAS_BATCH_CHARS", "9000")
    monkeypatch.setenv("VERITAS_MAX_BATCHES", "2")
    settings = load_settings(str(path))
    assert settings.exclude == ["from-env/", "*.min.js"]
    assert settings.batch_chars == 9000
    assert settings.max_batches == 2


def test_env_exclude_not_json_rejected(monkeypatch):
    monkeypatch.setenv("VERITAS_EXCLUDE", ".specify/")
    with pytest.raises(ValueError) as excinfo:
        load_settings("nope.toml")
    message = str(excinfo.value)
    assert "VERITAS_EXCLUDE" in message
    assert "JSON array" in message
    assert ".specify/" in message


def test_env_exclude_json_non_list_rejected(monkeypatch):
    """Valid JSON but not an array — pydantic rejects it, naming the field."""
    monkeypatch.setenv("VERITAS_EXCLUDE", '"just-a-string"')
    with pytest.raises(ValidationError) as excinfo:
        load_settings("nope.toml")
    message = str(excinfo.value)
    assert "exclude" in message
    assert "just-a-string" in message


def test_config_hash_changes_with_review_settings():
    base = Settings()
    assert base.config_hash != Settings(exclude=["other/"]).config_hash
    assert base.config_hash != Settings(batch_chars=24000).config_hash
    assert base.config_hash != Settings(max_batches=16).config_hash


def test_review_out_of_range_values_rejected(tmp_path):
    """Bounds are enforced by the existing pydantic validation path (no new error path)."""
    low_chars = _write_review_config(tmp_path, "[review]\nbatch_chars = 999\n")
    with pytest.raises(ValidationError) as excinfo:
        load_settings(str(low_chars))
    assert "batch_chars" in str(excinfo.value)
    assert "greater than or equal to 1000" in str(excinfo.value)

    low_batches = _write_review_config(tmp_path, "[review]\nmax_batches = 0\n")
    with pytest.raises(ValidationError) as excinfo:
        load_settings(str(low_batches))
    assert "max_batches" in str(excinfo.value)
    assert "greater than or equal to 1" in str(excinfo.value)