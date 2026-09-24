"""Shared secret redaction (T035, constitution Privacy & Data Handling).

Every review node calls ``redact_secrets()`` on user-facing finding text before
it enters state, so a hardcoded secret the review is reporting on is never
echoed back verbatim in the report, stdout, logs, or error messages.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|client[_-]?secret)\s*[=:]\s*['\"]?([A-Za-z0-9_\-\.]{12,})")),
    ("bearer", re.compile(r"(?i)\b(bearer|token|auth)\s+[a-zA-Z0-9._\-]{12,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL)),
    ("connection_string", re.compile(r"(?i)(mongodb(?:\+srv)?://|postgres(?:ql)?://|mysql://|redis://|amqp://|jdbc:)[^\s'\"]+")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("url_credentials", re.compile(r"\bhttps?://[^\s/@:]+:[^\s/@:]+@")),
]

_MASK = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Mask common secret shapes in ``text``.

    Non-secret code passes through unchanged.
    """
    if not text:
        return text
    result = text
    for _kind, pattern in _PATTERNS:
        result = pattern.sub(_MASK, result)
    return result