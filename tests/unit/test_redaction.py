"""Unit tests — secret redaction (T035)."""

import pytest

from tests.conftest import fake_secret
from veritas.utils.redaction import redact_secrets

_GHP_TOKEN = fake_secret("ghp_", "A" * 30)
_GHO_TOKEN = fake_secret("gho_", "B" * 30)
_GHU_TOKEN = fake_secret("ghu_", "C" * 30)
_GHS_TOKEN = fake_secret("ghs_", "D" * 30)
_GHR_TOKEN = fake_secret("ghr_", "E" * 30)
_GITLAB_TOKEN = fake_secret("glpat-", "x" * 20)
_OPENAI_KEY = fake_secret("sk-", "abcdefghijklmnopqrstuvwxyz123456")
_OPENROUTER_KEY = fake_secret("sk-or-v1-", "0123456789abcdef0123456789abcdef0123")
_ANTHROPIC_KEY = fake_secret("sk-ant-api03-", "AbCdEf0123456789AbCdEf0123")
_OPENAI_PROJECT_KEY = fake_secret("sk-proj-", "AbCdEf0123456789AbCdEf0123")
_GITHUB_PAT = fake_secret("github_pat_", "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123")
_AWS_ACCESS_KEY_ID = fake_secret("AKIA", "ABCDEFGHIJKLMNOP")
_AWS_SECRET_ACCESS_KEY = fake_secret("wJalrXUtnFEMI/", "K7MDENG/bPxRfiCYEXAMPLEKEY")
_GOOGLE_KEY = fake_secret("AIza", "SyA0123456789abcdefghijklmnopqrstuv")
_SLACK_BOT_TOKEN = fake_secret("xoxb-", "123456789012-abcdefABCDEF0123")
_SLACK_USER_TOKEN = fake_secret("xoxp-", "123456789012-abcdefABCDEF0123")
_PEM_BLOCK = fake_secret(
    "-----BEGIN ", "RSA PRIVATE KEY-----\\nMIIEowIBAAKCA\\n-----END RSA PRIVATE KEY-----"
)


def test_api_key_masked():
    out = redact_secrets(f'key = "{_OPENAI_KEY}"')
    assert "[REDACTED]" in out
    assert _OPENAI_KEY not in out


def test_bearer_token_masked():
    out = redact_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    assert "[REDACTED]" in out


def test_github_token_masked():
    out = redact_secrets(f"value: {_GHP_TOKEN}")
    assert _GHP_TOKEN not in out
    assert "[REDACTED]" in out


def test_gitlab_token_masked():
    out = redact_secrets(f"value: {_GITLAB_TOKEN}")
    assert _GITLAB_TOKEN not in out


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


# --- FR-013 coverage ---------------------------------------------------------
# Every shape the requirement names, in the forms it names it: as a value rule
# (a credential shaped string wherever it appears) and as an assignment rule (a
# credential-named variable, whatever its value looks like).

_FRAGMENT = 8  # a surviving run this long out of a secret is a leak


