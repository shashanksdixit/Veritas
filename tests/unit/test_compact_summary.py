"""Unit tests — compact stdout renderer (T019, FR-016)."""

from veritas.models.entities import (
    Category,
    CodeFinding,
    LineRange,
    Report,
    ReportStatus,
    ReviewRun,
    ReviewScope,
    Severity,
    Summary,
    Verdict,
    FindingSource,
)
from veritas.output.compact import render_compact


def _report() -> Report:
    run = ReviewRun(
        scope=ReviewScope.PROJECT,
        target=".",
        config_hash="abc",
        model_name="m",
        prompt_version="1.0.0",
        report_status=ReportStatus.COMPLETE,
    )
    errors = [
        CodeFinding(
            file=f"src/f{i}.py",
            line_range=LineRange(start_line=1, start_col=1, end_line=1, end_col=1),
            severity=Severity.ERROR,
            category=Category.SECURITY,
            source=FindingSource.SAST,
            title=f"error {i}",
            description="d",
            recommendation="r",
            confidence=1.0,
        )
        for i in range(12)
    ]
    return Report(
        run=run,
        code_findings=errors,
        summary=Summary(
            total_code_findings=12,
            severity_counts={Severity.ERROR: 12},
            category_counts={Category.SECURITY: 12},
            total_requirement_findings=0,
            verification_failure_count=0,
            verdict=Verdict.REQUIRES_MODIFICATION,
        ),
    )


def test_count_line():
    text = render_compact(_report())
    assert "Veritas Review: 12 findings (12 error, 0 warning, 0 info)" in text


def test_verdict_line():
    assert "Verdict: RequiresModification" in render_compact(_report())


def test_error_headline_capped_at_ten():
    text = render_compact(_report())
    assert "Headline findings (errors):" in text
    assert text.count("  - ") == 10
    assert "+2 more, see full report" in text


def test_security_source_breakdown():
    text = render_compact(_report())
    assert "Security sources: 12 sast" in text


def test_incomplete_status_reported():
    report = _report()
    report.run.report_status = ReportStatus.INCOMPLETE
    report.run.error = "LLM call failed for category security"
    text = render_compact(report)
    assert "Report status: incomplete — LLM call failed for category security" in text


def test_report_path_included():
    text = render_compact(_report(), "veritas-report-20260101-000000.md")
    assert "Report: veritas-report-20260101-000000.md" in text