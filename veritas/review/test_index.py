"""Relevance-ordered test index for the test-coverage review (T082, FR-004).

Batches split a codebase into pieces, so the reviewer looking at one batch cannot
see the tests belonging to code in another batch - and reports tests as missing
that exist. Each batch therefore carries its own index of the test files in
scope, with the test names inside each Python one (FR-004).

One index per batch rather than one per scope, because relevance is a property of
a batch: the tests that matter for ``src/batching.py`` are not the tests that
matter for ``src/settings.py``. Each index is ranked name-match first, then
Python import match, then everything else, so when the budget runs out it is the
tests of the code in *this* batch that survive rather than an alphabetical prefix
of the whole suite.

Pure: this module reads the scoped file contents it is handed and nothing else.
No settings, no filesystem access, no logging, and Python sources are parsed,
never executed - a test file with import-time side effects costs nothing here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # a type only: importing the planner at runtime would drag in the LLM layer
    from veritas.review.batching import BatchPlan

# Shown to the model when the scope holds no test files at all, so a coverage gap
# is described as "not in the reviewed scope" instead of "does not exist".
NO_TEST_FILES_NOTICE = (
    "No test files were found in the reviewed scope. "
    "Tests outside the reviewed scope may exist."
)

INDEX_HEADER = (
    "Test index (test files in the reviewed scope; test names only, bodies not shown):"
)

# FR-004: a per-batch budget. Large enough to hold a real suite's names, small
# enough that the index cannot crowd out the code the reviewer must judge.
DEFAULT_MAX_CHARS = 16000

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

# Markers stripped from a stem to recover the name of the thing under test. The
# dot markers must be tried before the bare "test" suffix, or "batching.test"
# loses its dot along with the marker. A marker is only stripped when something
# is left, so a file called "test.py" keeps the name it has.
_STEM_PREFIXES = ("test_",)
_STEM_SUFFIXES = (".test", ".spec", "_test", "_tests", "tests", "test")


@dataclass(frozen=True)
class TestFileEntry:
    """One test file's index line, plus the keys a batch is matched against.

    ``subject`` is the name of the thing under test (see :func:`subject_stem`) and
    ``module`` the dotted path a Python test would import, or None for a file that
    is not Python. Both are computed once per file, not once per batch.
    """

    path: str
    line: str
    name_count: int
    subject: str
    module: str | None


def _posix(path: str) -> PurePosixPath:
    return PurePosixPath(path.replace("\\", "/"))


def is_test_file(path: str) -> bool:
    """True when ``path`` names a test file or sits in a test tree."""
    segments = [part for part in path.replace("\\", "/").split("/") if part]
    if not segments:
        return False
    if _TEST_DIR_SEGMENTS.intersection(segments):
        return True
    name = segments[-1]
    return any(fnmatchcase(name, pattern) for pattern in _TEST_FILE_PATTERNS)


def subject_stem(path: str) -> str:
    """The lower-cased stem of ``path`` with its test markers removed.

    This is the name of the thing under test, so ``tests/unit/test_batching.py``,
    ``batching_test.go``, ``BatchingTest.java``, ``BatchingTests.cs``,
    ``batching.test.ts`` and ``batching.spec.js`` all give ``batching``, as does
    the source file ``batching.py`` that they test.
    """
    stem = _posix(path).stem.lower()
    for marker in _STEM_PREFIXES:
        if stem.startswith(marker) and len(stem) > len(marker):
            stem = stem[len(marker) :]
            break
    for marker in _STEM_SUFFIXES:
        if stem.endswith(marker) and len(stem) > len(marker):
            stem = stem[: -len(marker)]
            break
    return stem


def module_path(path: str) -> str | None:
    """The dotted module path of a Python file, or None for any other file.

    ``veritas/config/settings.py`` is ``veritas.config.settings``, which is how a
    test file spells the module it imports. A package's ``__init__.py`` is the
    package itself, so ``veritas/review/__init__.py`` is ``veritas.review``, and a
    root-level ``__init__.py`` has no module path at all.
    """
    pure = _posix(path)
    if pure.suffix != ".py":
        return None
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


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
    """Render one test file as a single index line, with its match keys."""
    module = module_path(path)
    subject = subject_stem(path)
    if module is None:
        # Names are only extracted for Python; for every other language the path is
        # what the reviewer gets, and only the subject name can be matched.
        return TestFileEntry(
            path=path, line=f"- {path}", name_count=0, subject=subject, module=None
        )
    names = _python_test_names(source)
    if names is None:
        line, name_count = f"- {path} (could not parse)", 0
    elif not names:
        line, name_count = f"- {path}", 0
    else:
        line, name_count = f"- {path}: {', '.join(names)}", len(names)
    return TestFileEntry(
        path=path, line=line, name_count=name_count, subject=subject, module=module
    )


def _omitted_note(omitted: int, max_chars: int) -> str:
    return f"...and {omitted} more test file(s) not listed (index limit {max_chars} characters)."


def _render(lines: list[str], note: str) -> str:
    parts = [INDEX_HEADER, *lines]
    if note:
        parts.append(note)
    return "\n".join(parts)


def _fit(
    entries: list[TestFileEntry], max_chars: int
) -> tuple[str | None, bool]:
    """Fit the most relevant entries into ``max_chars``, whole files at a time.

    ``entries`` arrives in ranked order, so what is dropped is the tail: the least
    relevant tests. No file is ever cut in half, and the note only ever counts
    files that really are absent - a run that keeps every file never claims one was
    omitted. When not even a minimal rendering fits, the index is None and the
    caller counts the batch as truncated.
    """
    lines = [entry.line for entry in entries]
    full = _render(lines, "")
    if len(full) <= max_chars:
        return (full, False)

    kept: list[str] = []
    for take in range(1, len(entries)):
        candidate = lines[:take]
        if len(_render(candidate, _omitted_note(len(entries) - take, max_chars))) <= max_chars:
            kept = candidate

    if not kept:
        # Even the first file's line does not fit. Say the file exists and how many
        # names are in it, which is far shorter than the names themselves.
        first = entries[0]
        kept = [f"- {first.path}: {first.name_count} test(s), names omitted for length"]
        note = _omitted_note(len(entries) - 1, max_chars)
        if len(_render(kept, note)) > max_chars:
            return (None, True)
        return (_render(kept, note), True)

    return (_render(kept, _omitted_note(len(entries) - len(kept), max_chars)), True)


def build_batch_test_indexes(
    files: dict[str, str],
    plan: "BatchPlan | None",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[dict[int, str] | None, dict]:
    """One relevance-ordered test index per batch, with stats over the whole scope.

    ``files`` is the scoped path -> content mapping after exclusion, and ``plan``
    the batch plan built from it. Every test file is classified and parsed exactly
    once; the per-batch work is ranking, so the cost does not grow with the number
    of batches.

    Ranking is by relevance to that batch: first the test files whose subject name
    matches a non-test source file in the batch, then the Python test files whose
    text contains the dotted module path of a Python source file in the batch, then
    every other test file - sorted by path within each group. Only non-test source
    files are subjects: a test file is never evidence that another test file
    matters, or every file in a test-heavy batch would promote every other.

    When an index does not fit ``max_chars`` it is cut at a file boundary from the
    least relevant end and says how many files were dropped, so a truncation costs
    the reviewer the tests that matter least for the code in front of it.

    Returns ``(indexes, stats)`` keyed by ``batch.index``, which is 1-based.
    ``indexes`` is None when the scope holds no test files at all; ``stats`` says
    what was found either way and carries ``test_files``, ``test_names``,
    ``batches``, ``truncated_batches`` and ``max_chars``.
    """
    entries = [
        _entry(path, files[path]) for path in sorted(path for path in files if is_test_file(path))
    ]
    batches = list(plan.batches) if plan is not None else []
    stats: dict = {
        "test_files": len(entries),
        "test_names": sum(entry.name_count for entry in entries),
        "batches": len(batches),
        "truncated_batches": 0,
        "max_chars": max_chars,
    }
    if not entries:
        return (None, stats)

    indexes: dict[int, str] = {}
    for batch in batches:
        # Sorted for the same reason the entries are: a batch's own file order
        # must not leak into the ranking.
        sources = sorted({chunk.path for chunk in batch.chunks if not is_test_file(chunk.path)})
        subjects = {subject_stem(path) for path in sources}
        modules = {module for module in (module_path(path) for path in sources) if module}
        ranked: list[TestFileEntry] = []
        tier2: list[TestFileEntry] = []
        rest: list[TestFileEntry] = []
        for entry in entries:
            if entry.subject in subjects:
                ranked.append(entry)
            elif entry.module and any(module in files[entry.path] for module in modules):
                tier2.append(entry)
            else:
                rest.append(entry)
        # Three buckets, concatenated: an import match is weaker than a name
        # match, so it must not overtake one merely by sorting earlier.
        ranked.extend(tier2)
        ranked.extend(rest)
        index, truncated = _fit(ranked, max_chars)
        if index is not None:
            indexes[batch.index] = index
        if truncated:
            stats["truncated_batches"] += 1
    return (indexes, stats)