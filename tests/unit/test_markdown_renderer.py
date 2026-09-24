"""Unit tests — Markdown renderer contract (T018, FR-016/SC-007)."""

from veritas.models.entities import (
    Category,
    CodeFinding,
    FindingSource,
    LineRange,
    Report,
    ReportStatus,
    RequirementFinding,
    RequirementStatus,
    ReviewRun,
    ReviewScope,
    Severity,
    Summary,
    Verdict,
    VerificationFailure,
)
from veritas.output.markdown import render_markdown


def _report(**overrides) -> Report:
    run = ReviewRun(
        scope=ReviewScope.FILE,
        target="src/app.py",
        config_hash="abc",
        model_name="openai/gpt-4o-mini",
        prompt_version="1.0.0",
        report_status=ReportStatus.COMPLETE,
    )
    finding = override_finding = CodeFinding(
        id="f-1",
        file="src/app.py",
        line_range=LineRange(start_line=2, start_col=1, end_line=2, end_col=18),
        severity=Severity.WARNING,
        category=Category.CODE_QUALITY,
        source=FindingSource.LLM_IDENTIFIED,
        title="Unused import",
        description="os is imported but unused.",
        recommendation="Remove it.",
        confidence=0.9,
        cited_snippet="import os",
    )
    req = RequirementFinding(
        id="r-1",
        requirement_ref="REQ-1",
        requirement_text="Supports project scope.",
        status=RequirementStatus.PARTIAL,
        evidence=["src/app.py:2"],
        explanation="mostly there",
    )
    vf = VerificationFailure(
        finding_id="gone",
        file="src/missing.py",
        line_range=LineRange(start_line=1, start_col=1, end_line=1, end_col=1),
        reason="file not found in reviewed scope",
    )
    report = Report(
        run=run,
        code_findings=[finding],
        requirement_findings=[req],
        summary=Summary(
            total_code_findings=1,
            severity_counts={Severity.WARNING: 1},
            category_counts={Category.CODE_QUALITY: 1},
            total_requirement_findings=1,
            requirement_status_counts={RequirementStatus.PARTIAL: 1},
            verification_failure_count=1,
            verification_failures=[vf],
            verdict=Verdict.REQUIRES_REVIEW,
        ),
    )
    if overrides:
        report = report.model_copy(update=overrides)
    return report


def test_schema_version_comment_top():
    md = render_markdown(_report())
    assert md.startswith("<!-- veritas-report-schema: 1.0.0 -->")


def test_sections_present():
    md = render_markdown(_report())
    for heading in ("# Veritas Code Review", "## Summary", "## Code Findings", "## Requirement Findings"):
        assert heading in md


def test_source_label_sast():
    report = _report()
    report.code_findings[0].source = FindingSource.SAST
    md = render_markdown(report)
    assert "*(SAST)*" in md
    assert "*(LLM-verified)*" not in md


def test_unsuppressed_flag_rendered():
    md = render_markdown(_report())
    assert "| no |" in md


def test_suppressed_flag_rendered():
    report = _report()
    report.code_findings[0].is_suppressed = True
    md = render_markdown(report)
    assert "| yes |" in md


def test_sets_report_markdown_content():
    report = _report()
    md = render_markdown(report)
    assert report.markdown_content == md


def test_verification_failures_tabulated():
    md = render_markdown(_report())
    assert "### Verification failures (FR-013)" in md
    assert "src/missing.py" in md


def test_evidence_rendered():
    md = render_markdown(_report())
    assert "- `src/app.py:2`" in md


def test_no_findings_path():
    report = _report(code_findings=[], requirement_findings=[])
    md = render_markdown(report)
    assert "No code findings." in md
    assert "No requirement findings." in md


def test_pipe_escaped_in_cells():
    report = _report()
    report.summary.verification_failures[0].reason = "multi | pipe"
    md = render_markdown(report)
    assert "| multi \\| pipe |" in md