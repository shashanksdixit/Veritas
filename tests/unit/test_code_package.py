"""Unit tests — line-numbered, line-boundary-truncated code_package (T068, FR-014).

The package is what every review prompt sees, so two properties are load
bearing: a reviewer can copy a citation line number straight out of it, and
the whole string fits the caller's character budget no matter how small that
budget is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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

# Only the test-coverage review is given the scope's test index (T082, FR-004):
# the other review types neither receive nor are told about it.
_TEST_INDEX_RULE = (
    'The user message may include a "Test index" listing test files and test '
    "names that exist in the reviewed scope, including tests that are not shown "
    "in this batch. Before reporting missing or insufficient tests, check the "
    "index, and do not report missing tests for behaviour that an indexed test "
    "name plausibly covers. Tests outside the reviewed scope may also exist, so "
    'describe a gap as "no test found in the reviewed scope" rather than '
    "asserting that no test exists."
)

# An index cut at its character limit ends by saying how many test files were not
# listed, so the reviewer has to know the list is partial (T082, FR-004).
_TRUNCATED_INDEX_RULE = (
    "If the index says that some test files were not listed, a test name missing "
    "from the index is not evidence that the test does not exist."
)

# FR-014: the four prompts that grade code are told what each severity means, where
# its ceiling is, and what is not a finding at all. requirements.md is exempt: its
# output has no severity field, so a rubric would grade something it never emits.
_RUBRIC_PROMPTS = ("code_quality", "performance", "security", "test_coverage")
_RUBRIC_HEADER = "Severity rubric (apply strictly):"
_RUBRIC_ERROR = (
    "error: a likely defect in production code that causes incorrect results, a "
    "crash, data loss, or an exploitable security vulnerability with a plausible "
    "path for attacker-controlled input."
)
_RUBRIC_WARNING = (
    "warning: a real risk or maintainability problem worth fixing that is not "
    "shown to be broken."
)
_RUBRIC_INFO = (
    "info: a minor improvement, such as style, naming, docstrings, or type-hint "
    "conventions."
)
_RUBRIC_LIMITS = (
    "A finding in a test file (a test, fixture, or fake) is info, unless it makes "
    "a test incorrect, such as an assertion that can never fail; then it is "
    "warning. A performance finding is error only for a complexity problem on a "
    "code path whose input can realistically be large; otherwise it is warning or "
    "info."
)
_RUBRIC_DO_NOT_REPORT = (
    "Do not report: that code is acceptable or needs no change; a preference for "
    "an older idiom over a valid modern one (for example Optional[str] instead "
    "of str | None); a security issue with no plausible attack path (for example "
    "authorization checks in a single-user command-line tool, or placeholder keys "
    "in test fixtures)."
)
# The security prompt states the same rubric as prose rather than bullets, so its
# definitions are named without the "- severity:" lead-in the others use.
_PROSE_RUBRIC_ERROR = (
    "Severity rubric (apply strictly): error is a likely defect in production "
    "code that causes incorrect results, a crash, data loss, or an exploitable "
    "security vulnerability with a plausible path for attacker-controlled input."
)
_PROSE_RUBRIC_WARNING = (
    "warning is a real risk or maintainability problem worth fixing that is not "
    "shown to be broken."
)
_PROSE_RUBRIC_INFO = (
    "info is a minor improvement, such as style, naming, docstrings, or type-hint "
    "conventions."
)

# FR-004: the cap is enforced in code, but the prompt says so too, so the reviewer
# spends its budget on what a warning-severity test finding should say.
_NEVER_ERROR = "Test-coverage findings are warning or info, never error."

# FR-014: verification only confirms a citation it can find verbatim, so the four
# prompts that quote code must say what a verbatim quote is. requirements.md is
# exempt: it returns "file:line" evidence refs and no cited_snippet to copy.
_CONTIGUOUS_SNIPPET_RULE = (
    "Copy cited_snippet as one contiguous block of the file, exactly as written, "
    "at most 8 lines, including every line in between. Never skip lines, insert "
    '"..." or comments, join strings, or reformat code.'
)


def _prompt(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    """Collapse the prompt's hard line wrapping so assertions can use one line."""
    return " ".join(text.split())


def _plain(text: str) -> str:
    """Collapse hard wrapping and drop `backtick` styling around field names.

    The rule is written as `Copy `cited_snippet` as ...` in the prompts that mark
    field names as code and as plain `cited_snippet` in security.md, which is
    prose throughout; the requirement is on the words, not the markup.
    """
    return _normalized(text).replace("`", "")


def _rubric_text(text: str) -> str:
    """Flatten wrapping, list markers and case for rubric assertions.

    Three prompts state the rubric as bullets and security.md states the same
    content as prose, so a fragment has to be compared without the bullets, the
    wrapping, or the sentence-initial capital that the two styles differ on.
    """
    return " ".join(_normalized(text).lower().replace("- ", " ").split())


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
    assert PROMPT_VERSION == "1.7.0"
    assert current_prompt_version() == "1.7.0"


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


