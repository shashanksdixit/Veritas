"""Unit tests — an incomplete report's verdict says it covers only what was reviewed (T140, FR-015, FR-027).

The verdict is derived only from the findings, so a run whose every LLM call
failed derives Clean. The derived value stays in the data; stdout and the
Markdown report qualify it when the report is incomplete, and leave a complete
report's verdict exactly as it was.
"""

import pytest

from veritas.models.entities import (
    Report,
    ReportStatus,
    ReviewRun,
    ReviewScope,
    Summary,
    Verdict,
)
from veritas.output.compact import render_compact
from veritas.output.markdown import render_markdown

QUALIFIER = "(incomplete review: covers only what was reviewed)"
NOTE = (
    "> This report is incomplete (see the report status and error at the top), so the verdict comes only "
    "from what was reviewed. Code that a failed call never reviewed may still have problems: a `Clean` or "
    "`RequiresReview` verdict here is not a clean bill of health, and a `RequiresModification` verdict may "
    "not list every change needed."
)
TRIGGERED_BY = (
    "**Triggered by**: 0 error(s), 0 requirement gap(s), 0 warning(s), 0 partial/unclear requirement(s)"
)


def _report(status: ReportStatus, verdict: Verdict) -> Report:
    run = ReviewRun(
        scope=ReviewScope.PROJECT,
        target=".",
        config_hash="abc",
        model_name="m",
        prompt_version="1.0.0",
        report_status=status,
        error="code-quality: batch 1/1 failed (files: src/app.py): boom"
        if status is ReportStatus.INCOMPLETE
        else None,
    )
    return Report(
        run=run,
        summary=Summary(
            total_code_findings=0,
            total_requirement_findings=0,
            verification_failure_count=0,
            verdict=verdict,
        ),
    )


def _stdout_verdict(report: Report) -> str:
    (line,) = [line for line in render_compact(report).splitlines() if line.startswith("Verdict:")]
    return line


def _markdown_verdict_part(markdown: str) -> str:
    start = markdown.index("## Summary\n") + len("## Summary\n")
    return markdown[start : markdown.index("**What the verdicts mean**")]


@pytest.mark.parametrize("verdict", list(Verdict))
def test_incomplete_report_qualifies_the_stdout_verdict(verdict):
    report = _report(ReportStatus.INCOMPLETE, verdict)

    assert _stdout_verdict(report) == f"Verdict: {verdict.value} {QUALIFIER}"


@pytest.mark.parametrize("verdict", list(Verdict))
def test_complete_report_stdout_verdict_is_unqualified(verdict):
    report = _report(ReportStatus.COMPLETE, verdict)

    assert _stdout_verdict(report) == f"Verdict: {verdict.value}"


@pytest.mark.parametrize("verdict", list(Verdict))
def test_incomplete_report_qualifies_the_markdown_verdict_and_explains_it(verdict):
    markdown = render_markdown(_report(ReportStatus.INCOMPLETE, verdict))

    assert _markdown_verdict_part(markdown) == (
        f"\n**Verdict**: `{verdict.value}` {QUALIFIER}\n\n{TRIGGERED_BY}\n\n{NOTE}\n\n"
    )


@pytest.mark.parametrize("verdict", list(Verdict))
def test_complete_report_markdown_verdict_is_unqualified(verdict):
    markdown = render_markdown(_report(ReportStatus.COMPLETE, verdict))

    assert _markdown_verdict_part(markdown) == f"\n**Verdict**: `{verdict.value}`\n\n{TRIGGERED_BY}\n\n"
    assert "incomplete review" not in markdown


def test_rendering_an_incomplete_report_leaves_its_verdict_unchanged():
    report = _report(ReportStatus.INCOMPLETE, Verdict.CLEAN)

    render_compact(report)
    render_markdown(report)

    assert report.summary.verdict is Verdict.CLEAN
    assert report.model_dump(mode="json")["summary"]["verdict"] == "Clean"


def test_legend_is_the_same_on_complete_and_incomplete_reports():
    def legend(status: ReportStatus) -> str:
        markdown = render_markdown(_report(status, Verdict.CLEAN))
        start = markdown.index("**What the verdicts mean**")
        return markdown[start : markdown.index("\n\n", start + len("**What the verdicts mean**\n\n"))]

    complete = legend(ReportStatus.COMPLETE)

    assert "`RequiresModification`" in complete
    assert "`RequiresReview`" in complete
    assert "`Clean`" in complete
    assert legend(ReportStatus.INCOMPLETE) == complete
