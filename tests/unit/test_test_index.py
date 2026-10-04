"""Unit tests — relevance-ordered test index (T082, FR-004).

Each batch gets its own index, ranked for the code that batch holds, so a
truncation costs the reviewer the tests that matter least rather than an
alphabetical tail. These pin the three things that go wrong in practice: a file
mistaken for a test (or the reverse), a batch whose most relevant tests fall off
the end of the index, and a note that miscounts what was dropped.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from veritas.review.batching import BatchPlan, FileChunk, ReviewBatch
from veritas.review.test_index import (
    INDEX_HEADER,
    NO_TEST_FILES_NOTICE,
    build_batch_test_indexes,
    is_test_file,
    module_path,
    subject_stem,
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

# One source file per batch, so a batch's ranking can be read off its index.
_SUBJECTS = ("veritas/config/settings.py", "veritas/review/batching.py")


def _plan(*paths_per_batch: list[str]) -> BatchPlan:
    """A plan with one batch per argument, each holding the given paths."""
    batches = []
    for position, paths in enumerate(paths_per_batch, start=1):
        chunks = tuple(
            FileChunk(path=path, start_line=1, end_line=1, total_lines=1) for path in paths
        )
        batches.append(ReviewBatch(index=position, chunks=chunks, text="code"))
    return BatchPlan(
        batches=tuple(batches), reviewed_files=(), split_files=(), not_reviewed_files=()
    )


def _paths_of(index: str) -> list[str]:
    """The test file paths in an index, in the order it lists them."""
    return [
        line[2:].split(":", 1)[0].split(" (", 1)[0]
        for line in index.splitlines()[1:]
        if not line.startswith("...")
    ]


def _listed(index: str) -> list[str]:
    """The index's file lines, excluding the header and any omission note."""
    lines = index.splitlines()[1:]
    return [line for line in lines if not line.startswith("...")]


def _omitted(index: str) -> int:
    return int(index.rsplit("...and ", 1)[1].split(" ", 1)[0])


def _omitted_or_zero(index: str) -> int:
    """How many files the note says were dropped, or 0 when nothing was."""
    return _omitted(index) if index.rsplit("\n", 1)[-1].startswith("...and ") else 0


# --- is_test_file: unchanged by T082, kept so a rename cannot quietly break it ---


@pytest.mark.parametrize("path", _TEST_TREE_PATHS + _TEST_NAME_PATHS)
def test_test_paths_are_recognised(path):
    assert is_test_file(path) is True


@pytest.mark.parametrize("path", _LOOK_ALIKES)
def test_ordinary_files_are_not_tests(path):
    assert is_test_file(path) is False


def test_empty_and_root_path_are_not_tests():
    assert is_test_file("") is False
    assert is_test_file("/") is False


def test_uppercase_directory_segment_is_not_a_test_tree():
    # Only the exact lower-case segment names a test tree: a directory called
    # TESTS proves nothing, and the segment names are not normalised case.
    assert is_test_file("TESTS/helpers.py") is False


def test_windows_separated_test_tree_is_recognised():
    assert is_test_file("a\\tests\\b.py") is True
    assert is_test_file("a\\app_test.py") is True


# --- subject_stem ---


@pytest.mark.parametrize(
    "path",
    (
        "tests/unit/test_batching.py",
        "batching_test.go",
        "BatchingTest.java",
        "BatchingTests.cs",
        "batching.test.ts",
        "batching.spec.js",
        "batching.py",
    ),
)
def test_every_marker_form_gives_the_same_subject(path):
    # The whole point of the stem: one name for the thing under test, whichever
    # language's convention spelled it.
    assert subject_stem(path) == "batching"


def test_subject_stem_is_lower_cased_and_case_insensitive():
    assert subject_stem("src/Batching.py") == "batching"
    assert subject_stem("tests/unit/TEST_BATCHING.PY") == "batching"


