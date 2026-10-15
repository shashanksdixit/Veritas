"""Unit tests — supported language detection (T013)."""

import pytest

from veritas.utils.languages import is_supported, language_of, supported_name


@pytest.mark.parametrize(
    "path,lang",
    [
        ("src/app.py", "python"),
        ("main.pyi", "python"),
        ("pkg/Thing.java", "java"),
        ("web/app.js", "javascript"),
        ("web/app.ts", "javascript"),
        ("web/app.tsx", "javascript"),
        ("lib/core.cs", "csharp"),
        ("cmd/main.go", "go"),
        ("src/lib.rs", "rust"),
    ],
)
def test_supported_language_detected(path, lang):
    assert language_of(path) == lang
    assert is_supported(path)
    assert supported_name(path) == lang


@pytest.mark.parametrize(
    "path",
    ["legacy/app.cob", "notes.md", "assets/logo.png", "script.sh", "Makefile", "spec.md"],
)
def test_unsupported_files_skipped(path):
    assert language_of(path) is None
    assert not is_supported(path)


def test_extension_case_insensitive():
    assert language_of("src/App.PY") == "python"