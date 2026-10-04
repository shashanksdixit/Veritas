"""Unit tests — deterministic batch planner (T076/T083, FR-029).

``plan_batches`` is pure, so the algorithm, its invariants and its rendering are
asserted directly. Scope wiring (only supported source files are batched) is in
``tests/integration/test_review_exclusion.py``.

Application code is batched before test files (T083), so a scope whose test paths
sort before its source paths still spends its batches on the code first and drops
tests at the cap.
"""

import subprocess
import sys

import pytest

from veritas.review import batching as batching_module
from veritas.review.batching import plan_batches

_SMALL = {
    "src/a.py": "x = 1\n",
    "src/b.py": "y = 2\n",
    "src/c.py": "z = 3\n",
}


def _lines(n: int, *, width: int = 10) -> str:
    return "".join(f"line_{i:0{width}d}\n" for i in range(1, n + 1))


def _paths_in(plan) -> set[str]:
    return {chunk.path for batch in plan.batches for chunk in batch.chunks}


def _assert_invariants(plan, files, batch_chars):
    for batch in plan.batches:
        assert len(batch.text) <= batch_chars, f"batch {batch.index} exceeds budget"
    assert sorted(plan.reviewed_files + plan.not_reviewed_files) == sorted(files)
    assert not set(plan.reviewed_files) & set(plan.not_reviewed_files)
    assert set(plan.split_files) <= set(plan.reviewed_files)
    assert list(plan.reviewed_files) == sorted(plan.reviewed_files)
    assert list(plan.split_files) == sorted(plan.split_files)
    assert list(plan.not_reviewed_files) == sorted(plan.not_reviewed_files)
    assert [b.index for b in plan.batches] == list(range(1, len(plan.batches) + 1))


def _mixed_scope() -> dict[str, str]:
    """A repo-shaped scope: tests sort before the source they cover (``t`` < ``v``).

    Six whole-file blocks of 726, 956, 841, 496, 496 and 496 chars: with
    ``batch_chars=600`` each takes a batch of its own (two never fit together), so
    the batch each file lands in is exactly its place in the ordering.
    """
    files = {
        "veritas/config/settings.py": _lines(30),
        "veritas/review/batching.py": _lines(40),
        "veritas/review/graph.py": _lines(35),
    }
    files.update({f"tests/unit/test_{name}.py": _lines(20) for name in ("app", "batch", "config")})
    return files


def _sorted_order_plan(files, **kwargs) -> object:
    """The plan the old pure-sorted ordering produced, for comparison in a test."""
    original = batching_module._batch_order
    batching_module._batch_order = sorted
    try:
        return plan_batches(files, **kwargs)
    finally:
        batching_module._batch_order = original


def _batch_of(plan, path: str) -> int:
    for batch in plan.batches:
        if any(chunk.path == path for chunk in batch.chunks):
            return batch.index
    raise AssertionError(f"{path} is in no batch")


def _is_test(files: dict[str, str], path: str) -> bool:
    """The planner's own answer, asked the way it asks it."""
    from veritas.review.test_index import is_test_file

    return is_test_file(path, files[path])


def test_small_files_pack_into_one_batch():
    plan, warnings = plan_batches(_SMALL, batch_chars=4000, max_batches=8)
    assert warnings == []
    assert len(plan.batches) == 1
    assert plan.reviewed_files == ("src/a.py", "src/b.py", "src/c.py")
    assert plan.split_files == ()
    assert plan.not_reviewed_files == ()
    assert plan.batches[0].text.count("### FILE:") == 3


def test_plan_is_independent_of_insertion_order():
    forward, _ = plan_batches(_SMALL, batch_chars=4000, max_batches=8)
    backward, _ = plan_batches(dict(reversed(list(_SMALL.items()))), batch_chars=4000, max_batches=8)
    assert forward == backward


@pytest.mark.parametrize("batch_chars", [1000, 1500, 4000, 48000])
def test_invariants_hold_across_budgets(batch_chars):
    files = {
        "src/small.py": _lines(3),
        "src/empty.py": "",
        "src/medium.py": _lines(60),
        "src/large.py": _lines(400),
        "src/huge.py": _lines(1500),
        "web/app.js": _lines(25),
    }
    plan, _ = plan_batches(files, batch_chars=batch_chars, max_batches=8)
    _assert_invariants(plan, files, batch_chars)


