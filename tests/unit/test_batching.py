"""Unit tests — deterministic batch planner (T076, FR-029).

``plan_batches`` is pure, so the algorithm, its invariants and its rendering are
asserted directly. Scope wiring (only supported source files are batched) is in
``tests/integration/test_review_exclusion.py``.
"""

import pytest

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
