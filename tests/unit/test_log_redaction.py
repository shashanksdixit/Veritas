"""Unit tests — every log line and CLI error message is redacted (T104, FR-013)."""

import io

from typer.testing import CliRunner

import veritas.cli.app as cli_app
from tests.conftest import fake_secret
from veritas.review import ReviewFatalError
from veritas.utils.logging import Log

_GITHUB_TOKEN = fake_secret("ghp_", "F" * 30)
_PASSWORD = fake_secret("hunter", "2")
_API_KEY = fake_secret("abcd1234", "efgh5678")


def test_log_line_with_a_known_secret_shape_is_masked():
    stream = io.StringIO()
    Log(stream=stream).error(f"GitHub API 401 using {_GITHUB_TOKEN}")
    out = stream.getvalue()
    assert _GITHUB_TOKEN not in out
    assert out == "[error] GitHub API 401 using [REDACTED]\n"


def test_log_line_with_an_unquoted_credential_value_is_masked():
    stream = io.StringIO()
    Log(stream=stream).warn(f"connect failed: password={_PASSWORD} host=db1")
    assert stream.getvalue() == "[warn] connect failed: password=[REDACTED] host=db1\n"


def test_verbose_structured_payload_is_masked():
    stream = io.StringIO()
    Log(stream=stream, verbose=True).info(
        "posting comment", detail=f"api_key: {_API_KEY}", url_token=_GITHUB_TOKEN
    )
    out = stream.getvalue()
    assert _API_KEY not in out
    assert _GITHUB_TOKEN not in out
    assert out.startswith("[info] posting comment {")
    assert "[REDACTED]" in out


def test_llm_call_error_is_masked():
    stream = io.StringIO()
    Log(stream=stream).llm_call(
        "openrouter/some-model", 12.0, error=f"401 bad key {_GITHUB_TOKEN}"
    )
    out = stream.getvalue()
    assert _GITHUB_TOKEN not in out
    assert out.startswith("[error] LLM call openrouter/some-model (12 ms) failed: 401 bad key [REDACTED]")


def test_log_without_secrets_is_unchanged():
    stream = io.StringIO()
    Log(stream=stream).info("reviewed 12 files; api_key = settings.api_key")
    assert stream.getvalue() == "[info] reviewed 12 files; api_key = settings.api_key\n"


def test_cli_fatal_error_is_masked_on_stderr(monkeypatch):
    def fail_review(*_args, **_kwargs):
        raise ReviewFatalError(f"GitHub API 401: Bad credentials for {_GITHUB_TOKEN}")

    monkeypatch.setattr(cli_app, "load_settings", lambda _config: object())
    monkeypatch.setattr(cli_app, "run_review", fail_review)

    result = CliRunner().invoke(
        cli_app.app, ["review", "--scope", "pr", "--target", "acme/repo#1"]
    )

    assert result.exit_code == 1
    assert _GITHUB_TOKEN not in result.stderr
    assert "[error] GitHub API 401: Bad credentials for [REDACTED]" in result.stderr


def test_cli_configuration_error_is_masked_on_stderr(monkeypatch):
    def bad_config(_config):
        raise ValueError(f"invalid config file local.toml: api_key: {_API_KEY}")

    monkeypatch.setattr(cli_app, "load_settings", bad_config)

    result = CliRunner().invoke(
        cli_app.app, ["review", "--scope", "project", "--target", "."]
    )

    assert result.exit_code == 1
    assert _API_KEY not in result.stderr
    assert (
        "[error] invalid configuration: invalid config file local.toml: api_key: [REDACTED]"
        in result.stderr
    )


def test_cli_tracebacks_do_not_show_local_variables():
    assert cli_app.app.pretty_exceptions_show_locals is False
