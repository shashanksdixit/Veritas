"""Unit tests — line-numbered, line-boundary-truncated code_package (T068, FR-014).

The package is what every review prompt sees, so two properties are load
bearing: a reviewer can copy a citation line number straight out of it, and
the whole string fits the caller's character budget no matter how small that
budget is.
"""

from __future__ import annotations

from pathlib import Path

from veritas.config.constants import PROMPT_VERSION
from veritas.review.nodes.common import code_package, current_prompt_version

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "veritas" / "review" / "prompts"

# Prompts that receive a code package, and therefore must tell the model how
# to read the line-number prefix.
_CODE_PROMPTS = (
    "code_quality",
    "performance",
    "requirements",
    "security",
    "test_coverage",
)

# Of those, the ones whose output schema has a start_line/end_line line range.
# requirements.md is the exception: it returns "file:line" evidence entries.
_LINE_RANGE_PROMPTS = (
    "code_quality",
    "performance",
    "security",
    "test_coverage",
)

# The two sentences every code prompt shares: how to read the prefix, and never to
# estimate a line number.
_LINE_NUMBER_RULE = (
    "Each code line is prefixed with its line number followed by '| '. "
    "Cite line numbers exactly as shown in that prefix; never estimate them."
)
# How the cited code gets transcribed then differs by output schema, so the third
# sentence is asserted per group rather than for all five prompts at once.
_CITED_SNIPPET_RULE = (
    "In cited_snippet, copy the code text only, without the line-number prefix."
)
_EVIDENCE_RULE = (
    "In evidence entries, write the file path and the line number shown in the "
    'prefix (for example "path/to/file.py:42"); never copy the prefix text itself.'
)

# FR-014: the line range must span the quoted snippet, not a guess at it. Both
# fragments below appear in all five code prompts; the line_range sentence appears
# only in the four whose output schema actually has start_line/end_line.
_COUNTING_RULE = "Count the lines you quote"
_QUOTE_EXACTLY_RULE = (
    "Quote the exact line or lines your finding is about; do not quote a "
    "neighbouring line (for example, do not quote an `if` condition while "
    "citing the line inside it)."
)
_LINE_RANGE_SENTENCE = (
    "start_line MUST be the line number of the first line of cited_snippet, and "
    "end_line MUST be the line number of its last line."
)

# FR-029: the code review types are shown chunked batches, so a chunk header can
# mean part of a file only. They must be told not to invent findings about code
# they cannot see. requirements.md is exempt: it still receives code_package,
# which never shows a partial file (B2).
_PARTIAL_FILE_RULE = (
    "Some files are shown in parts. A header such as "
    '"### FILE: path (lines 301-560 of 812)" means you can see only that range '
    "of the file. Do not report problems that exist only because code outside "
    "the shown range is not visible, such as imports or definitions you cannot "
    "see."
)


