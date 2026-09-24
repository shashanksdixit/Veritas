"""Unit tests — secret redaction (T035)."""

from veritas.utils.redaction import redact_secrets


def test_api_key_masked():
    out = redact_secrets('key = "sk-abcdefghijklmnopqrstuvwxyz123456"')
    assert "[REDACTED]" in out
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out


def test_bearer_token_masked():
    out = redact_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    assert "[REDACTED]" in out


def test_github_token_masked():
    out = redact_secrets("value: ghp_" + "A" * 30)
    assert "ghp_" + "A" * 30 not in out
    assert "[REDACTED]" in out


def test_gitlab_token_masked():
    out = redact_secrets("value: glpat-" + "x" * 20)
    assert "glpat-" + "x" * 20 not in out


def test_connection_string_masked():
    out = redact_secrets('uri = "postgresql://user:secret@localhost:5432/db"')
    assert "[REDACTED]" in out
    assert "postgresql://user:secret" not in out


def test_plain_code_passes_through():
    code = 'def add(a, b):\n    return a + b\nprint(add(1, 2))'
    assert redact_secrets(code) == code


def test_empty_string():
    assert redact_secrets("") == ""
    assert redact_secrets(None) is None