def test_large_file_is_split_with_correct_ranges():
    files = {"src/large.py": _lines(200)}
    plan, warnings = plan_batches(files, batch_chars=1000, max_batches=8)
    assert warnings == []
    assert plan.split_files == ("src/large.py",)
    assert plan.not_reviewed_files == ()
    assert len(plan.batches) > 1

    chunks = [chunk for batch in plan.batches for chunk in batch.chunks]
    assert len(chunks) == len(plan.batches)  # one chunk per batch
    # contiguous, in order, covering every line exactly once
    assert chunks[0].start_line == 1
    for previous, current in zip(chunks, chunks[1:]):
        assert current.start_line == previous.end_line + 1
    assert chunks[-1].end_line == 200
    assert all(c.total_lines == 200 for c in chunks)

    # range headers name the original line numbers
    for batch, chunk in zip(plan.batches, chunks):
        expected = f"### FILE: src/large.py (lines {chunk.start_line}-{chunk.end_line} of 200)"
        assert batch.text.startswith(expected)


def test_second_chunk_keeps_original_line_numbers():
    files = {"src/large.py": _lines(120)}
    plan, _ = plan_batches(files, batch_chars=1000, max_batches=8)
    second = plan.batches[1]
    first_numbered = second.text.splitlines()[1]
    first_chunk = plan.batches[0].chunks[0]
    assert first_numbered == f"{first_chunk.end_line + 1:>5}| {f'line_{first_chunk.end_line + 1:010d}'}"
    # and it is contiguous with the first chunk
    assert plan.batches[0].chunks[0].end_line + 1 == plan.batches[1].chunks[0].start_line


def test_final_chunk_batch_receives_following_small_file():
    lines = 200
    files = {"src/aaa_large.py": _lines(lines), "src/zzz_small.py": "tiny = 1\n"}
    plan, _ = plan_batches(files, batch_chars=1000, max_batches=8)
    last = plan.batches[-1]
    assert [c.path for c in last.chunks] == ["src/aaa_large.py", "src/zzz_small.py"]
    assert plan.reviewed_files == ("src/aaa_large.py", "src/zzz_small.py")
    assert all(len(b.text) <= 1000 for b in plan.batches)


def test_whole_file_dropped_when_chunks_exceed_available_batches():
    files = {"src/large.py": _lines(2000)}
    plan, warnings = plan_batches(files, batch_chars=1000, max_batches=2)
    assert warnings == []
    assert plan.batches == ()
    assert plan.not_reviewed_files == ("src/large.py",)
    assert plan.reviewed_files == ()
    assert plan.split_files == ()


def test_no_chunk_of_a_dropped_file_appears_in_any_batch():
    files = {"src/large.py": _lines(2000), "src/small.py": "x = 1\n"}
    plan, _ = plan_batches(files, batch_chars=1000, max_batches=2)
    assert "src/large.py" not in _paths_in(plan)
    assert plan.not_reviewed_files == ("src/large.py",)
    assert plan.reviewed_files == ("src/small.py",)


def test_cap_drops_later_files_but_still_tries_them():
    # 5 files of 20 lines each (a 21-char rendered block per file, so a batch
    # holds exactly one at batch_chars=600): only the first 3 fit the cap.
    files = {f"src/f{index}.py": _lines(20) for index in range(5)}
    plan, _ = plan_batches(files, batch_chars=600, max_batches=3)
    assert len(plan.batches) == 3
    assert plan.reviewed_files == ("src/f0.py", "src/f1.py", "src/f2.py")
    assert plan.not_reviewed_files == ("src/f3.py", "src/f4.py")
    _assert_invariants(plan, files, 600)