def test_subject_stem_keeps_a_name_that_is_only_a_marker():
    # Stripping must not empty the stem: "test.py" is a file called test.
    assert subject_stem("tests/test.py") == "test"
    assert subject_stem("tests/test_.py") == "test_"


def test_subject_stem_of_an_unmarked_name_is_the_stem():
    assert subject_stem("src/app.py") == "app"
    assert subject_stem("web/components/Button.tsx") == "button"


def test_subject_stem_drops_the_dot_marker_with_its_dot():
    # ".test" must be stripped before the bare "test" suffix, or "batching.test"
    # would come back as "batching." with a trailing dot that never matches.
    assert subject_stem("src/batching.test.ts") == "batching"
    assert subject_stem("src/batching.spec.ts") == "batching"


# --- module_path ---


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        ("veritas/config/settings.py", "veritas.config.settings"),
        ("veritas/review/batching.py", "veritas.review.batching"),
        ("src/app.py", "src.app"),
        ("app.py", "app"),
        ("a/b/c.py", "a.b.c"),
        ("veritas/review/__init__.py", "veritas.review"),
        ("tests/unit/__init__.py", "tests.unit"),
        ("__init__.py", None),
        ("./src/app.py", "src.app"),
        ("veritas\\review\\batching.py", "veritas.review.batching"),
    ),
)
def test_module_path(path, expected):
    assert module_path(path) == expected


@pytest.mark.parametrize(
    "path",
    ("app.test.ts", "app_test.go", "AppTest.java", "README.md", "src/app.pyc", "Makefile"),
)
def test_module_path_is_none_for_non_python(path):
    # Only a .py suffix has a dotted module, so no other file can be matched by
    # import.
    assert module_path(path) is None


# --- ranking: three tiers, per batch ---


def _ranking_files() -> dict[str, str]:
    return {
        "veritas/config/settings.py": "x = 1\n",
        "veritas/review/batching.py": "y = 2\n",
        # Tier 1 for the batching batch: the subject name matches.
        "tests/unit/test_batching.py": "def test_packs():\n    pass\n",
        # Tier 2 for the settings batch: its text names the module it imports.
        "tests/unit/test_settings.py": (
            "from veritas.config.settings import Settings\n\n\ndef test_defaults():\n    pass\n"
        ),
        "tests/unit/test_unrelated.py": "def test_nothing():\n    pass\n",
        "tests/zz_orphan.py": "def test_orphan():\n    pass\n",
    }


def test_name_match_ranks_first_for_the_matching_batch():
    files = _ranking_files()
    indexes, _ = build_batch_test_indexes(files, _plan(["veritas/review/batching.py"]))

    assert _paths_of(indexes[1]) == [
        "tests/unit/test_batching.py",
        "tests/unit/test_settings.py",
        "tests/unit/test_unrelated.py",
        "tests/zz_orphan.py",
    ]


def test_import_match_ranks_before_an_unrelated_test_file():
    files = _ranking_files()
    indexes, _ = build_batch_test_indexes(files, _plan(["veritas/config/settings.py"]))

    # No subject name matches, so the file that imports veritas.config.settings is
    # promoted over the tests that mention nothing from this batch.
    assert _paths_of(indexes[1]) == [
        "tests/unit/test_settings.py",
        "tests/unit/test_batching.py",
        "tests/unit/test_unrelated.py",
        "tests/zz_orphan.py",
    ]


def test_two_batches_get_different_orderings():
    files = _ranking_files()
    plan = _plan(["veritas/config/settings.py"], ["veritas/review/batching.py"])
    indexes, _ = build_batch_test_indexes(files, plan)

    assert set(indexes) == {1, 2}
    assert _paths_of(indexes[1]) != _paths_of(indexes[2])
    assert _paths_of(indexes[1])[0] == "tests/unit/test_settings.py"
    assert _paths_of(indexes[2])[0] == "tests/unit/test_batching.py"


