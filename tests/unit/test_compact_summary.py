"""Unit tests — compact stdout renderer (T019, FR-016)."""

from veritas.models.entities import (
    Category,
    CodeFinding,
    Coverage,
    ExcludedFile,
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


# --- coverage line (T077, FR-029) ---


def _coverage(**overrides) -> Coverage:
    base = {
        "batch_chars": 40000,
        "max_batches": 8,
        "batches_used": 3,
        "reviewed_files": ["a.py", "b.py", "c.py"],
        "split_files": ["big.py"],
        "excluded_files": [
            ExcludedFile(path="vendor/lib.py", pattern="vendor/"),
            ExcludedFile(path="dist/out.js", pattern="dist/"),
        ],
        "not_reviewed_files": ["huge.py"],
    }
    base.update(overrides)
    return Coverage(**base)


def test_no_coverage_line_when_coverage_is_none():
    text = render_compact(_report())
    assert "Coverage:" not in text


def test_coverage_line_text_is_exact():
    report = _report().model_copy(update={"coverage": _coverage()})
    lines = render_compact(report).splitlines()
    assert (
        "Coverage: 3 reviewed (1 split), 2 excluded, 1 not reviewed; 3/8 batches" in lines
    )
    # Directly after the category line.
    assert lines.index("Security: 12") + 1 == lines.index(
        "Coverage: 3 reviewed (1 split), 2 excluded, 1 not reviewed; 3/8 batches"
    )


def test_coverage_line_is_ascii_only():
    report = _report().model_copy(update={"coverage": _coverage()})
    line = next(l for l in render_compact(report).splitlines() if l.startswith("Coverage:"))
    assert line.isascii()


def test_coverage_line_counts_an_empty_run():
    report = _report().model_copy(
        update={
            "coverage": _coverage(
                reviewed_files=[],
                split_files=[],
                excluded_files=[],
                not_reviewed_files=[],
                batches_used=0,
            )
        }
    )
    assert "Coverage: 0 reviewed (0 split), 0 excluded, 0 not reviewed; 0/8 batches" in render_compact(
        report
    )