MUST_REDACT = [
    ("openai_key", f'key = "{_OPENAI_KEY}"', _OPENAI_KEY),
    (
        "openrouter_key",
        f'OPENROUTER_API_KEY = "{_OPENROUTER_KEY}"',
        _OPENROUTER_KEY,
    ),
    (
        "anthropic_key",
        f'ANTHROPIC_API_KEY = "{_ANTHROPIC_KEY}"',
        _ANTHROPIC_KEY,
    ),
    (
        "openai_key_passed_to_a_client",
        f'client = OpenAI(api_key="{_OPENAI_PROJECT_KEY}")',
        _OPENAI_PROJECT_KEY,
    ),
    ("github_ghp", f"value: {_GHP_TOKEN}", _GHP_TOKEN),
    ("github_gho", f"value: {_GHO_TOKEN}", _GHO_TOKEN),
    ("github_ghu", f"value: {_GHU_TOKEN}", _GHU_TOKEN),
    ("github_ghs", f"value: {_GHS_TOKEN}", _GHS_TOKEN),
    ("github_ghr", f"value: {_GHR_TOKEN}", _GHR_TOKEN),
    ("github_fine_grained_pat", f"repo: {_GITHUB_PAT}", _GITHUB_PAT),
    ("gitlab_token", f"value: {_GITLAB_TOKEN}", _GITLAB_TOKEN),
    ("aws_access_key_id", f"key_id = {_AWS_ACCESS_KEY_ID}", _AWS_ACCESS_KEY_ID),
    (
        "aws_secret_access_key",
        f"AWS_SECRET_ACCESS_KEY={_AWS_SECRET_ACCESS_KEY}",
        _AWS_SECRET_ACCESS_KEY,
    ),
    ("google_api_key", f'key = "{_GOOGLE_KEY}"', _GOOGLE_KEY),
    ("slack_bot_token", f'SLACK = "{_SLACK_BOT_TOKEN}"', _SLACK_BOT_TOKEN),
    ("slack_user_token", f'SLACK_USER = "{_SLACK_USER_TOKEN}"', _SLACK_USER_TOKEN),
    (
        "bearer_token",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "abcdefghijklmnopqrstuvwxyz",
    ),
    ("quoted_password", 'password = "hunter2hunter2"', "hunter2hunter2"),
    (
        "json_client_secret",
        '{"client_secret": "abcd1234efgh5678ijkl"}',
        "abcd1234efgh5678ijkl",
    ),
    ("env_style_password", 'DB_PASSWORD: "S3cr3t!passw0rd"', "S3cr3t!passw0rd"),
    (
        "connection_string",
        'uri = "postgresql://user:secret@localhost:5432/db"',
        "postgresql://user:secret@localhost:5432/db",
    ),
    (
        "url_credentials",
        "git clone https://user:hunter2@github.com/acme/repo.git",
        "user:hunter2",
    ),
    (
        "private_key_block",
        f'pem = "{_PEM_BLOCK}"',
        _PEM_BLOCK,
    ),
    ("token_named_secret", 'SECRET_TOKEN = "abc12345secret6789"', "abc12345secret6789"),
]


MUST_NOT_REDACT = [
    ("read_from_settings", "api_key = settings.api_key"),
    ("read_from_environ", 'api_key = os.environ.get("VERITAS_API_KEY")'),
    ("environment_variable_name", 'API_KEY_ENV = "VERITAS_API_KEY"'),
    ("django_secret_key", "SECRET_KEY = settings.SECRET_KEY"),
    ("nil_check", "if password is None:"),
    ("form_field", "password_field = forms.CharField()"),
    ("method_signature", "def get_api_key(self) -> str:"),
    ("bitwise_or", "x = a | b"),
    ("pip_install", "pip install scikit-learn"),
    ("package_name", "the sk-learn package"),
    ("getenv", 'OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")'),
    ("f_string", 'raise RuntimeError(f"missing api_key for {model}")'),
    ("dict_lookup", 'token = os.environ["TOKEN"]'),
    ("plain_code", "def add(a, b):\n    return a + b\nprint(add(1, 2))"),
]


@pytest.mark.parametrize(
    ("text", "secret"),
    [(_text, _secret) for _id, _text, _secret in MUST_REDACT],
    ids=[case_id for case_id, _text, _secret in MUST_REDACT],
)
def test_required_secret_is_fully_masked(text, secret):
    out = redact_secrets(text)
    assert "[REDACTED]" in out
    assert secret not in out
    fragments = [
        secret[index : index + _FRAGMENT]
        for index in range(len(secret) - _FRAGMENT + 1)
    ]
    leaked = [fragment for fragment in fragments if fragment in out]
    assert not leaked, f"fragments of the secret survived: {leaked}"


@pytest.mark.parametrize(
    "text",
    [text for _id, text in MUST_NOT_REDACT],
    ids=[case_id for case_id, _text in MUST_NOT_REDACT],
)
def test_non_secret_text_is_untouched(text):
    assert redact_secrets(text) == text