def test_a_name_match_beats_an_import_match():
    # One file matches both ways; the stronger signal has to win, or the tier
    # ordering is untested. The import-only file sorts first by path, so if the
    # tiers were ignored it would come out ahead.
    files = {
        "veritas/config/settings.py": "x = 1\n",
        "tests/unit/test_aaa_imports.py": (
            "from veritas.config.settings import Settings\n\n\ndef test_load():\n    pass\n"
        ),
        "tests/unit/test_settings.py": (
            "from veritas.config.settings import Settings\n\n\ndef test_defaults():\n    pass\n"
        ),
    }
    indexes, _ = build_batch_test_indexes(files, _plan(["veritas/config/settings.py"]))

    assert _paths_of(indexes[1]) == [
        "tests/unit/test_settings.py",
        "tests/unit/test_aaa_imports.py",
    ]


def test_a_test_file_is_never_a_subject():
    # Two test files for the same source in one batch: neither may promote the
    # other, or every file in a test-heavy batch would rank first.
    files = {
        "src/batching.py": "y = 2\n",
        "tests/test_batching.py": "def test_a():\n    pass\n",
        "tests/test_batching_extra.py": "def test_b():\n    pass\n",
    }
    indexes, _ = build_batch_test_indexes(files, _plan(["src/batching.py", "tests/test_batching.py"]))

    # Both are tier 1 by name, and inside a tier the order is by path.
    assert _paths_of(indexes[1]) == ["tests/test_batching.py", "tests/test_batching_extra.py"]


def test_every_test_file_appears_in_every_batch():
    files = _ranking_files()
    plan = _plan(["veritas/config/settings.py"], ["veritas/review/batching.py"])
    indexes, stats = build_batch_test_indexes(files, plan)

    for index in indexes.values():
        assert sorted(_paths_of(index)) == sorted(
            path for path in files if is_test_file(path)
        )
    assert stats["test_files"] == 4
    assert stats["batches"] == 2


def test_the_rest_of_each_group_is_sorted_by_path():
    files = {
        "src/app.py": "x = 1\n",
        "tests/test_c.py": "def test_c():\n    pass\n",
        "tests/test_a.py": "def test_a():\n    pass\n",
        "tests/test_b.py": "def test_b():\n    pass\n",
    }
    indexes, _ = build_batch_test_indexes(files, _plan(["src/app.py"]))

    assert _paths_of(indexes[1]) == ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]


def test_a_chunk_of_a_split_file_still_matches_its_test():
    # A large source file appears once per batch it was split across; a batch
    # holding only the tail chunk still gets the tier-1 test.
    files = {
        "veritas/review/batching.py": "y = 2\n",
        "tests/unit/test_batching.py": "def test_packs():\n    pass\n",
    }
    chunk = FileChunk(path="veritas/review/batching.py", start_line=500, end_line=600, total_lines=900)
    plan = BatchPlan(
        batches=(ReviewBatch(index=1, chunks=(chunk,), text="code"),),
        reviewed_files=(),
        split_files=(),
        not_reviewed_files=(),
    )
    indexes, _ = build_batch_test_indexes(files, plan)

    assert _paths_of(indexes[1])[0] == "tests/unit/test_batching.py"


# --- per-batch truncation ---


def _many_files(count: int = 24) -> dict[str, str]:
    return {
        "src/batching.py": "y = 2\n",
        "tests/unit/test_batching.py": "def test_packs():\n    pass\n",
        "tests/unit/test_settings_import.py": (
            "import veritas.config.settings\n\n\ndef test_load():\n    pass\n"
        ),
        **{
            f"tests/test_filler_{index:02d}.py": f"def test_filler_{index}():\n    pass\n"
            for index in range(count)
        },
    }


