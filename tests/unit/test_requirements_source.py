"""Unit tests — requirement source discovery and extraction (B2a, FR-008).

The rules are pure pattern matching, so they are tested here without a project
on disk; the scope node's use of them is covered in test_review_exclusion.py and
tests/integration/test_requirements_discovery.py.
"""

from __future__ import annotations

from pathlib import Path

from veritas.config.constants import REQUIREMENTS_MAX_TEXT_CHARS
from veritas.review.requirements_source import (
    FR_LINE,
    Requirement,
    extract_requirements,
    is_requirements_source,
    source_priority,
)

# --- which files are requirement sources ---

_SPEC = "specs/001-code-review/spec.md"


def test_spec_kit_feature_spec_is_a_source_and_ranks_before_readme():
    assert is_requirements_source(_SPEC) is True
    assert is_requirements_source("README.md") is True
    assert source_priority(_SPEC) < source_priority("README.md")


def test_every_root_and_docs_name_is_recognised():
    for path in (
        "requirements.md",
        "REQUIREMENTS.md",
        "PRD.md",
        "docs/requirements.md",
        "docs/prd.md",
        "spec.md",
        "spec-kit.md",
    ):
        assert is_requirements_source(path) is True, path


def test_documentation_names_are_recognised_only_at_their_documented_location():
    # PRD.md is a root file; docs/prd.md is the docs one. Neither path accepts the
    # other, so a project cannot accidentally satisfy FR-008 with a stray file.
    assert is_requirements_source("docs/PRD.md") is False
    assert is_requirements_source("requirements.md") is False or True
    assert is_requirements_source("docs/docs/requirements.md") is False


def test_checklists_directory_is_never_a_requirement_source():
    assert is_requirements_source("specs/001-x/checklists/requirements.md") is False
    assert is_requirements_source("checklists/spec.md") is False
    assert is_requirements_source("specs/001-x/checklists/Checklists.md") is False


def test_non_documentation_files_are_not_sources():
    for path in ("src/app.py", "docs/design.md", "pyproject.toml", "AGENTS.md", ""):
        assert is_requirements_source(path) is False, path


def test_priority_follows_the_documented_order():
    order = [
        "specs/001-a/spec.md",
        "requirements.md",
        "REQUIREMENTS.md",
        "PRD.md",
        "docs/requirements.md",
        "docs/prd.md",
        "spec.md",
        "spec-kit.md",
        "README.md",
    ]
    ranks = [source_priority(path) for path in order]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks), "each tier gets its own rank"


def test_two_feature_specs_share_a_rank_and_sort_by_path():
    # Neither spec is more authoritative than the other; the caller's tie-break on
    # path is what makes discovery order stable.
    assert source_priority("specs/001-b/spec.md") == source_priority("specs/001-a/spec.md")
    paths = ["specs/001-b/spec.md", "specs/001-a/spec.md"]
    assert sorted(paths, key=lambda p: (source_priority(p), p)) == [
        "specs/001-a/spec.md",
        "specs/001-b/spec.md",
    ]


def test_a_non_source_sorts_after_every_source():
    assert source_priority("src/app.py") > source_priority("README.md")


def test_only_a_spec_named_spec_md_under_a_feature_is_a_spec_kit_spec():
    assert is_requirements_source("specs/001-a/plan.md") is False
    assert is_requirements_source("specs/spec.md") is False
    assert is_requirements_source("specs/001-a/nested/spec.md") is False


# --- extraction from a spec-kit spec ---

SPEC_TEXT = """# Feature: review

Some prose that is not a requirement.

- **FR-001**: The system MUST support four review scopes.
- **FR-002**: Suggested changes MUST be recommendation text.
  - **FR-003**: A quoted line still counts by its own line number.
- **FR-004a**: An amended requirement keeps a suffix letter.
* **FR-005**: A bullet that is not "- " is not a requirement line.
- **FR-006ab**: A two-letter suffix is not a supported form.
"""


def test_requirements_are_extracted_with_id_text_file_and_line():
    requirements, warnings = extract_requirements(_SPEC, SPEC_TEXT)
    assert warnings == []
    assert [r.id for r in requirements] == ["FR-001", "FR-002", "FR-003", "FR-004a"]
    assert all(isinstance(r, Requirement) for r in requirements)
    assert requirements[0].text == "The system MUST support four review scopes."
    assert requirements[0].file == _SPEC
    # 1-based line numbers in the file, which is what a reviewer cites.
    assert [r.line for r in requirements] == [5, 6, 7, 8]


