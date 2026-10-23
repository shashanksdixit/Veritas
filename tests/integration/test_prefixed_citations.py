"""Integration tests — cited snippets copied with the prompts' line numbers.

Review code is shown as ``{n:>5}| {line}`` (FR-014), so a model asked to quote
the offending lines often quotes them as displayed. Before T080 that prefix made
a perfectly real citation fail verification, and the finding was reported as a
verification failure instead of shown.

These run the whole graph with a fake LLM, because the point is not the helper:
it is that the cleaned snippet is what reaches verification, the report and the
suppression fingerprint, and that the prefixes themselves are never trusted as
a location.
"""

from __future__ import annotations

import json
from pathlib import Path

from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.models.entities import Report, ReviewScope
from veritas.review.graph import run_review
from veritas.suppression.fingerprint import compute_fingerprint

_FILE = "src/app.py"
_START_LINE = 21
_END_LINE = 22

# The real code at lines 21-22 of the project below, as written in the file...
_REAL_CODE = "def f():\n    return 1"
# ...and as the reviewer quoted it back: the prompt's line-number prefixes.
_PREFIXED = "   21| def f():\n   22|     return 1"
# Prefix numbers the reviewer got wrong; the code and the cited range are right.
_WRONG_NUMBERS = "   99| def f():\n  100|     return 1"


def _project(tmp_path: Path) -> Path:
    """A file whose lines 21-22 are exactly the code the fake LLM cites."""
    lines = [f"value_{n:02d} = {n}" for n in range(1, _START_LINE)]
    lines += _REAL_CODE.splitlines()
    lines += [f"tail_{n:02d} = {n}" for n in range(1, 4)]
    target = tmp_path / _FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


class _CannedLLM:
    """Answers every code-review batch with one finding citing ``snippet``.

    Requirements traceability gets ``[]`` so the assertions below are about the
    code findings only.
    """

    model_name = "canned-model"

    def __init__(self, snippet: str) -> None:
        self.snippet = snippet

    def complete(self, system: str, user: str) -> str:
        if "requirements-traceability" in system.lower():
            return "[]"
        return json.dumps(
            [
                {
                    "id": "cf-prefixed",
                    "file": _FILE,
                    "start_line": _START_LINE,
                    "start_col": 1,
                    "end_line": _END_LINE,
                    "end_col": 1,
                    "severity": "warning",
                    "title": f"Unused value at {_FILE}:{_START_LINE}",
                    "description": "The returned value is never used.",
                    "recommendation": "Return something meaningful.",
                    "confidence": 0.9,
                    "cited_snippet": self.snippet,
                }
            ]
        )


def _run(tmp_path: Path, snippet: str) -> tuple[object, Report]:
    """Run the whole pipeline over a project whose lines 21-22 the fake cites."""
    project = _project(tmp_path)
    outcome = run_review(
        Settings(api_key="test-key"),
        ReviewScope.PROJECT,
        str(project),
        output=str(tmp_path / "report.md"),
        llm=_CannedLLM(snippet),
    )
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    return (outcome, report)


def test_prefixed_snippet_of_real_code_is_kept_and_stored_clean(tmp_path, capsys):
    """A citation quoted as displayed survives verification, stored prefix-free."""
    outcome, report = _run(tmp_path, _PREFIXED)
    assert outcome.exit_code == 0

    # Nothing was rejected for the quoting: the code really is at 21-22.
    assert report.summary.verification_failures == []
    kept = [f for f in report.code_findings if f.file == _FILE]
    # One per code review type, none dropped and none invented.
    assert len(kept) == 4
    for finding in kept:
        assert finding.cited_snippet == _REAL_CODE
        assert (finding.line_range.start_line, finding.line_range.end_line) == (
            _START_LINE,
            _END_LINE,
        )
        # Verified where it was cited, so no near-miss correction was needed.
        assert finding.citation_adjusted_from is None

    # Fingerprinted as the clean code, not as it was quoted: this is the key a
    # suppression is stored under, so it must not depend on the quoting style.
    category = kept[0].category.value
    for finding in kept:
        assert compute_fingerprint(finding.file, category, finding.cited_snippet) == (
            compute_fingerprint(finding.file, category, _REAL_CODE)
        )
    assert compute_fingerprint(_FILE, category, _REAL_CODE) != compute_fingerprint(
        _FILE, category, _PREFIXED
    )

    # The prefixes are gone from the rendered report, not just from state.
    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert "21| def f():" not in markdown
    assert "def f():" in markdown

    # Every removal is logged, with how many snippets in that batch were cleaned.
    removed = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "removed line-number prefixes" in line
    ]
    assert removed, "no prefix-stripping line was logged"
    for line in removed:
        assert line.startswith("[info] ")
        assert line.endswith(": removed line-number prefixes from 1 snippet(s)")
    for name in ("code_quality", "security", "test_coverage", "performance"):
        assert any(f"{name}: batch " in line for line in removed), name


def test_prefix_numbers_are_ignored_when_the_code_is_real(tmp_path):
    """Prefixes 99/100 over real lines 21-22: the numbers are not the location.

    The cited line range decides, so a reviewer who miscounted while quoting is
    still correct about where the code is — and must not have the citation
    rewritten to the prefix numbers.
    """
    _outcome, report = _run(tmp_path, _WRONG_NUMBERS)

    assert report.summary.verification_failures == []
    kept = [f for f in report.code_findings if f.file == _FILE]
    assert len(kept) == 4
    for finding in kept:
        assert finding.cited_snippet == _REAL_CODE
        assert (finding.line_range.start_line, finding.line_range.end_line) == (
            _START_LINE,
            _END_LINE,
        )
        assert finding.citation_adjusted_from is None