def test_test_coverage_prompt_explains_the_test_index():
    assert _TEST_INDEX_RULE in _normalized(_prompt("test_coverage"))


def test_test_index_rule_is_in_the_test_coverage_prompt_only():
    # The other four review types are never handed an index, so naming one would
    # describe input they cannot get and invite them to hedge on missing tests.
    for name in _CODE_PROMPTS:
        if name == "test_coverage":
            continue
        assert _TEST_INDEX_RULE not in _normalized(_prompt(name)), name


def test_test_coverage_prompt_explains_a_truncated_index():
    # An index that ran out of room says so; without this the reviewer would read
    # the absence of a test name as the absence of the test (FR-004).
    assert _TRUNCATED_INDEX_RULE in _normalized(_prompt("test_coverage"))


def test_truncated_index_rule_is_in_the_test_coverage_prompt_only():
    for name in _CODE_PROMPTS:
        if name == "test_coverage":
            continue
        assert _TRUNCATED_INDEX_RULE not in _normalized(_prompt(name)), name


# --- FR-014 severity rubric, and the FR-004 cap it is read with ---


@pytest.mark.parametrize(
    ("name", "error", "warning", "info"),
    [
        ("code_quality", _RUBRIC_ERROR, _RUBRIC_WARNING, _RUBRIC_INFO),
        ("performance", _RUBRIC_ERROR, _RUBRIC_WARNING, _RUBRIC_INFO),
        ("test_coverage", _RUBRIC_ERROR, _RUBRIC_WARNING, _RUBRIC_INFO),
        ("security", _PROSE_RUBRIC_ERROR, _PROSE_RUBRIC_WARNING, _PROSE_RUBRIC_INFO),
    ],
)
def test_every_code_prompt_defines_all_three_severities(name, error, warning, info):
    # Each severity is defined, not merely listed: "error" and "warning" are the
    # two that decide the run's verdict, so the model has to know what earns them.
    text = _rubric_text(_prompt(name))
    assert _rubric_text(_RUBRIC_HEADER) in text, name
    for fragment in (
        error,
        warning,
        info,
        _RUBRIC_LIMITS,
        _RUBRIC_DO_NOT_REPORT,
    ):
        assert _rubric_text(fragment) in text, f"{name}: {fragment[:40]}"


def test_the_severity_rubric_is_in_the_four_code_prompts_only():
    # requirements.md has no severity in its output, so a rubric there would grade
    # a field the review never returns.
    assert set(_RUBRIC_PROMPTS) == set(_CODE_PROMPTS) - {"requirements"}
    for name in _CODE_PROMPTS:
        present = _RUBRIC_HEADER in _normalized(_prompt(name))
        assert present is (name != "requirements"), name


def test_the_rubric_follows_the_instructions_it_qualifies():
    # It grades the instructions above it, so it has to come after them and before
    # the JSON schema that follows it.
    for name in _RUBRIC_PROMPTS:
        text = _normalized(_prompt(name))
        assert text.index(_PARTIAL_FILE_RULE) < text.index(_RUBRIC_HEADER), name
        assert text.index(_RUBRIC_HEADER) < text.index("Respond with a single JSON array"), name


def test_only_the_test_coverage_prompt_is_told_never_error():
    for name in _CODE_PROMPTS:
        present = _NEVER_ERROR in _normalized(_prompt(name))
        assert present is (name == "test_coverage"), name


def test_the_never_error_rule_sits_before_the_rubric_it_constrains():
    text = _normalized(_prompt("test_coverage"))
    assert text.index(_NEVER_ERROR) < text.index(_RUBRIC_HEADER)


# --- FR-014: quoted code must be verbatim and contiguous ---


def test_the_contiguous_snippet_rule_is_in_the_four_line_range_prompts():
    for name in _LINE_RANGE_PROMPTS:
        assert _CONTIGUOUS_SNIPPET_RULE in _plain(_prompt(name)), name


def test_the_requirements_prompt_is_not_asked_to_copy_a_snippet():
    # Its output schema has no cited_snippet; asking for one would describe a field
    # the requirements review never returns.
    assert _CONTIGUOUS_SNIPPET_RULE not in _plain(_prompt("requirements"))


def test_the_contiguous_snippet_rule_appears_once_per_prompt():
    for name in _LINE_RANGE_PROMPTS:
        assert _plain(_prompt(name)).count(_CONTIGUOUS_SNIPPET_RULE) == 1, name


def test_the_contiguous_snippet_rule_follows_the_citation_instructions():
    # It refines how to copy the snippet, so it belongs with the other citation
    # rules: after the line-range rule, before the JSON schema.
    for name in _LINE_RANGE_PROMPTS:
        text = _plain(_prompt(name))
        assert text.index(_LINE_RANGE_SENTENCE) < text.index(_CONTIGUOUS_SNIPPET_RULE), name
        assert text.index(_CONTIGUOUS_SNIPPET_RULE) < text.index(
            "Respond with a single JSON array"
        ), name
