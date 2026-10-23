"""Unit tests — exclusion-pattern matching (T075, FR-029).

``matching_exclusion`` is pure, so every rule is asserted directly. Scope
integration (exclusion before fetch, SAST input, file-scope exemption) lives in
``tests/integration/test_review_exclusion.py``.
"""

import pytest

from veritas.utils.paths import matching_exclusion, normalize_rel


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        (".specify/scripts/a.py", ".specify/"),
        (".specify/x.py", ".specify/"),
        ("./.specify/x.py", ".specify/"),
        (".specify/", ".specify/"),
    ],
)
def test_directory_prefix_pattern_matches(path, pattern):
    assert matching_exclusion(path, [pattern]) == pattern


@pytest.mark.parametrize(
    "path",
    ["my.specify/a.py", ".specifyx/a.py", "src/specify/a.py", "deep/nested/.specify/x.py"],
)
def test_directory_prefix_pattern_requires_leading_prefix(path):
    """A "/" pattern is a leading directory-prefix test, not a substring one."""
    assert matching_exclusion(path, [".specify/"]) is None


def test_glob_pattern_matches_relative_path():
    assert matching_exclusion("web/app.min.js", ["*.min.js"]) == "*.min.js"


def test_glob_star_also_matches_slash():
    """fnmatch "*" spans "/", so one pattern covers nested paths."""
    assert matching_exclusion("tests/unit/x.py", ["tests/*"]) == "tests/*"
    assert matching_exclusion("tests/x.py", ["tests/*"]) == "tests/*"


def test_matching_is_case_sensitive():
    assert matching_exclusion("a.py", ["*.PY"]) is None
    assert matching_exclusion("a.PY", ["*.PY"]) == "*.PY"


def test_first_matching_pattern_in_list_order_wins():
    patterns = ["vendor/", "*.min.js", "web/"]
    assert matching_exclusion("web/app.min.js", patterns) == "*.min.js"
    assert matching_exclusion("web/app.js", patterns) == "web/"
    assert matching_exclusion("vendor/lib.js", patterns) == "vendor/"


def test_empty_pattern_list_excludes_nothing():
    assert matching_exclusion(".specify/x.py", []) is None
    assert matching_exclusion("any/file.py", []) is None


def test_backslash_paths_normalized():
    assert matching_exclusion(r".specify\scripts\a.py", [".specify/"]) == ".specify/"


def test_normalize_rel_strips_leading_dot_slash_and_backslashes():
    assert normalize_rel("./a/b.py") == "a/b.py"
    assert normalize_rel("././a/b.py") == "a/b.py"
    assert normalize_rel(r"a\b.py") == "a/b.py"
    assert normalize_rel("a/b.py") == "a/b.py"
