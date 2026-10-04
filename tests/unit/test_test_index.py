"""Unit tests — test index (T081, FR-004).

The index is what stops the test-coverage review reporting tests as missing just
because they landed in another batch, so these pin the two things that go wrong
in practice: a file mistaken for a test (or a test mistaken for ordinary code),
and a long test suite silently eating the budget or claiming a file was omitted
when it was listed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritas.review.test_index import (
    INDEX_HEADER,
    NO_TEST_FILES_NOTICE,
    build_test_index,
    is_test_file,
)

_TEST_TREE_PATHS = (
    "tests/test_app.py",
    "src/tests/test_app.py",
    "src/test/helper.py",
    "web/__tests__/App.test.js",
    "src/app/tests/conftest.py",
)

_TEST_NAME_PATHS = (
    "test_app.py",
    "app_test.py",
    "app_test.go",
    "AppTest.java",
    "AppTests.java",
    "AppTest.cs",
    "AppTests.cs",
    "app.test.js",
    "app.test.ts",
    "app.test.jsx",
    "app.test.tsx",
    "app.spec.js",
    "app.spec.ts",
)

_LOOK_ALIKES = (
    "attest.py",
    "latest.py",
    "src/protest.py",
    "contest/main.py",
    "src/testing_utils.py",
    "src/app.py",
    "src/app.test.js.map",
    "src/AppTesting.java",
    "src/testimonial.js",
    "README.md",
)


@pytest.mark.parametrize("path", _TEST_TREE_PATHS + _TEST_NAME_PATHS)
def test_test_paths_are_recognised(path):
    assert is_test_file(path) is True


@pytest.mark.parametrize("path", _LOOK_ALIKES)
def test_ordinary_files_are_not_tests(path):
    assert is_test_file(path) is False


def test_empty_and_root_path_are_not_tests():
    assert is_test_file("") is False
    assert is_test_file("/") is False
    assert is_test_file("///") is False


def test_uppercase_directory_segment_is_not_a_test_tree():
    # Only the exact lower-case segment names a test tree: a directory called
    # TESTS proves nothing, and the segment names are not normalised case.
    assert is_test_file("TESTS/helpers.py") is False


def test_windows_separated_test_tree_is_recognised():
    # A local walk on Windows can hand over backslashes, so they are normalised
    # before the segment check rather than becoming part of a file name.
    assert is_test_file("a\\tests\\b.py") is True
    assert is_test_file("a\\app_test.py") is True


def test_python_names_extracted_without_executing_the_file():
    source = (
        "import pytest\n"
        "TOP = 1\n"
        "def test_alpha():\n    pass\n"
        "async def test_beta():\n    pass\n"
        "def helper():\n    pass\n"
        "class TestGamma:\n"
        "    def test_one(self):\n        pass\n"
        "    async def test_two(self):\n        pass\n"
        "    def helper(self):\n        pass\n"
        "class NotATestClass:\n"
        "    def test_nope(self):\n        pass\n"
        "class TestHelpers:\n"
        "    def build(self):\n        pass\n"
    )
    index, stats = build_test_index({"tests/test_alpha.py": source})
    assert index == (
        f"{INDEX_HEADER}\n"
        "- tests/test_alpha.py: test_alpha, test_beta, TestGamma.test_one, "
        "TestGamma.test_two"
    )
    assert stats == {
        "test_files": 1,
        "test_names": 4,
        "chars": len(index),
        "truncated": False,
        "omitted_files": 0,
    }


def test_a_test_prefixed_function_is_named_even_if_it_is_a_helper():
    # test* is a prefix match, exactly as test discovery treats it, so a helper
    # called test_utils is listed rather than silently dropped from the index.
    source = "def testing_helper():\n    pass\n"
    index, stats = build_test_index({"tests/test_prefix.py": source})
    assert index.endswith("- tests/test_prefix.py: testing_helper")
    assert stats["test_names"] == 1


def test_import_time_side_effect_is_not_executed():
    # A test module that would raise or write on import must not be imported to
    # be indexed; parsing alone must suffice.
    source = "raise RuntimeError('imported at index time')\ndef test_x():\n    pass\n"
    index, stats = build_test_index({"tests/test_boom.py": source})
    assert index.endswith("- tests/test_boom.py: test_x")
    assert stats["test_names"] == 1


def test_nested_test_function_is_not_collected():
    # pytest does not collect a nested function, so neither does the index.
    source = "def test_outer():\n    def test_inner():\n        pass\n"
    index, stats = build_test_index({"tests/test_nested.py": source})
    assert index.endswith("- tests/test_nested.py: test_outer")
    assert stats["test_names"] == 1


def test_test_method_of_a_non_test_class_is_not_collected():
    source = "class Helper:\n    def test_x(self):\n        pass\n"
    _, stats = build_test_index({"tests/test_helper.py": source})
    assert stats["test_names"] == 0


def test_python_file_without_tests_is_listed_by_path_only():
    index, stats = build_test_index({"tests/conftest.py": "import pytest\n"})
    assert index == f"{INDEX_HEADER}\n- tests/conftest.py"
    assert stats["test_files"] == 1
    assert stats["test_names"] == 0


def test_unparseable_python_file_is_listed_and_marked():
    index, stats = build_test_index({"tests/test_broken.py": "def test_x(:\n"})
    assert index == f"{INDEX_HEADER}\n- tests/test_broken.py (could not parse)"
    # The path is known; the names are not, so they are not counted as found.
    assert stats["test_files"] == 1
    assert stats["test_names"] == 0


def test_non_python_test_files_are_listed_by_path_only():
    files = {
        "src/app.test.ts": "describe('app', () => {});\n",
        "web/helper_test.go": "package web\n",
        "src/AppTest.java": "class AppTest {}\n",
    }
    index, stats = build_test_index(files)
    assert index == (
        f"{INDEX_HEADER}\n"
        "- src/AppTest.java\n"
        "- src/app.test.ts\n"
        "- web/helper_test.go"
    )
    assert stats["test_names"] == 0


def test_ordinary_source_files_are_left_out_entirely():
    files = {
        "src/app.py": "def test_nothing():\n    pass\n",
        "src/util.py": "x = 1\n",
        "README.md": "# hi\n",
    }
    index, stats = build_test_index(files)
    assert index is None
    assert stats["test_files"] == 0


def test_index_is_sorted_by_path_and_independent_of_input_order():
    files = {
        "tests/unit/test_c.py": "def test_c():\n    pass\n",
        "tests/unit/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": "def test_b():\n    pass\n",
        "src/app.py": "x = 1\n",
    }
    forward, forward_stats = build_test_index(files)
    reversed_order, reversed_stats = build_test_index(dict(reversed(list(files.items()))))
    assert forward == reversed_order
    assert forward_stats == reversed_stats
    assert forward.splitlines()[1:] == [
        "- tests/test_b.py: test_b",
        "- tests/unit/test_a.py: test_a",
        "- tests/unit/test_c.py: test_c",
    ]


def test_no_test_files_returns_none_and_a_zeroed_stat_block():
    index, stats = build_test_index({"src/app.py": "x = 1\n", "docs/requirements.md": "x\n"})
    assert index is None
    assert stats == {
        "test_files": 0,
        "test_names": 0,
        "chars": 0,
        "truncated": False,
        "omitted_files": 0,
    }


def test_no_test_files_notice_says_outside_tests_may_exist():
    # The notice is what stops the model turning "no tests in scope" into "no
    # tests exist", which is what the requirement forbids.
    assert "No test files were found in the reviewed scope." in NO_TEST_FILES_NOTICE
    assert "Tests outside the reviewed scope may exist." in NO_TEST_FILES_NOTICE


def test_default_budget_is_8000_characters():
    files = {f"tests/test_{index:03d}.py": f"def test_{index}():\n    pass\n" for index in range(900)}
    index, stats = build_test_index(files)
    assert index is not None
    assert stats["chars"] == len(index)
    assert stats["chars"] <= 8000
    assert stats["truncated"] is True
    assert stats["test_files"] == 900
    assert stats["omitted_files"] > 0


def test_truncation_keeps_whole_file_lines_and_counts_the_omitted():
    files = {f"tests/test_{index:02d}.py": f"def test_{index}():\n    pass\n" for index in range(40)}
    index, stats = build_test_index(files, max_chars=400)
    lines = index.splitlines()
    assert lines[0] == INDEX_HEADER
    assert lines[-1] == "...and 31 more test file(s) not listed (index limit 400 characters)."
    listed = lines[1:-1]
    assert listed == [f"- tests/test_{i:02d}.py: test_{i}" for i in range(9)]
    # Every file line is intact and in order: no path cut in half, no file listed
    # after one that sorts later.
    assert listed == sorted(listed)
    assert stats == {
        "test_files": 40,
        "test_names": 40,
        "chars": len(index),
        "truncated": True,
        "omitted_files": 31,
    }
    assert len(index) == 393


def test_truncated_note_count_matches_the_files_absent_from_the_index():
    files = {f"tests/test_{index:02d}.py": f"def test_{index}():\n    pass\n" for index in range(20)}
    index, stats = build_test_index(files, max_chars=250)
    listed = index.splitlines()[1:-1]
    omitted_from_text = {f"tests/test_{i:02d}.py" for i in range(20)} - {
        line.split(": ", 1)[0][2:] for line in listed
    }
    assert stats["omitted_files"] == len(omitted_from_text)
    assert stats["omitted_files"] == stats["test_files"] - len(listed)


@pytest.mark.parametrize("max_chars", (100, 200, 260, 400, 1000, 4000))
def test_index_never_exceeds_the_budget(max_chars):
    files = {
        f"tests/test_{index:02d}.py": f"def test_{index}():\n    pass\n" for index in range(30)
    }
    files["tests/with_a_long_name_that_costs_chars.py"] = (
        "def test_a_really_long_name_that_costs_a_lot_of_characters_to_write_out():\n    pass\n"
    )
    index, stats = build_test_index(files, max_chars=max_chars)
    if index is None:
        # Only legitimate when not even a one-file index fits the budget; the
        # files found are still counted so the caller can say so.
        assert stats["test_files"] == 31
        assert stats["chars"] == 0
        assert stats["truncated"] is True
        return
    assert stats["chars"] == len(index) <= max_chars
    assert index.startswith(f"{INDEX_HEADER}\n- tests/test_00.py")
    listed = index.splitlines()[1:-1] if stats["truncated"] else index.splitlines()[1:]
    # Whole file lines only, and the note accounts for every file not shown.
    assert len(listed) + stats["omitted_files"] == stats["test_files"]
    assert all(line.startswith("- tests/") for line in listed)


def test_exact_budget_boundary_is_inclusive():
    files = {"tests/test_a.py": "def test_a():\n    pass\n"}
    exact = len(f"{INDEX_HEADER}\n- tests/test_a.py: test_a")
    index, stats = build_test_index(files, max_chars=exact)
    assert index is not None
    assert stats["chars"] == exact
    assert stats["truncated"] is False
    index, stats = build_test_index(files, max_chars=exact - 1)
    assert stats["truncated"] is True


def test_budget_one_short_of_full_drops_files_and_says_how_many():
    files = {f"tests/test_{index:02d}.py": f"def test_{index}():\n    pass\n" for index in range(6)}
    full, _ = build_test_index(files)
    cap = len(full) - 1
    index, stats = build_test_index(files, max_chars=cap)
    lines = index.splitlines()
    assert lines[0] == INDEX_HEADER
    assert lines[1:-1] == [
        "- tests/test_00.py: test_0",
        "- tests/test_01.py: test_1",
        "- tests/test_02.py: test_2",
    ]
    assert lines[-1] == f"...and 3 more test file(s) not listed (index limit {cap} characters)."
    assert stats == {
        "test_files": 6,
        "test_names": 6,
        "chars": len(index),
        "truncated": True,
        "omitted_files": 3,
    }


def test_oversized_first_line_falls_back_to_a_file_and_name_count():
    # One file whose names alone exceed the budget still has to be visible: the
    # shorter form says it exists and how much is in it.
    files = {
        "tests/test_huge.py": "".join(
            f"def test_{letter * 40}_number_{index}():\n    pass\n"
            for index in range(4)
            for letter in ("a", "b", "c")
        ),
        "tests/test_small.py": "def test_small():\n    pass\n",
    }
    index, stats = build_test_index(files, max_chars=220)
    assert index.splitlines()[1] == "- tests/test_huge.py: 12 test(s), names omitted for length"
    assert index.splitlines()[2] == "...and 1 more test file(s) not listed (index limit 220 characters)."
    assert stats["chars"] == len(index) <= 220
    assert stats["test_names"] == 13


def test_nothing_fits_returns_none_with_the_files_still_counted():
    files = {"tests/test_a.py": "def test_a():\n    pass\n", "tests/test_b.py": "x\n"}
    index, stats = build_test_index(files, max_chars=40)
    assert index is None
    assert stats["test_files"] == 2
    assert stats["chars"] == 0
    assert stats["truncated"] is True
    assert stats["omitted_files"] == 2


def test_long_test_paths_still_fit_and_are_omitted_individually():
    files = {f"tests/{'nested/' * 12}test_{index}.py": f"def test_{index}():\n    pass\n" for index in range(8)}
    index, stats = build_test_index(files, max_chars=600)
    lines = index.splitlines()
    assert len(lines) > 2
    assert lines[-1].startswith("...and ")
    assert stats["omitted_files"] == 8 - (len(lines) - 2)


def test_a_file_with_no_trailing_newline_is_parsed():
    index, _ = build_test_index({"tests/test_a.py": "def test_a():\n    pass"})
    assert index.endswith("- tests/test_a.py: test_a")


def test_docstring_and_comment_mentions_of_tests_are_not_names():
    source = (
        '"""Module of test things."""\n'
        "# def test_not_a_function():\n"
        "class TestReal:\n"
        "    def test_real(self):\n        pass\n"
    )
    _, stats = build_test_index({"tests/test_doc.py": source})
    assert stats["test_names"] == 1


def test_empty_python_test_file_is_listed_without_names():
    index, stats = build_test_index({"tests/test_empty.py": ""})
    assert index == f"{INDEX_HEADER}\n- tests/test_empty.py"
    assert stats["test_files"] == 1
    assert stats["test_names"] == 0


def test_stats_keys_are_stable_regardless_of_outcome():
    for files, cap in (
        ({}, 8000),
        ({"src/a.py": "x\n"}, 8000),
        ({"tests/test_a.py": "def test_a():\n    pass\n"}, 8000),
        ({f"tests/test_{i}.py": "def test_a():\n    pass\n" for i in range(50)}, 200),
    ):
        _, stats = build_test_index(files, max_chars=cap)
        assert set(stats) == {
            "test_files",
            "test_names",
            "chars",
            "truncated",
            "omitted_files",
        }


def test_module_reads_nothing_and_configures_nothing():
    # The module stays pure: the index is built from contents already in state,
    # so a review cannot change what gets indexed by reading a new file, and no
    # test path can depend on the machine's locale, clock or environment.
    import ast

    import veritas.review.test_index as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"__future__", "ast", "dataclasses", "fnmatch"}