def test_small_file_still_reviewed_after_cap_when_it_fits_last_batch():
    # a_first and b_second each take a batch (711 chars, budget 800); c_tiny is
    # small enough to ride along in batch 2 under rule d.
    files = {
        "src/a_first.py": _lines(30),
        "src/b_second.py": _lines(30),
        "src/c_tiny.py": "x = 1\n",
    }
    plan, _ = plan_batches(files, batch_chars=800, max_batches=2)
    assert len(plan.batches) == 2
    assert [c.path for c in plan.batches[-1].chunks] == ["src/b_second.py", "src/c_tiny.py"]
    assert plan.reviewed_files == ("src/a_first.py", "src/b_second.py", "src/c_tiny.py")
    assert plan.not_reviewed_files == ()
    assert all(len(b.text) <= 800 for b in plan.batches)


def test_single_line_longer_than_batch_chars_is_not_reviewed():
    files = {"src/huge_line.py": "x" * 5000 + "\n" + "ok = 1\n"}
    plan, warnings = plan_batches(files, batch_chars=1000, max_batches=8)
    assert warnings == [
        "batching: src/huge_line.py not reviewed: a single line exceeds batch_chars (1000)"
    ]
    assert plan.not_reviewed_files == ("src/huge_line.py",)
    assert plan.batches == ()
    _assert_invariants(plan, files, 1000)


def test_empty_file_is_reviewed_with_header_only():
    files = {"src/empty.py": "", "src/a.py": "x = 1\n"}
    plan, warnings = plan_batches(files, batch_chars=1000, max_batches=8)
    assert warnings == []
    assert len(plan.batches) == 1
    chunk = next(c for c in plan.batches[0].chunks if c.path == "src/empty.py")
    assert (chunk.start_line, chunk.end_line, chunk.total_lines) == (1, 0, 0)
    assert "### FILE: src/empty.py" in plan.batches[0].text
    assert "src/empty.py" in plan.reviewed_files
    _assert_invariants(plan, files, 1000)


def test_zero_files_yields_zero_batches():
    plan, warnings = plan_batches({}, batch_chars=1000, max_batches=8)
    assert plan.batches == ()
    assert plan.reviewed_files == ()
    assert plan.split_files == ()
    assert plan.not_reviewed_files == ()
    assert warnings == []


def test_batches_use_blank_line_separator():
    plan, _ = plan_batches(_SMALL, batch_chars=4000, max_batches=8)
    text = plan.batches[0].text
    assert "\n\n### FILE: src/b.py" in text
    assert not text.endswith("\n")


def test_header_alone_over_budget_is_not_reviewed():
    """An empty file whose header overflows cannot be split, so it is dropped."""
    long_name = f"src/{'x' * 200}.py"
    plan, warnings = plan_batches({long_name: ""}, batch_chars=100, max_batches=8)
    assert warnings == []
    assert plan.not_reviewed_files == (long_name,)
    assert plan.batches == ()


def test_code_package_output_is_unchanged_by_the_shared_rendering_helper():
    """Golden string: the planner and code_package share file_header/numbered_lines,
    so this pins code_package's exact bytes (FR-014 line-number format)."""
    from veritas.review.nodes.common import code_package

    files = {"src/b.py": "y = 2\n", "src/a.py": "x = 1\nz = 3\n"}
    expected = (
        "### FILE: src/a.py\n"
        "    1| x = 1\n"
        "    2| z = 3\n"
        "\n"
        "### FILE: src/b.py\n"
        "    1| y = 2"
    )
    assert code_package(files) == expected


def test_whole_file_block_matches_code_package_block_format():
    """A batch's whole-file block is exactly what code_package renders for it."""
    from veritas.review.nodes.common import code_package

    files = {"src/a.py": "x = 1\nz = 3\n"}
    plan, _ = plan_batches(files, batch_chars=4000, max_batches=8)
    assert plan.batches[0].text == code_package(files)


# --- application code before test files (T083, FR-029) ---


def test_every_source_file_is_batched_before_any_test_file():
    files = _mixed_scope()
    plan, warnings = plan_batches(files, batch_chars=1200, max_batches=8)
    assert warnings == []
    # Three source batches, then the tests: two test blocks (994 chars together)
    # share the fourth and the last test takes one of its own.
    assert len(plan.batches) == 5

    sources = {path for path in files if not _is_test(files, path)}
    tests = {path for path in files if _is_test(files, path)}
    # The failure this fixes: every test path sorts before every source path, so
    # sorted-order planning would have put the tests in the first batches.
    assert all(min(tests) < path for path in sources)
    assert max(_batch_of(plan, path) for path in sources) < min(
        _batch_of(plan, path) for path in tests
    )