def _prompt(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    """Collapse the prompt's hard line wrapping so assertions can use one line."""
    return " ".join(text.split())


def test_line_one_is_numbered_with_one_based_prefix():
    out = code_package({"a.py": "x = 1\ny = 2\n"})
    assert out == "### FILE: a.py\n    1| x = 1\n    2| y = 2"


def test_every_line_numbered_in_sequence():
    out = code_package({"a.py": "one\ntwo\nthree\nfour\n"})
    assert [line.split("|", 1)[0] for line in out.splitlines()[1:]] == [
        "    1",
        "    2",
        "    3",
        "    4",
    ]


def test_content_without_trailing_newline_numbers_correctly():
    out = code_package({"a.py": "x = 1\ny = 2"})
    assert out == "### FILE: a.py\n    1| x = 1\n    2| y = 2"


def test_content_with_trailing_newline_numbers_identically():
    without = code_package({"a.py": "x = 1\ny = 2"})
    with_newline = code_package({"a.py": "x = 1\ny = 2\n"})
    assert without == with_newline


def test_trailing_newline_does_not_add_a_phantom_line():
    out = code_package({"a.py": "x = 1\n"})
    assert out == "### FILE: a.py\n    1| x = 1"


def test_truncation_never_splits_a_numbered_line():
    body = "".join(f"line {n} {'x' * 40}\n" for n in range(1, 41))
    out = code_package({"a.py": body}, max_chars=300)
    assert out.endswith("…(truncated)")
    for line in out.splitlines()[1:-1]:
        numbered, text = line.split("| ", 1)
        assert numbered.strip().isdigit()
        assert text in body.splitlines()


def test_truncated_package_ends_with_the_marker():
    body = "".join(f"line {n}\n" for n in range(1, 200))
    out = code_package({"a.py": body}, max_chars=200)
    assert out.endswith("…(truncated)")


def test_kept_line_numbers_stay_contiguous_from_one():
    body = "".join(f"line {n} {'y' * 30}\n" for n in range(1, 30))
    out = code_package({"a.py": body}, max_chars=400)
    numbers = [int(line.split("|", 1)[0]) for line in out.splitlines()[1:-1]]
    assert numbers == list(range(1, len(numbers) + 1))
    assert len(numbers) < 29


def test_never_exceeds_max_chars_for_tiny_budgets():
    files = {
        "a.py": "import os\nprint('a')\n",
        "b.py": "x = 2\n" * 5,
        "c.py": "y = 3\n",
    }
    for max_chars in (1, 5, 11, 12, 13, 20, 50, 200, 1000):
        out = code_package(files, max_chars=max_chars)
        assert len(out) <= max_chars, f"max_chars={max_chars} len={len(out)}"


def test_never_exceeds_max_chars_with_many_files():
    files = {f"pkg/mod_{n}.py": f"value_{n} = {n}\n" * 8 for n in range(30)}
    for max_chars in (0, 1, 5, 11, 12, 50, 200, 1000, 5000):
        out = code_package(files, max_chars=max_chars)
        assert len(out) <= max_chars


def test_tiny_budget_returns_empty_rather_than_exceeding():
    out = code_package({"a.py": "x = 1\n"}, max_chars=5)
    assert out == ""


def test_budget_below_minimum_block_omits_the_file_entirely():
    # A useful block needs the header, at least one numbered line and the
    # truncation marker; one character short of that, the file is omitted
    # rather than shown misleadingly (FR-014).
    files = {"a.py": "x = 1\n" * 3}
    smallest = next(n for n in range(1, 200) if code_package(files, max_chars=n))
    out = code_package(files, max_chars=smallest)
    assert out.startswith("### FILE: a.py\n    1| x = 1")
    assert out.endswith("…(truncated)")
    assert len(out) == smallest
    assert code_package(files, max_chars=smallest - 1) == ""


def test_budget_shorter_than_a_file_header_omits_the_file():
    # "### FILE: a.py" alone is 13 chars, so nothing can fit below that.
    assert code_package({"a.py": "x = 1\n"}, max_chars=12) == ""


def test_max_files_caps_the_number_of_headers():
    files = {f"f{n}.py": f"x = {n}\n" for n in range(10)}
    out = code_package(files, max_files=3)
    assert out.count("### FILE:") == 3
    assert out.count("### FILE:") == len([ln for ln in out.splitlines() if ln.startswith("### FILE:")])


def test_max_files_one_yields_one_header():
    files = {f"f{n}.py": f"x = {n}\n" for n in range(5)}
    assert code_package(files, max_files=1).count("### FILE:") == 1


def test_files_stay_in_sorted_order():
    out = code_package({"z.py": "z = 1\n", "a.py": "a = 1\n", "m.py": "m = 1\n"})
    headers = [ln for ln in out.splitlines() if ln.startswith("### FILE:")]
    assert headers == ["### FILE: a.py", "### FILE: m.py", "### FILE: z.py"]


def test_files_separated_by_a_blank_line():
    out = code_package({"a.py": "x = 1\n", "b.py": "y = 1\n"})
    assert "\n\n### FILE: b.py" in out


def test_second_file_dropped_when_budget_runs_out_mid_package():
    files = {"a.py": "x = 1\n", "b.py": "y = 1\n"}
    out = code_package(files, max_chars=len(code_package({"a.py": "x = 1\n"})))
    assert "### FILE: a.py" in out
    assert "### FILE: b.py" not in out
    assert len(out) <= len(code_package({"a.py": "x = 1\n"}))


def test_empty_file_renders_header_only():
    out = code_package({"a.py": ""})
    assert out == "### FILE: a.py"


def test_empty_files_package_is_empty_string():
    assert code_package({}) == ""


def test_untruncated_package_has_no_marker():
    out = code_package({"a.py": "x = 1\n"})
    assert "…(truncated)" not in out


def test_every_code_prompt_carries_the_line_number_instruction():
    for name in _CODE_PROMPTS:
        assert _LINE_NUMBER_RULE in _normalized(_prompt(name)), name


def test_instruction_present_in_all_five_prompt_files():
    files = sorted(p.name for p in _PROMPT_DIR.glob("*.md"))
    assert files == [f"{name}.md" for name in _CODE_PROMPTS]
    for filename in files:
        assert _LINE_NUMBER_RULE in _normalized(_prompt(filename[:-3])), filename


def test_cited_snippet_rule_present_in_the_four_line_range_prompts():
    for name in _LINE_RANGE_PROMPTS:
        assert _CITED_SNIPPET_RULE in _normalized(_prompt(name)), name


def test_requirements_prompt_uses_the_evidence_rule_not_cited_snippet():
    # It returns "file:line" evidence entries, so telling it about cited_snippet
    # would name a field it never emits.
    text = _normalized(_prompt("requirements"))
    assert _EVIDENCE_RULE in text
    assert "cited_snippet" not in text
    assert _CITED_SNIPPET_RULE not in text


def test_prompt_version_is_current():
    assert PROMPT_VERSION == "1.3.0"
    assert current_prompt_version() == "1.3.0"


def test_every_prompt_with_line_numbers_also_states_the_line_range_rule():
    """Any prompt told to cite shown line numbers must also be told how line_range
    maps onto the code it quotes (FR-014)."""
    checked = 0
    for path in sorted(_PROMPT_DIR.glob("*.md")):
        text = _normalized(path.read_text(encoding="utf-8"))
        if _LINE_NUMBER_RULE not in text:
            continue
        checked += 1
        assert _COUNTING_RULE in text, path.name
        assert _QUOTE_EXACTLY_RULE in text, path.name
    assert checked == len(_CODE_PROMPTS) == 5


def test_line_range_prompts_state_the_start_and_end_rule():
    # requirements.md returns evidence "file:line" refs rather than a line_range,
    # so its wording is adapted; the four line_range prompts share this sentence.
    for name in _LINE_RANGE_PROMPTS:
        assert _LINE_RANGE_SENTENCE in _normalized(_prompt(name)), name


def test_requirements_prompt_adapts_the_rule_to_evidence_refs():
    # It has no cited_snippet to bound, so it must not claim to have one.
    text = _normalized(_prompt("requirements"))
    assert _LINE_RANGE_SENTENCE not in text
    assert _COUNTING_RULE in text
    assert "evidence \"path/to/file.py:19\"" in text


def test_every_prompt_file_header_matches_prompt_version():
    for name in _CODE_PROMPTS:
        first = _prompt(name).splitlines()[0]
        assert first == f"prompt_version: {PROMPT_VERSION}", name


def test_batched_code_prompts_explain_partial_file_chunks():
    """The four batched review types see chunk headers, so they must know a
    '(lines s-e of n)' header means only that range is visible (FR-029)."""
    for name in _LINE_RANGE_PROMPTS:
        assert _PARTIAL_FILE_RULE in _normalized(_prompt(name)), name


def test_partial_file_rule_follows_the_line_range_instruction():
    # Placing it after the line-range rule keeps the citation rules together and
    # stops a model from reading the chunk header as a line-numbering change.
    for name in _LINE_RANGE_PROMPTS:
        text = _normalized(_prompt(name))
        assert text.index(_LINE_RANGE_SENTENCE) < text.index(_PARTIAL_FILE_RULE), name


def test_requirements_prompt_has_no_partial_file_rule():
    # It still receives code_package, which never shows part of a file, so
    # telling it about chunk headers would describe input it cannot get.
    assert _PARTIAL_FILE_RULE not in _normalized(_prompt("requirements"))
