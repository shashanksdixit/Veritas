"""Path matching helpers (T075, FR-029).

Exclusion-pattern matching for review coverage. Pure functions only — no
settings, no filesystem access — so the rules are testable in isolation and
usable by every scope (project/module/file/PR).
"""

from __future__ import annotations

import fnmatch


def normalize_rel(path: str) -> str:
    """Normalize a path to forward slashes with no leading ``./``."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def matching_exclusion(path: str, patterns: list[str]) -> str | None:
    """Return the FIRST pattern in ``patterns`` that excludes ``path``, else None.

    FR-029 exclusion rules:
      * a pattern ending in "/" excludes every path under that directory prefix
        (e.g. ``.specify/`` excludes ``.specify/scripts/a.py``);
      * any other pattern is matched against the full relative path with
        ``fnmatch.fnmatchcase`` — case-sensitive on every OS, so ``*.PY`` does
        not match ``a.py``, and "*" also matches "/", so ``tests/*`` covers
        ``tests/unit/x.py`` as well as ``tests/x.py``.

    ``path`` is normalized first (forward slashes, no leading ``./``), so
    ``./.specify/x.py`` and ``.specify/x.py`` behave identically. Patterns are
    tested in list order and the first match wins, which makes the reported
    pattern deterministic. An empty list — the documented way to disable
    exclusion — always returns None.
    """
    normalized = normalize_rel(path)
    for pattern in patterns:
        if pattern.endswith("/"):
            if normalized.startswith(pattern):
                return pattern
        elif fnmatch.fnmatchcase(normalized, pattern):
            return pattern
    return None