def test_a_tight_batch_limit_drops_tests_and_never_application_code():
    files = {
        "veritas/app.py": _lines(30),
        "veritas/batching.py": _lines(30),
    }
    files.update({f"tests/unit/test_{index}.py": _lines(30) for index in range(6)})
    # Each block is 726 chars and the budget holds one, so the two batches go to
    # the first two files of whichever ordering is used - and only to those.
    budgets = {"batch_chars": 800, "max_batches": 2}

    plan, warnings = plan_batches(files, **budgets)
    assert warnings == []

    # What the requirement asks for: the cap reaches tests, never the code.
    assert set(plan.reviewed_files) == {"veritas/app.py", "veritas/batching.py"}
    assert all(_is_test(files, path) for path in plan.not_reviewed_files)
    assert len(plan.not_reviewed_files) == 6

    # And what it replaced: with sorted ordering the two batches go to the tests
    # that sort first, and application code is left unreviewed.
    sorted_plan, _ = _sorted_order_plan(files, **budgets)
    assert set(sorted_plan.reviewed_files) == {
        "tests/unit/test_0.py",
        "tests/unit/test_1.py",
    }
    assert {path for path in files if not _is_test(files, path)} <= set(sorted_plan.not_reviewed_files)


def test_a_source_file_is_still_split_and_a_small_test_still_rides_along():
    # The test-first rule changes placement, not the algorithm: an oversized
    # source file still splits at line boundaries, and a small test file that fits
    # the last batch is still reviewed under the rule that runs after the cap.
    files = {
        "veritas/large.py": _lines(200),
        "tests/unit/test_tiny.py": "x = 1\n",
    }
    plan, warnings = plan_batches(files, batch_chars=1000, max_batches=8)
    assert warnings == []
    assert plan.split_files == ("veritas/large.py",)
    assert plan.not_reviewed_files == ()
    assert [c.path for c in plan.batches[-1].chunks] == [
        "veritas/large.py",
        "tests/unit/test_tiny.py",
    ]
    assert list(plan.reviewed_files) == sorted(files), "coverage lists stay in path order"
    assert list(plan.not_reviewed_files) == sorted(plan.not_reviewed_files)


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(
            {f"veritas/pkg/{name}.py": _lines(30) for name in ("app", "core", "util")},
            id="only source files",
        ),
        pytest.param(
            {f"tests/unit/test_{name}.py": _lines(30) for name in ("app", "core", "util")},
            id="only test files",
        ),
    ],
)
def test_a_scope_of_one_kind_of_file_plans_exactly_as_sorted_order_did(files):
    # One group means the two orderings coincide, so nothing about the existing
    # behaviour changes for a scope that has tests or has no tests.
    for budgets in ({"batch_chars": 800, "max_batches": 2}, {"batch_chars": 48000, "max_batches": 8}):
        plan, warnings = plan_batches(files, **budgets)
        sorted_plan, sorted_warnings = _sorted_order_plan(files, **budgets)
        assert plan == sorted_plan
        assert warnings == sorted_warnings


def test_the_plan_is_independent_of_dict_order_with_mixed_files():
    files = _mixed_scope()
    forward, forward_warnings = plan_batches(files, batch_chars=600, max_batches=8)
    backward, backward_warnings = plan_batches(
        dict(reversed(list(files.items()))), batch_chars=600, max_batches=8
    )
    assert forward == backward
    assert forward_warnings == backward_warnings


def test_invariants_hold_for_a_mixed_source_and_test_scope():
    files = _mixed_scope()
    for batch_chars in (400, 600, 1500, 48000):
        plan, _ = plan_batches(files, batch_chars=batch_chars, max_batches=8)
        _assert_invariants(plan, files, batch_chars)


