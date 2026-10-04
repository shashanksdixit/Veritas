"""Test index for the test-coverage review (T081, FR-004).

Batches split a codebase into pieces, so the reviewer looking at one batch cannot
see the tests belonging to code in another batch - and reports tests as missing
that exist. The index lists every test file in scope once, with the test names
inside each Python one, so a coverage judgement does not depend on which batch
happens to be in front of the model.

Pure: this module reads the scoped file contents it is handed and nothing else.
No settings, no filesystem access, no logging, and Python sources are parsed,
never executed - a test file with import-time side effects costs nothing here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from fnmatch import fnmatchcase

# Shown to the model when the scope holds no test files at all, so a coverage gap
# is described as "not in the reviewed scope" instead of "does not exist".
NO_TEST_FILES_NOTICE = (
    "No test files were found in the reviewed scope. "
    "Tests outside the reviewed scope may exist."
)

INDEX_HEADER = (
    "Test index (test files in the reviewed scope; test names only, bodies not shown):"
)

# FR-004: the index is a fixed budget, so a large test suite cannot crowd out the
# code the reviewer was actually asked to judge.
DEFAULT_MAX_CHARS = 8000

# A path containing one of these segments is a test tree, whatever the file in it
# is called.
_TEST_DIR_SEGMENTS = frozenset({"tests", "test", "__tests__"})

# Matched against the file name only. Anchored patterns rather than a substring
# test for "test": attest.py, contest/main.py and src/testing_utils.py are
# ordinary code, not tests.
_TEST_FILE_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*Test.java",
    "*Tests.java",
    "*Test.cs",
    "*Tests.cs",
    "*.test.js",
    "*.test.ts",
    "*.test.jsx",
    "*.test.tsx",
    "*.spec.js",
    "*.spec.ts",
)


@dataclass(frozen=True)
class TestFileEntry:
    """One test file's line of the index, plus how many names it contributed."""

    path: str
    line: str
    name_count: int


def is_test_file(path: str) -> bool:
    """True when ``path`` names a test file or sits in a test tree."""
    segments = [part for part in path.replace("\\", "/").split("/") if part]
    if not segments:
        return False
    if _TEST_DIR_SEGMENTS.intersection(segments):
        return True
    name = segments[-1]
    return any(fnmatchcase(name, pattern) for pattern in _TEST_FILE_PATTERNS)


def _python_test_names(source: str) -> tuple[str, ...] | None:
    """The test names in one Python file, or None when the file does not parse.

    Only what test discovery would find: module-level functions named ``test*``,
    and methods named ``test*`` directly inside a class named ``Test*``, rendered
    ``TestClass.test_name`` in definition order. Nested functions are not visited
    and other classes are not opened, so a nested helper or an unrelated class
    cannot inflate the index.

    A file that does not parse is still listed - by path, marked - because "could
    not parse" is a fact about the file, and the path alone is useful to the
    reviewer.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                names.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith(
                    "test"
                ):
                    names.append(f"{node.name}.{child.name}")
    return tuple(names)


def _entry(path: str, source: str) -> TestFileEntry:
    """Render one test file as a single line of the index."""
    if not path.endswith(".py"):
        # Names are only extracted for Python; for every other language the path
        # is what the reviewer gets.
        return TestFileEntry(path=path, line=f"- {path}", name_count=0)
    names = _python_test_names(source)
    if names is None:
        return TestFileEntry(path=path, line=f"- {path} (could not parse)", name_count=0)
    if not names:
        return TestFileEntry(path=path, line=f"- {path}", name_count=0)
    return TestFileEntry(
        path=path, line=f"- {path}: {', '.join(names)}", name_count=len(names)
    )


def _omitted_note(omitted: int, max_chars: int) -> str:
    return f"...and {omitted} more test file(s) not listed (index limit {max_chars} characters)."


def _render(lines: list[str], note: str) -> str:
    parts = [INDEX_HEADER, *lines]
    if note:
        parts.append(note)
    return "\n".join(parts)


def build_test_index(
    files: dict[str, str], *, max_chars: int = DEFAULT_MAX_CHARS
) -> tuple[str | None, dict]:
    """Index the test files in ``files``, with stats describing what was indexed.

    ``files`` is the scoped path -> content mapping, after exclusion. Only paths
    where :func:`is_test_file` holds are considered, in sorted order, so the same
    scope produces the same index however the mapping happened to be ordered.

    Returns ``(index, stats)``. ``index`` is None when the scope has no test
    files at all, or when not even a minimal index fits ``max_chars``; ``stats``
    says what was found either way. Otherwise the index never exceeds
    ``max_chars``: whole file lines are kept in sorted order and whatever does not
    fit is reported in a trailing note rather than cut mid-line.

    ``stats`` carries ``test_files``, ``test_names``, ``chars``, ``truncated`` and
    ``omitted_files``. ``test_names`` counts the names actually extracted, so a
    file that could not be parsed contributes a file but no names.
    """
    paths = sorted(path for path in files if is_test_file(path))
    entries = [_entry(path, files[path]) for path in paths]
    stats: dict = {
        "test_files": len(entries),
        "test_names": sum(entry.name_count for entry in entries),
        "chars": 0,
        "truncated": False,
        "omitted_files": 0,
    }
    if not entries:
        return (None, stats)

    lines = [entry.line for entry in entries]
    full = _render(lines, "")
    if len(full) <= max_chars:
        return (full, {**stats, "chars": len(full)})

    # Too long. Keep the longest run of whole file lines whose own trailing note
    # still fits, so the note never claims a file was omitted when it was listed.
    kept: list[str] = []
    for take in range(1, len(lines)):
        candidate = lines[:take]
        if len(_render(candidate, _omitted_note(len(lines) - take, max_chars))) <= max_chars:
            kept = candidate

    if not kept:
        # Even the first file's line does not fit. Say the file exists and how
        # many names are in it, which is far shorter than the names themselves.
        first = entries[0]
        fallback = [f"- {first.path}: {first.name_count} test(s), names omitted for length"]
        if len(_render(fallback, _omitted_note(len(lines) - 1, max_chars))) <= max_chars:
            kept = fallback

    if not kept:
        return (None, {**stats, "truncated": True, "omitted_files": len(lines)})

    omitted = len(lines) - len(kept)
    index = _render(kept, _omitted_note(omitted, max_chars) if omitted else "")
    return (
        index,
        {**stats, "chars": len(index), "truncated": omitted > 0, "omitted_files": omitted},
    )