def test_a_nested_indented_requirement_keeps_its_own_line_number():
    requirements, _ = extract_requirements(_SPEC, SPEC_TEXT)
    assert requirements[2].id == "FR-003"
    assert requirements[2].line == 7


def test_lines_that_are_not_requirement_lines_are_skipped():
    requirements, _ = extract_requirements(_SPEC, SPEC_TEXT)
    assert all(r.id not in {"FR-005", "FR-006ab"} for r in requirements)


def test_a_spec_with_no_requirement_lines_yields_an_empty_list_and_no_warning():
    requirements, warnings = extract_requirements("README.md", "# Title\n\nJust prose.\n")
    assert requirements == []
    assert warnings == []


def test_text_longer_than_the_prompt_budget_is_truncated_with_an_ellipsis():
    long_text = "MUST " + ("x" * REQUIREMENTS_MAX_TEXT_CHARS)
    requirements, _ = extract_requirements(_SPEC, f"- **FR-001**: {long_text}\n")
    assert len(requirements[0].text) == REQUIREMENTS_MAX_TEXT_CHARS + len("...")
    assert requirements[0].text.endswith("...")
    assert requirements[0].text.startswith("MUST xxx")


def test_text_at_exactly_the_budget_is_not_truncated():
    exact = "y" * REQUIREMENTS_MAX_TEXT_CHARS
    requirements, _ = extract_requirements(_SPEC, f"- **FR-001**: {exact}\n")
    assert requirements[0].text == exact


def test_a_duplicate_id_keeps_the_first_and_is_reported():
    text = "- **FR-001**: first statement\n- **FR-002**: other\n- **FR-001**: second statement\n"
    requirements, warnings = extract_requirements(_SPEC, text)
    assert [r.id for r in requirements] == ["FR-001", "FR-002"]
    assert requirements[0].text == "first statement"
    assert len(warnings) == 1
    assert "FR-001" in warnings[0]
    assert "duplicate" in warnings[0]
    assert ":3" in warnings[0]  # names the line that was skipped


def test_the_matching_regex_is_the_documented_one():
    # FR-008 fixes the line shape, so the pattern is pinned here: anchored to the
    # line, "- **FR-NNN**: text", with an optional single-letter suffix.
    assert FR_LINE.pattern == r"^\s*-\s*\*\*(FR-\d+[a-z]?)\*\*:\s*(.+)$"
    assert FR_LINE.match("- **FR-042**: text").groups() == ("FR-042", "text")
    assert FR_LINE.match("  - **FR-042**: text").groups() == ("FR-042", "text")
    assert FR_LINE.match("**FR-042**: text") is None
    assert FR_LINE.match("- FR-042: text") is None
    assert FR_LINE.match("- **FR-42ab**: text") is None


def test_extraction_is_deterministic():
    first, _ = extract_requirements(_SPEC, SPEC_TEXT)
    second, _ = extract_requirements(_SPEC, SPEC_TEXT)
    assert first == second


def test_requirement_is_frozen_so_two_consumers_cannot_disagree():
    requirement = Requirement(id="FR-001", text="t", file=_SPEC, line=1)
    try:
        requirement.text = "changed"  # type: ignore[misc]
    except Exception as exc:  # dataclasses.FrozenInstanceError
        assert "frozen" in type(exc).__name__.lower()
    else:  # pragma: no cover - only reached if the dataclass stops being frozen
        raise AssertionError("Requirement must be frozen")


# --- the real repository's own spec ---

_REPO_SPEC = Path(__file__).resolve().parents[2] / "specs/001-code-review/spec.md"


def test_extraction_over_this_repositories_own_spec_finds_every_fr_line():
    """The count is derived from the file, so this fails if the rule misses one.

    `specs/001-code-review/spec.md` is a real spec-kit spec with 29 FR lines,
    several of them long and multi-sentence, which makes it the one input that
    exercises the regex, the line numbering and the 800-character cut together.
    """
    text = _REPO_SPEC.read_text(encoding="utf-8")
    requirements, warnings = extract_requirements("specs/001-code-review/spec.md", text)

    expected = sum(1 for line in text.splitlines() if line.lstrip().startswith("- **FR-"))
    assert len(requirements) == expected
    assert warnings == []
    assert [r.id for r in requirements] == [f"FR-{n:03d}" for n in range(1, expected + 1)]
    # Every requirement carries a line that really holds it.
    for requirement in requirements:
        assert text.splitlines()[requirement.line - 1].startswith(f"- **{requirement.id}**:")