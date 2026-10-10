"""Integration tests — configuration precedence (T006).

The `VERITAS_*` environment is cleared before every test by the autouse fixture
in `tests/conftest.py`, so a shell or CI variable cannot reach the precedence
assertions here.
"""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import fake_secret

from veritas.config.settings import Settings, load_settings

def _write_config(tmp_path: Path) -> Path:
    """A config file passed explicitly with --config, so it may hold the key (T107)."""
    path = tmp_path / "explicit-config.toml"
    path.write_text(
        '[llm]\napi_key = "toml-key"\nmodel = "openai:toml/model"\nbase_url = "https://example.com/v1"\nzdr = true\n'
        '[hosting]\nprovider = "gitlab"\ngitlab_url = "https://gitlab.example.com"\n',
        encoding="utf-8",
    )
    return path


def test_explicit_config_file_values_loaded_including_api_key(tmp_path):
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


def test_max_concurrency_defaults_apply():
    settings = Settings()
    assert settings.max_concurrency == 4


def test_max_concurrency_override_and_env(tmp_path, monkeypatch):
    """[llm] max_concurrency reads from the file like the other [llm] keys, and
    VERITAS_MAX_CONCURRENCY beats it (FR-019)."""
    path = _write_llm_config(tmp_path, "[llm]\nmax_concurrency = 2\n")
    assert load_settings(str(path)).max_concurrency == 2

    monkeypatch.setenv("VERITAS_MAX_CONCURRENCY", "8")
    assert load_settings(str(path)).max_concurrency == 8


def test_max_concurrency_bounds_rejected(tmp_path):
    """ge=1 / le=16, enforced by the same pydantic validation path."""
    low = _write_llm_config(tmp_path, "[llm]\nmax_concurrency = 0\n")
    with pytest.raises(ValidationError) as excinfo:
        load_settings(str(low))
    assert "max_concurrency" in str(excinfo.value)
    assert "greater than or equal to 1" in str(excinfo.value)

    high = _write_llm_config(tmp_path, "[llm]\nmax_concurrency = 17\n")
    with pytest.raises(ValidationError) as excinfo:
        load_settings(str(high))
    assert "max_concurrency" in str(excinfo.value)
    assert "less than or equal to 16" in str(excinfo.value)


def test_max_concurrency_boundary_values_accepted(tmp_path):
    low = _write_llm_config(tmp_path, "[llm]\nmax_concurrency = 1\n")
    high = tmp_path / "high-concurrency.toml"
    high.write_text("[llm]\nmax_concurrency = 16\n", encoding="utf-8")
    assert load_settings(str(low)).max_concurrency == 1
    assert load_settings(str(high)).max_concurrency == 16


def test_config_hash_changes_with_max_concurrency():
    assert Settings().config_hash != Settings(max_concurrency=2).config_hash


# --- CLI configuration errors (exit 1, one [error] line, no traceback) ---


def test_cli_batch_chars_zero_prints_one_error_line_and_exits_1(tmp_path):
    """[review] batch_chars = 0 fails validation; the CLI reports the one
    problem on stderr and exits 1 without a traceback."""
    from typer.testing import CliRunner

    from veritas.cli.app import app

    path = tmp_path / "bad-config.toml"
    path.write_text("[review]\nbatch_chars = 0\n", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["review", "--scope", "project", "--target", ".", "--config", str(path)]
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "[error] invalid configuration: batch_chars: "
        "Input should be greater than or equal to 1000"
    )
    assert "Traceback" not in result.stderr


def test_cli_env_exclude_not_json_prints_one_error_line_and_exits_1(monkeypatch):
    """A malformed VERITAS_EXCLUDE surfaces as one ValueError line, same shape."""
    from typer.testing import CliRunner

    from veritas.cli.app import app

    monkeypatch.setenv("VERITAS_EXCLUDE", ".specify/")
    result = CliRunner().invoke(app, ["review", "--scope", "project", "--target", "."])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "[error] invalid configuration: VERITAS_EXCLUDE must be a JSON array of "
        'strings, for example VERITAS_EXCLUDE=\'[".specify/\"]\'; got: .specify/'
    )
    assert "Traceback" not in result.stderr


# --- [security] opengrep_rules (FR-012): a registry name or a local rules source ---


def test_opengrep_rules_default_is_owasp_top_ten():
    assert Settings().opengrep_rules == "p/owasp-top-ten"


def test_opengrep_rules_override_from_config_file(tmp_path):
    path = tmp_path / "security-config.toml"
    path.write_text('[security]\nopengrep_rules = "r/corp-pack"\n', encoding="utf-8")
    assert load_settings(str(path)).opengrep_rules == "r/corp-pack"


def test_opengrep_rules_env_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "security-config.toml"
    path.write_text('[security]\nopengrep_rules = "p/owasp-top-ten"\n', encoding="utf-8")
    monkeypatch.setenv("VERITAS_OPENGREP_RULES", "r/from-env")
    assert load_settings(str(path)).opengrep_rules == "r/from-env"