@pytest.mark.parametrize(
    ("max_chars", "kept"),
    ((200, (1, 1)), (250, (2, 2)), (300, (3, 3)), (400, (6, 5)), (600, (10, 10)), (900, (18, 17))),
)
def test_truncation_drops_the_least_relevant_files_first(max_chars, kept):
    # Two batches with different subjects, one budget: each keeps its own relevant
    # test and loses the fillers that sort before it alphabetically. The kept
    # counts differ by one because the two batches' relevant lines differ in
    # length, which is why they are pinned per batch.
    files = _many_files()
    plan = _plan(["src/batching.py"], ["veritas/config/settings.py"])
    indexes, stats = build_batch_test_indexes(files, plan, max_chars=max_chars)

    assert set(indexes) == {1, 2}
    assert _listed(indexes[1])[0] == "- tests/unit/test_batching.py: test_packs"
    assert _listed(indexes[2])[0] == "- tests/unit/test_settings_import.py: test_load"
    for batch, count in enumerate(kept, start=1):
        index = indexes[batch]
        assert len(index) <= max_chars
        assert len(_listed(index)) == count
        assert _omitted(index) == stats["test_files"] - count
    assert stats["truncated_batches"] == 2
    assert stats["test_files"] == 26


def test_an_index_that_fits_reports_no_omission():
    files = _many_files()
    indexes, stats = build_batch_test_indexes(
        files, _plan(["src/batching.py"]), max_chars=1600
    )

    assert _omitted_or_zero(indexes[1]) == 0
    assert len(_listed(indexes[1])) == stats["test_files"]
    assert stats["truncated_batches"] == 0


def test_truncation_keeps_the_relevant_files_when_the_budget_is_tiny():
    # At a budget that holds one file, the one it keeps is the batch's own test -
    # an alphabetical index would have kept a filler instead.
    files = _many_files()
    indexes, stats = build_batch_test_indexes(files, _plan(["src/batching.py"]), max_chars=200)

    assert _listed(indexes[1]) == ["- tests/unit/test_batching.py: test_packs"]
    assert _omitted(indexes[1]) == 25
    assert stats["truncated_batches"] == 1
    assert stats["max_chars"] == 200


def test_no_truncation_reports_no_omission_and_zero_truncated_batches():
    files = _many_files(count=2)
    indexes, stats = build_batch_test_indexes(files, _plan(["src/batching.py"]), max_chars=16000)

    assert "...and" not in indexes[1]
    assert stats == {
        "test_files": 4,
        "test_names": 4,
        "batches": 1,
        "truncated_batches": 0,
        "max_chars": 16000,
    }


def test_nothing_fitting_anywhere_leaves_the_batch_out_and_counts_it():
    files = _many_files(count=1)
    indexes, stats = build_batch_test_indexes(files, _plan(["src/batching.py"]), max_chars=40)

    assert indexes == {}
    assert stats["truncated_batches"] == 1
    assert stats["test_files"] == 3


def test_oversized_first_line_falls_back_to_a_file_and_name_count():
    files = {
        "src/batching.py": "y = 2\n",
        "tests/unit/test_batching.py": "".join(
            f"def test_{letter * 40}_number_{index}():\n    pass\n"
            for index in range(4)
            for letter in ("a", "b", "c")
        ),
        "tests/test_small.py": "def test_small():\n    pass\n",
    }
    indexes, stats = build_batch_test_indexes(files, _plan(["src/batching.py"]), max_chars=220)
    listed = _listed(indexes[1])

    assert listed == ["- tests/unit/test_batching.py: 12 test(s), names omitted for length"]
    assert _omitted(indexes[1]) == 1
    assert stats["truncated_batches"] == 1
    assert len(indexes[1]) <= 220


def test_default_budget_is_16000_characters():
    files = {f"tests/test_{index:04d}.py": f"def test_{index}():\n    pass\n" for index in range(400)}
    _, stats = build_batch_test_indexes(files, _plan(["src/app.py"]))

    assert stats["max_chars"] == 16000


def test_every_index_is_within_the_budget_at_every_cap():
    files = _many_files(count=40)
    plan = _plan(["src/batching.py"], ["veritas/config/settings.py"])
    for max_chars in range(150, 2000, 37):
        indexes, stats = build_batch_test_indexes(files, plan, max_chars=max_chars)
        assert stats["max_chars"] == max_chars
        assert set(indexes) <= {1, 2}
        for index in indexes.values():
            assert len(index) <= max_chars
            assert len(_listed(index)) + _omitted_or_zero(index) == stats["test_files"]


