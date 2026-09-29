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


def test_triggered_by_line_counts_from_summary():
    md = render_markdown(_report())
    assert "**Triggered by**: 0 error(s), 0 requirement gap(s), 1 warning(s), 1 partial/unclear requirement(s)" in md


def test_triggered_by_line_placed_immediately_after_verdict():
    md = render_markdown(_report())
    verdict_line = "**Verdict**: `RequiresReview`\n**Triggered by**:"
    assert verdict_line in md


def test_triggered_by_line_reflects_error_and_gap_counts():
    report = _report()
    report.summary.severity_counts = {Severity.ERROR: 2, Severity.WARNING: 5}
    report.summary.requirement_status_counts = {
        RequirementStatus.GAP: 3,
        RequirementStatus.PARTIAL: 4,
        RequirementStatus.UNCLEAR: 1,
    }
    md = render_markdown(report)
    assert "**Triggered by**: 2 error(s), 3 requirement gap(s), 5 warning(s), 5 partial/unclear requirement(s)" in md


def test_triggered_by_line_present_in_every_report():
    report = _report(code_findings=[], requirement_findings=[])
    report.summary = Summary(
        total_code_findings=0,
        total_requirement_findings=0,
        verification_failure_count=0,
        verdict=Verdict.CLEAN,
    )
    md = render_markdown(report)
    assert "**Triggered by**: 0 error(s), 0 requirement gap(s), 0 warning(s), 0 partial/unclear requirement(s)" in md


def test_verdict_legend_rendered_in_every_report():
    for report in (
        _report(),
        _report(code_findings=[], requirement_findings=[]),
    ):
        md = render_markdown(report)
        assert "**What the verdicts mean**" in md
        assert "| Verdict | Meaning |" in md
        assert "`RequiresModification` | At least one error-severity finding or one requirement gap." in md
        assert "`RequiresReview` | No errors or gaps, but at least one warning-severity finding" in md
        assert "`Clean` | No errors, warnings, gaps, or partial/unclear requirements." in md


def test_verification_note_present_when_failures_exist():
    md = render_markdown(_report())
    heading = "### Verification failures (FR-013)\n\n"
    note_start = "> Each proposed finding must cite a file, line range, and the exact code snippet it refers to."
    assert heading + note_start in md
    assert "it means the reviewer's claim could not be confirmed." in md
    assert md.index(note_start) < md.index("| Finding ID | File | Lines | Reason |")


def test_verification_note_absent_when_no_failures():
    report = _report()
    report.summary.verification_failures = []
    report.summary.verification_failure_count = 0
    md = render_markdown(report)
    assert "### Verification failures (FR-013)" not in md
    assert "Each proposed finding must cite a file" not in md


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