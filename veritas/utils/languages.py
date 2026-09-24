"""Supported-language detection + skip logic (T013, FR-022).

Supported: Python, Java (primary); JavaScript, C#/.NET, Go, Rust.
Files outside the set are skipped and explicitly noted.
"""

from __future__ import annotations

from veritas.config.constants import SUPPORTED_LANGUAGES

_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
}


def language_of(path: str) -> str | None:
    """Return the supported language name for ``path`` or None if unsupported.

    TypeScript (``.ts``/``.tsx``) is treated as JavaScript-family and therefore
    supported.
    """
    import os

    return _EXTENSIONS.get(os.path.splitext(path)[1].lower())


def is_supported(path: str) -> bool:
    """True if the file's language is in the supported set."""
    lang = language_of(path)
    return lang is not None and lang in SUPPORTED_LANGUAGES


def supported_name(path: str) -> str | None:
    """Human-readable supported language name, or None if unsupported."""
    return language_of(path)