# --- stats, ordering and the no-test-files case ---


def test_stats_are_identical_for_every_batch_shape():
    files = _ranking_files()
    _, stats = build_batch_test_indexes(files, _plan(["veritas/config/settings.py"]))
    assert stats == {
        "test_files": 4,
        "test_names": 4,
        "batches": 1,
        "truncated_batches": 0,
        "max_chars": 16000,
    }


def test_result_is_independent_of_input_dict_order():
    files = _many_files(count=6)
    plan = _plan(["src/batching.py"], ["veritas/config/settings.py"])
    forward, forward_stats = build_batch_test_indexes(files, plan)
    backward, backward_stats = build_batch_test_indexes(dict(reversed(list(files.items()))), plan)

    assert forward == backward
    assert forward_stats == backward_stats


def test_result_is_independent_of_the_chunk_order_inside_a_batch():
    files = _many_files(count=6)
    forward, _ = build_batch_test_indexes(
        files, _plan(["src/batching.py", "veritas/config/settings.py"])
    )
    backward, _ = build_batch_test_indexes(
        files, _plan(["veritas/config/settings.py", "src/batching.py"])
    )

    # Same batch, same sources, different chunk order: the planner's ordering is
    # not an input to the index.
    assert forward == backward


def test_no_test_files_returns_none_and_a_zeroed_stat_block():
    files = {"src/app.py": "x = 1\n", "docs/requirements.md": "x\n"}
    indexes, stats = build_batch_test_indexes(files, _plan(["src/app.py"]))

    assert indexes is None
    assert stats == {
        "test_files": 0,
        "test_names": 0,
        "batches": 1,
        "truncated_batches": 0,
        "max_chars": 16000,
    }


def test_no_batches_yields_no_indexes():
    files = {"src/app.py": "x = 1\n", "tests/test_app.py": "def test_a():\n    pass\n"}
    indexes, stats = build_batch_test_indexes(files, _plan())

    assert indexes == {}
    assert stats == {
        "test_files": 1,
        "test_names": 1,
        "batches": 0,
        "truncated_batches": 0,
        "max_chars": 16000,
    }


def test_missing_plan_is_treated_as_no_batches():
    files = {"tests/test_app.py": "def test_a():\n    pass\n"}
    indexes, stats = build_batch_test_indexes(files, None)

    assert indexes == {}
    assert stats["batches"] == 0
    assert stats["test_files"] == 1


def test_no_test_files_notice_says_outside_tests_may_exist():
    # The notice is what stops the model turning "no tests in scope" into "no
    # tests exist", which is what the requirement forbids.
    assert "No test files were found in the reviewed scope." in NO_TEST_FILES_NOTICE
    assert "Tests outside the reviewed scope may exist." in NO_TEST_FILES_NOTICE


# --- extraction, unchanged but exercised through the batch index ---


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
    )
    indexes, stats = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/test_alpha.py": source}, _plan(["src/app.py"])
    )

    assert indexes[1] == (
        f"{INDEX_HEADER}\n"
        "- tests/test_alpha.py: test_alpha, test_beta, TestGamma.test_one, TestGamma.test_two"
    )
    assert stats["test_names"] == 4


def test_import_time_side_effect_is_not_executed():
    source = "raise RuntimeError('imported at index time')\ndef test_x():\n    pass\n"
    indexes, stats = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/test_boom.py": source}, _plan(["src/app.py"])
    )

    assert indexes[1].endswith("- tests/test_boom.py: test_x")
    assert stats["test_names"] == 1


def test_nested_test_function_is_not_collected():
    source = "def test_outer():\n    def test_inner():\n        pass\n"
    indexes, _ = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/test_nested.py": source}, _plan(["src/app.py"])
    )

    assert indexes[1].endswith("- tests/test_nested.py: test_outer")


