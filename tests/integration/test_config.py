"""Integration tests — configuration precedence (T006).

The `VERITAS_*` environment is cleared before every test by the autouse fixture
in `tests/conftest.py`, so a shell or CI variable cannot reach the precedence
assertions here.
"""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from veritas.config.settings import Settings, load_settings

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


def test_no_veritas_env_var_is_inherited_from_the_shell():
    """The conftest fixture is what makes the precedence tests above meaningful.

    `Settings` reads `VERITAS_*`, so a `VERITAS_MODEL` or `VERITAS_API_KEY` left
    over in the environment would change what `Settings()` returns without any
    test asserting it. Proven by running the suite with those variables set and
    watching this pass. Asserted at test start rather than against the fixture
    object, because that is the property the rest of the file relies on.
    """
    leaked = [name for name in os.environ if name.startswith("VERITAS_")]
    assert leaked == [], f"VERITAS_* leaked into the test environment: {leaked}"


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


# --- [llm] request bounds (FR-019) ---


def _write_llm_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "llm-config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_llm_bounds_defaults_apply():
    settings = Settings()
    assert settings.timeout_seconds == 120
    assert settings.max_retries == 2


def test_llm_section_overrides_defaults(tmp_path):
    path = _write_llm_config(tmp_path, "[llm]\ntimeout_seconds = 30.5\nmax_retries = 5\n")
    settings = load_settings(str(path))
    assert settings.timeout_seconds == 30.5
    assert settings.max_retries == 5


def test_env_overrides_llm_section(tmp_path, monkeypatch):
    path = _write_llm_config(tmp_path, "[llm]\ntimeout_seconds = 30\nmax_retries = 1\n")
    monkeypatch.setenv("VERITAS_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("VERITAS_MAX_RETRIES", "0")
    settings = load_settings(str(path))
    assert settings.timeout_seconds == 7.5
    # 0 is a meaningful value (one attempt, no retry), not "absent".
    assert settings.max_retries == 0


def test_llm_bounds_out_of_range_rejected(tmp_path):
    low_timeout = _write_llm_config(tmp_path, "[llm]\ntimeout_seconds = 0\n")
    with pytest.raises(ValidationError) as excinfo:
        load_settings(str(low_timeout))
    assert "timeout_seconds" in str(excinfo.value)
    assert "greater than or equal to 1" in str(excinfo.value)

    too_many_retries = _write_llm_config(tmp_path, "[llm]\nmax_retries = 11\n")
    with pytest.raises(ValidationError) as excinfo:
        load_settings(str(too_many_retries))
    assert "max_retries" in str(excinfo.value)
    assert "less than or equal to 10" in str(excinfo.value)


def test_llm_boundary_values_accepted(tmp_path):
    path = _write_llm_config(tmp_path, "[llm]\ntimeout_seconds = 1\nmax_retries = 10\n")
    settings = load_settings(str(path))
    assert settings.timeout_seconds == 1
    assert settings.max_retries == 10


def test_config_hash_changes_with_llm_bounds():
    """Both are non-secret effective config, so the hash covers them."""
    base = Settings()
    assert base.config_hash != Settings(timeout_seconds=60).config_hash
    assert base.config_hash != Settings(max_retries=7).config_hash