def test_opengrep_rules_local_file_and_directory_accepted(tmp_path):
    """An existing path is a valid rules source, whether file or directory."""
    rules_file = tmp_path / "rules" / "custom.yaml"
    rules_file.parent.mkdir()
    rules_file.write_text("rules: []\n", encoding="utf-8")
    rules_dir = tmp_path / "ruleset-dir"
    rules_dir.mkdir()

    config = tmp_path / "security-config.toml"
    # TOML basic strings escape backslashes, so the POSIX form of the path goes in.
    config.write_text(
        f'[security]\nopengrep_rules = "{rules_file.as_posix()}"\n', encoding="utf-8"
    )
    assert load_settings(str(config)).opengrep_rules == rules_file.as_posix()

    config.write_text(
        f'[security]\nopengrep_rules = "{rules_dir.as_posix()}"\n', encoding="utf-8"
    )
    assert load_settings(str(config)).opengrep_rules == rules_dir.as_posix()


def test_opengrep_rules_nonexistent_path_rejected():
    """Neither a registry name nor an existing path - rejected by validation."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(opengrep_rules="missing/rules.yaml")
    message = str(excinfo.value)
    assert "opengrep_rules" in message
    assert "registry ruleset" in message
    assert "missing/rules.yaml" in message


def test_cli_bad_opengrep_rules_prints_one_error_line_and_exits_1(tmp_path):
    """The same friendly invalid-configuration path as any other bad key."""
    from typer.testing import CliRunner

    from veritas.cli.app import app

    path = tmp_path / "bad-rules.toml"
    path.write_text('[security]\nopengrep_rules = "not-a-ruleset"\n', encoding="utf-8")
    result = CliRunner().invoke(
        app, ["review", "--scope", "project", "--target", ".", "--config", str(path)]
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "[error] invalid configuration: opengrep_rules: Value error, must be a "
        "registry ruleset name starting with 'p/' or 'r/', or an existing local "
        "rules file or directory; got: not-a-ruleset"
    )
    assert "Traceback" not in result.stderr

# --- secrets are refused in the committed .veritas/config.toml (T107) ---------

_COMMITTED_KEY = fake_secret("sk-", "CommittedConfigKey0123456789")
_SECRET_SECTIONS = {
    "api_key": "llm",
    "github_token": "hosting",
    "gitlab_token": "hosting",
}


def _write_dot_veritas(name: str, text: str) -> None:
    """Write .veritas/<name> in the sandboxed working directory."""
    path = Path(".veritas") / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("field", sorted(_SECRET_SECTIONS))
def test_cli_rejects_a_secret_in_the_committed_config(field):
    from typer.testing import CliRunner

    from veritas.cli.app import app

    _write_dot_veritas(
        "config.toml", f'[{_SECRET_SECTIONS[field]}]\n{field} = "{_COMMITTED_KEY}"\n'
    )

    result = CliRunner().invoke(app, ["review", "--scope", "project", "--target", "."])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        f"[error] invalid configuration: {field} must not be set in "
        ".veritas/config.toml, which is committed; put it in "
        ".veritas/config.local.toml or the environment variable "
        f"VERITAS_{field.upper()} instead"
    )
    assert _COMMITTED_KEY not in result.stderr
    assert "Traceback" not in result.stderr


def test_every_secret_in_the_committed_config_is_named_in_one_line():
    _write_dot_veritas(
        "config.toml",
        f'[llm]\napi_key = "{_COMMITTED_KEY}"\n'
        f'[hosting]\ngitlab_token = "{_COMMITTED_KEY}"\n',
    )
    with pytest.raises(ValueError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert message.startswith("api_key, gitlab_token must not be set in .veritas/config.toml")
    assert "VERITAS_API_KEY, VERITAS_GITLAB_TOKEN" in message


def test_a_secret_in_the_local_config_still_loads():
    _write_dot_veritas("config.toml", '[llm]\nmodel = "openai:committed/model"\n')
    _write_dot_veritas("config.local.toml", f'[llm]\napi_key = "{_COMMITTED_KEY}"\n')

    settings = load_settings()

    assert settings.api_key == _COMMITTED_KEY
    assert settings.model_runtime == "openai:committed/model"


def test_a_secret_in_the_environment_still_loads(monkeypatch):
    _write_dot_veritas("config.toml", '[llm]\nmodel = "openai:committed/model"\n')
    monkeypatch.setenv("VERITAS_API_KEY", _COMMITTED_KEY)

    settings = load_settings()

    assert settings.api_key == _COMMITTED_KEY
    assert settings.model_runtime == "openai:committed/model"


def test_cli_accepts_a_secret_in_the_local_config(monkeypatch):
    """The CLI gets past configuration loading: the next failure is the target."""
    from typer.testing import CliRunner

    from veritas.cli.app import app

    _write_dot_veritas("config.local.toml", f'[llm]\napi_key = "{_COMMITTED_KEY}"\n')

    result = CliRunner().invoke(
        app, ["review", "--scope", "project", "--target", "does-not-exist"]
    )

    assert result.exit_code == 1
    assert "invalid configuration" not in result.stderr
    assert "target directory does not exist" in result.stderr