def test_a_split_test_file_is_never_reviewed_before_a_source_file():
    # The ordering is by file, not by block: a source file that needs several
    # chunks still comes before a test file that would fit in the batch after it.
    files = {
        "veritas/large.py": _lines(200),
        "tests/unit/test_small.py": "x = 1\n",
        "tests/unit/test_also_small.py": "y = 2\n",
    }
    plan, _ = plan_batches(files, batch_chars=1000, max_batches=8)
    holders = {path: _batch_of(plan, path) for path in files}
    assert holders["veritas/large.py"] < holders["tests/unit/test_small.py"]
    assert holders["tests/unit/test_small.py"] <= holders["tests/unit/test_also_small.py"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("veritas.review.batching", "veritas.review.test_index"),
        ("veritas.review.test_index", "veritas.review.batching"),
        ("veritas.review.graph", "veritas.review.batching"),
    ],
)
def test_the_planner_imports_the_test_index_without_a_cycle(first, second):
    # batching -> test_index is a runtime import, so each entry point has to be
    # importable first on its own: test_index must not import back.
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}; import {second}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# --- a Python module named test_* is application code (T084, FR-004) ---


def _module_source(functions: int = 12) -> str:
    """A plausible application module: functions, no test* definitions."""
    return "".join(f"def helper_{index:06d}():\n    return {index}\n" for index in range(1, functions + 1))


def _test_source(functions: int = 12) -> str:
    return "".join(f"def test_thing_{index:06d}():\n    assert True\n" for index in range(1, functions + 1))


def _test_named_scope() -> dict[str, str]:
    """Source modules whose names look like tests, beside files that really are.

    Every block is 617 or 692 chars, so at ``batch_chars=800`` each file takes a
    batch of its own and a file's batch index is its place in the ordering.
    """
    return {
        "veritas/review/test_index.py": _module_source(),
        "veritas/review/nodes/test_coverage.py": _module_source(),
        "veritas/review/batching.py": _module_source(),
        "tests/unit/test_batching.py": _test_source(),
        "tests/unit/test_app.py": _test_source(),
    }


def test_a_test_named_source_module_is_batched_with_the_application_code():
    files = _test_named_scope()
    plan, warnings = plan_batches(files, batch_chars=800, max_batches=8)
    assert warnings == []
    assert len(plan.batches) == 5

    sources = {path for path in files if not _is_test(files, path)}
    tests = {path for path in files if _is_test(files, path)}
    assert sources == {
        "veritas/review/test_index.py",
        "veritas/review/nodes/test_coverage.py",
        "veritas/review/batching.py",
    }
    assert max(_batch_of(plan, path) for path in sources) < min(
        _batch_of(plan, path) for path in tests
    )
    assert plan.reviewed_files == tuple(sorted(files))


def test_a_test_named_source_module_is_never_dropped_before_a_real_test():
    # The regression: with the old name-only rule both modules were "tests", so at a
    # tight cap they were dropped with the suite while a test file survived.
    files = {
        "veritas/review/test_index.py": _module_source(),
        "veritas/review/batching.py": _lines(30),
        "tests/unit/test_batching.py": _lines(30),
        "tests/unit/test_app.py": _lines(30),
    }
    plan, _ = plan_batches(files, batch_chars=800, max_batches=2)

    assert set(plan.reviewed_files) == {
        "veritas/review/test_index.py",
        "veritas/review/batching.py",
    }
    assert plan.not_reviewed_files == ("tests/unit/test_app.py", "tests/unit/test_batching.py")


def test_the_same_name_gives_opposite_answers_inside_and_outside_a_test_tree():
    # src/test_helpers.py defines a test, so it is one; tests/unit/test_helpers.py
    # defines none but sits in a test tree, so it is one too; and the module of the
    # same name outside any tree defines none, so it is application code.
    files = {
        "veritas/review/test_index.py": _module_source(),
        "src/test_helpers.py": _test_source(),
        "tests/unit/test_helpers.py": _module_source(),
    }
    plan, _ = plan_batches(files, batch_chars=4000, max_batches=8)
    assert len(plan.batches) == 1, "small files share a batch, so order is unobservable"

    assert _is_test(files, "src/test_helpers.py") is True
    assert _is_test(files, "tests/unit/test_helpers.py") is True
    assert _is_test(files, "veritas/review/test_index.py") is False
    # The module is a source file to match against, so it leads the ordering.
    assert batching_module._batch_order(files)[0] == "veritas/review/test_index.py"
