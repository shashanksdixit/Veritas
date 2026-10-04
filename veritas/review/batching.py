"""Deterministic batch planner for code-review inputs (T076/T083, FR-029).

Pure: no settings, no filesystem, no I/O — it takes the scoped file contents and
the two configured budgets and returns the batches plus coverage bookkeeping.
Every code review type consumes the same plan, so a run reviews the same code
regardless of which review type is looking at it.

Application code is batched before test files, each group in sorted path order:
when the batch limit is reached it is tests that go unreviewed, never the code
under review (FR-029). Test files are identified exactly as the test index
identifies them, so "which tests exist" and "which tests are reviewed" cannot
disagree.

A batch holds whole-file blocks and, for a file too large for one batch, one
chunk per batch at a time. Blocks inside a batch are joined with a blank line and
that separator counts against the character budget, so
``len(batch.text) <= batch_chars`` always holds. Line rendering is delegated to
``veritas.review.nodes.common`` so batched code and ``code_package`` output are
formatted identically.
"""

from __future__ import annotations

from dataclasses import dataclass

from veritas.review.nodes.common import file_header, numbered_lines
from veritas.review.test_index import is_test_file

# Blocks within a batch are separated by a blank line, and the separator counts
# toward the budget. Same separator code_package uses.
_BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class FileChunk:
    """A contiguous run of lines from one file, with its original line numbers."""

    path: str
    start_line: int
    end_line: int
    total_lines: int


@dataclass(frozen=True)
class ReviewBatch:
    """One batch of review input. ``index`` is 1-based."""

    index: int
    chunks: tuple[FileChunk, ...]
    text: str


@dataclass(frozen=True)
class BatchPlan:
    """The planned batches plus which files they cover (FR-029 coverage inputs)."""

    batches: tuple[ReviewBatch, ...]
    reviewed_files: tuple[str, ...]
    split_files: tuple[str, ...]
    not_reviewed_files: tuple[str, ...]


def _whole_file_block(path: str, content: str) -> str:
    """Header plus every line, numbered from 1. An empty file is header only."""
    lines = numbered_lines(content.splitlines())
    if not lines:
        return file_header(path)
    return "\n".join([file_header(path), *lines])


def _chunk_block(path: str, lines: list[str], start: int, end: int, total: int) -> str:
    """Range header plus lines ``start..end``, keeping their original numbers."""
    header = file_header(path, start_line=start, end_line=end, total_lines=total)
    return "\n".join([header, *numbered_lines(lines[start - 1 : end], start_line=start)])


def _rendered_prefix(lines: list[str]) -> list[int]:
    """Prefix sums of the RENDERED numbered line lengths, newline included.

    ``prefix[i]`` covers lines ``1..i``. Rendered lengths are used, not raw line
    lengths: each line carries a ``{n:>5}| `` prefix whose width grows with the
    line number, so line 100 renders wider than line 9.
    """
    prefix = [0]
    for number, line in enumerate(lines, start=1):
        prefix.append(prefix[-1] + len(f"{number:>5}| {line}") + 1)
    return prefix


def _chunk_block_len(
    path: str, start: int, end: int, total: int, prefix: list[int]
) -> int:
    """Exact length of the rendered chunk block for ``start..end``.

    ``prefix[end] - prefix[start - 1]`` counts each rendered line plus one
    newline; the last line's newline is not emitted, so the block is the header,
    a newline, and the body.
    """
    header = file_header(path, start_line=start, end_line=end, total_lines=total)
    return len(header) + prefix[end] - prefix[start - 1]


def _chunk_ranges(
    path: str, lines: list[str], total: int, batch_chars: int
) -> list[tuple[int, int]] | None:
    """Greedy line-boundary chunk ranges for an oversized file.

    Each range is grown while the full chunk block — range header included —
    still fits ``batch_chars``. Returns ``None`` when a single numbered line plus
    its chunk header cannot fit, which means the file cannot be split usefully.
    """
    prefix = _rendered_prefix(lines)
    ranges: list[tuple[int, int]] = []
    start = 1
    while start <= total:
        end = start
        if _chunk_block_len(path, start, end, total, prefix) > batch_chars:
            return None
        while end < total and (
            _chunk_block_len(path, start, end + 1, total, prefix) <= batch_chars
        ):
            end += 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def _start_batch(block: str, chunk: FileChunk) -> list[tuple[FileChunk, str]]:
    return [(chunk, block)]


