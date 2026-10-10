"""Shared secret redaction (T035, constitution Privacy & Data Handling).

Every review node calls ``redact_secrets()`` on user-facing finding text before
it enters state, so a hardcoded secret the review is reporting on is never
echoed back verbatim in the report, stdout, logs, or error messages.

FR-013 fixes what the mask has to cover, so three complementary rules run over
every string:

* the *.env* rule masks any unquoted value in a ``KEY=value`` line whose key is
  upper-snake and credential-named (``DB_PASSWORD=...``), whatever it looks
  like, because a .env file holds only literals;
* the *assignment* rule masks a literal value of a credential-named key
  (``api_key``, ``password``, ``client_secret``, ...) in code, YAML or prose,
  quoted or unquoted, and masks only the value so the reader still sees what
  was protected;
* the *value* rules mask anything shaped like a credential wherever it appears,
  which catches a secret assigned to a name no rule recognises.

None of them touches an environment-variable name, code that reads a secret, or
prose about either; those strings are pinned by the MUST-NOT cases in
``tests/unit/test_redaction.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_MASK = "[REDACTED]"

_KEY_NAME = (
    r"(?:[A-Za-z0-9_]*[_-])?"
    r"(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|auth[_-]?token|password|passwd|pwd|token|secret|"
    r"credentials?|auth|key)"
)
_CREDENTIAL_KEY = re.compile(rf"(?i){_KEY_NAME}")

# `API_KEY = "VERITAS_API_KEY"` assigns an environment-variable *name*, not a
# secret: all caps, underscore-separated, at least two segments.
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


def _mask_group(match: re.Match[str], group: str) -> str:
    """``match`` with only ``group`` replaced by the mask."""
    start, end = match.span(group)
    offset = match.start()
    return (
        f"{match.group(0)[: start - offset]}{_MASK}"
        f"{match.group(0)[end - offset:]}"
    )


# ---------------------------------------------------------------------------
# .env rule: KEY=value at the start of a line, no spaces around "="
# ---------------------------------------------------------------------------

# Every unquoted value is a literal here, plain words included; only a
# `$VAR` / `${VAR}` reference is left alone. Quoted values are the assignment
# rule's job.
_DOTENV = re.compile(
    r"(?m)^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>[^\s\"'$]\S*)"
)


def _mask_dotenv(match: re.Match[str]) -> str:
    if not _CREDENTIAL_KEY.fullmatch(match.group("key")):
        return match.group(0)
    return _mask_group(match, "value")


# ---------------------------------------------------------------------------
# Assignment rule: name = value | {"name": "value"} | name: value
# ---------------------------------------------------------------------------

_QUOTED_VALUE = r'"[^"\r\n]{8,}"|\'[^\'\r\n]{8,}\''

# An unquoted candidate is one token of at least 4 characters: it does not
# start with a quote, `$` or an opening bracket, holds no whitespace, quotes,
# brackets, `,` or `;`, and is followed by whitespace, `,`, `;`, a closing quote
# or the end of the text - so `f(password=pw)` (followed by `)`) is not a
# candidate, while `"api_key: abcd1234"` inside a JSON string is.
_UNQUOTED_VALUE = (
    r"[^\s\"'`$(){}\[\]<,;][^\s\"'`(){}\[\],;]{3,}(?=[\s,;\"']|$)"
)

# The separator is a lone `=` or `:`, so `==`, `!=`, `<=`, `:=`, `::` and `=>`
# are comparisons and operators, not assignments.
_ASSIGNMENT = re.compile(
    rf"(?i)\b{_KEY_NAME}[\"']?\s*(?<![=!<>:])[=:](?![=:>])\s*"
    rf"(?P<value>{_QUOTED_VALUE}|{_UNQUOTED_VALUE})"
)

# Unquoted candidates that are code, not literals (FR-013).
_PLAIN_WORD = re.compile(r"[A-Za-z_]+")  # None, true, str, password, CharField
_NUMBER = re.compile(r"[+-]?\d+(?:[._]\d+)*")  # 42, 3600, -1
_DOTTED_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")  # settings.api_key


def _is_expression(value: str) -> bool:
    if value[0] in "\"'":
        return bool(_ENV_NAME.fullmatch(value[1:-1]))
    return any(
        pattern.fullmatch(value)
        for pattern in (_PLAIN_WORD, _NUMBER, _DOTTED_NAME, _ENV_NAME)
    )


def _mask_assignment(match: re.Match[str]) -> str:
    if _is_expression(match.group("value")):
        return match.group(0)
    return _mask_group(match, "value")


# ---------------------------------------------------------------------------
# Value rules: a credential shape, wherever it appears.
# ---------------------------------------------------------------------------


def _mask_aws_secret(match: re.Match[str]) -> str:
    """Mask a 40-character AWS secret access key, and nothing else that long.

    Requiring a ``/`` or ``+`` — an AWS secret key is base64 and the example in
    the spec carries both — keeps plain 40-character digests and identifiers
    untouched.
    """
    value = match.group(0)
    return _MASK if "/" in value or "+" in value else value


_Replacement = str | Callable[[re.Match[str]], str]

_VALUE_RULES: list[tuple[str, re.Pattern[str], _Replacement]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        _MASK,
    ),
    (
        "pem",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL
        ),
        _MASK,
    ),
    (
        "connection_string",
        re.compile(
            r"(?i)(mongodb(?:\+srv)?://|postgres(?:ql)?://|mysql://|redis://"
            r"|amqp://|jdbc:)[^\s'\"]+"
        ),
        _MASK,
    ),
    ("url_credentials", re.compile(r"\bhttps?://[^\s/@:]+:[^\s/@:]+@"), _MASK),
    (
        "bearer",
        re.compile(r"(?i)\b(?:bearer|token|auth)\s+[A-Za-z0-9._\-]{12,}"),
        _MASK,
    ),
    (
        "github_token",
        re.compile(r"(?<![A-Za-z0-9])gh[opurs]_[A-Za-z0-9]{20,}"),
        _MASK,
    ),
    (
        "github_pat",
        re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
        _MASK,
    ),
    ("gitlab_token", re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{20,}"), _MASK),
    # OpenAI, OpenRouter, Anthropic and every other vendor that ships `sk-`
    # keys. 16 characters after the prefix, so the 5-character `sk-learn` is
    # not one of them.
    ("provider_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}"), _MASK),
    (
        "aws_access_key",
        re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])"),
        _MASK,
    ),
    (
        "aws_secret_key",
        re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"),
        _mask_aws_secret,
    ),
    (
        "google_key",
        re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])"),
        _MASK,
    ),
    (
        "slack_token",
        re.compile(r"(?<![A-Za-z0-9])xox[bpras]-[A-Za-z0-9-]{10,}"),
        _MASK,
    ),
]


def redact_secrets(text: str) -> str:
    """Mask common secret shapes in ``text``.

    Non-secret code passes through unchanged.
    """
    if not text:
        return text
    result = _DOTENV.sub(_mask_dotenv, text)
    result = _ASSIGNMENT.sub(_mask_assignment, result)
    for _kind, pattern, replacement in _VALUE_RULES:
        result = pattern.sub(replacement, result)
    return result


def redact_json_strings(value: object) -> object:
    """``value`` with every string value inside it passed through ``redact_secrets()``.

    Walks the dicts and lists of a JSON-shaped value (``model_dump(mode="json")``)
    so a finished JSON output is redacted field by field (FR-013). Keys, numbers,
    booleans and None are returned unchanged.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redact_json_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_json_strings(item) for item in value]
    return value