def test_test_method_of_a_non_test_class_is_not_collected():
    source = "class Helper:\n    def test_x(self):\n        pass\n"
    _, stats = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/test_helper.py": source}, _plan(["src/app.py"])
    )

    assert stats["test_names"] == 0


def test_python_file_without_tests_is_listed_by_path_only():
    indexes, stats = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/conftest.py": "import pytest\n"}, _plan(["src/app.py"])
    )

    assert indexes[1] == f"{INDEX_HEADER}\n- tests/conftest.py"
    assert stats["test_files"] == 1
    assert stats["test_names"] == 0


def test_unparseable_python_file_is_listed_and_marked():
    indexes, stats = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/test_broken.py": "def test_x(:\n"}, _plan(["src/app.py"])
    )

    assert indexes[1] == f"{INDEX_HEADER}\n- tests/test_broken.py (could not parse)"
    # The path is known; the names are not, so they are not counted as found.
    assert stats["test_files"] == 1
    assert stats["test_names"] == 0


def test_non_python_test_files_are_listed_by_path_only():
    files = {
        "src/app.test.ts": "describe('app', () => {});\n",
        "web/helper_test.go": "package web\n",
        "src/AppTest.java": "class AppTest {}\n",
    }
    indexes, stats = build_batch_test_indexes(files, _plan(["src/app.test.ts"]))

    assert indexes[1] == (
        f"{INDEX_HEADER}\n- src/AppTest.java\n- src/app.test.ts\n- web/helper_test.go"
    )
    assert stats["test_names"] == 0


def test_ordinary_source_files_are_left_out_entirely():
    files = {"src/app.py": "def test_nothing():\n    pass\n", "src/util.py": "x = 1\n"}
    indexes, stats = build_batch_test_indexes(files, _plan(["src/app.py"]))

    assert indexes is None
    assert stats["test_files"] == 0


def test_a_file_with_no_trailing_newline_is_parsed():
    indexes, _ = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/test_a.py": "def test_a():\n    pass"}, _plan(["src/app.py"])
    )

    assert indexes[1].endswith("- tests/test_a.py: test_a")


def test_docstring_and_comment_mentions_of_tests_are_not_names():
    source = (
        '"""Module of test things."""\n'
        "# def test_not_a_function():\n"
        "class TestReal:\n"
        "    def test_real(self):\n        pass\n"
    )
    _, stats = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/test_doc.py": source}, _plan(["src/app.py"])
    )

    assert stats["test_names"] == 1


def test_empty_python_test_file_is_listed_without_names():
    indexes, stats = build_batch_test_indexes(
        {"src/app.py": "x = 1\n", "tests/test_empty.py": ""}, _plan(["src/app.py"])
    )

    assert indexes[1] == f"{INDEX_HEADER}\n- tests/test_empty.py"
    assert stats["test_files"] == 1
    assert stats["test_names"] == 0


def test_every_test_file_is_parsed_once_not_once_per_batch():
    # The scope is parsed once and only ranked per batch: a counter in the
    # module's own AST proves no second pass over the sources.
    import veritas.review.test_index as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    entry_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_entry"
    ]

    assert len(entry_calls) == 1


def test_module_reads_nothing_and_configures_nothing():
    # The module stays pure: the index is built from contents already in state,
    # so a review cannot change what gets indexed by reading a new file, and no
    # test path can depend on the machine's locale, clock or environment. The
    # planner is a type-only import, so nothing is pulled in at runtime.
    import veritas.review.test_index as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    type_only = set()
    runtime = set()
    for node in tree.body:
        targets = (
            node.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
            else [node]
        )
        for statement in targets:
            if isinstance(statement, ast.Import):
                names = {alias.name.split(".")[0] for alias in statement.names}
            elif isinstance(statement, ast.ImportFrom) and statement.module:
                names = {statement.module.split(".")[0]}
            else:
                continue
            (type_only if isinstance(node, ast.If) else runtime).update(names)

    assert runtime == {"__future__", "ast", "dataclasses", "fnmatch", "pathlib", "typing"}
    assert type_only == {"veritas"}