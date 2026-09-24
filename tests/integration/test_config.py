"""Integration tests — configuration precedence (T006)."""

from pathlib import Path

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