def _finalize(batches: list[list[tuple[FileChunk, str]]]) -> tuple[ReviewBatch, ...]:
    return tuple(
        ReviewBatch(
            index=position,
            chunks=tuple(chunk for chunk, _ in blocks),
            text=_BLOCK_SEPARATOR.join(block for _, block in blocks),
        )
        for position, blocks in enumerate(batches, start=1)
    )


def _fits(blocks: list[tuple[FileChunk, str]], block: str, batch_chars: int) -> bool:
    used = sum(len(text) for _, text in blocks) + len(_BLOCK_SEPARATOR) * (len(blocks) - 1)
    return used + len(_BLOCK_SEPARATOR) + len(block) <= batch_chars


def _batch_order(files: dict[str, str]) -> list[str]:
    """Non-test source files first, then test files; each group in path order.

    ``False`` sorts before ``True``, so application code is placed before the tests
    that cover it and the batch cap can only reach the tests. Only batch
    composition depends on this; the coverage tuples are sorted on the way out, so
    the report still lists paths in path order (FR-029).
    """
    return sorted(files, key=lambda path: (is_test_file(path), path))


def plan_batches(
    files: dict[str, str], *, batch_chars: int, max_batches: int
) -> tuple[BatchPlan, list[str]]:
    """Plan review batches over ``files`` (FR-029).

    Deterministic and independent of ``files``' insertion order: paths are
    processed with non-test source files first and test files last, each group in
    sorted order. A file is assigned as follows:

    * it fits in the remaining space of the current (last) batch -> appended;
    * it fits in a batch of its own but not there -> a new batch, if fewer than
      ``max_batches`` batches exist, else it is not reviewed;
    * it is larger than one batch -> split at line boundaries, one chunk per
      new batch, keeping original line numbers. If the chunks do not all fit in
      the batches still available the WHOLE file is not reviewed (never a
      partial review), as it is when a single line cannot fit at all.

    Files are still tried after the cap is reached, so a small file that fits
    the last batch is reviewed rather than dropped - which, with tests placed
    last, is what lets a small test file still be reviewed while application code
    never is left out by the cap.

    Returns the plan and warning messages (ASCII only).
    """
    warnings: list[str] = []
    batches: list[list[tuple[FileChunk, str]]] = []
    reviewed: list[str] = []
    split: list[str] = []
    not_reviewed: list[str] = []

    for path in _batch_order(files):
        content = files[path]
        lines = content.splitlines()
        total = len(lines)
        whole = _whole_file_block(path, content)

        # 1. fits the current batch
        if batches and _fits(batches[-1], whole, batch_chars):
            batches[-1].append((FileChunk(path, 1, total, total), whole))
            reviewed.append(path)
            continue

        # 2. fits a batch of its own
        if len(whole) <= batch_chars:
            if len(batches) < max_batches:
                batches.append(_start_batch(whole, FileChunk(path, 1, total, total)))
                reviewed.append(path)
            else:
                not_reviewed.append(path)
            continue

        # 3. larger than one batch: split. An empty file cannot be split further
        # (its header alone is what overflowed), so it is simply not reviewed.
        if total == 0:
            not_reviewed.append(path)
            continue
        ranges = _chunk_ranges(path, lines, total, batch_chars)
        if ranges is None:
            warnings.append(
                f"batching: {path} not reviewed: a single line exceeds batch_chars ({batch_chars})"
            )
            not_reviewed.append(path)
            continue
        if len(ranges) > max_batches - len(batches):
            not_reviewed.append(path)
            continue
        for start, end in ranges:
            block = _chunk_block(path, lines, start, end, total)
            batches.append(_start_batch(block, FileChunk(path, start, end, total)))
        reviewed.append(path)
        split.append(path)

    return (
        BatchPlan(
            batches=_finalize(batches),
            # Sorted on the way out: batch composition follows the test-first rule,
            # but the coverage lists the report renders stay in path order.
            reviewed_files=tuple(sorted(reviewed)),
            split_files=tuple(sorted(split)),
            not_reviewed_files=tuple(sorted(not_reviewed)),
        ),
        warnings,
    )
