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
    VerificationReasonCode,
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
    assert md.startswith("<!-- veritas-report-schema: 1.3.0 -->")


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
    verdict_line = "**Verdict**: `RequiresReview`\n\n**Triggered by**:"
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
    assert md.index(note_start) < md.index("| Finding ID | File | Lines | Code | Reason |")


def test_verification_note_absent_when_no_failures():
    report = _report()
    report.summary.verification_failures = []
    report.summary.verification_failure_count = 0
    md = render_markdown(report)
    assert "### Verification failures (FR-013)" not in md
    assert "Each proposed finding must cite a file" not in md


def test_verification_note_explains_citation_correction():
    # The note must not claim a mismatch always drops the finding: near misses are
    # corrected and kept (FR-013).
    section = _verification_section(render_markdown(_report_with(_failure())))
    assert (
        "When they don't, Veritas looks for the quoted code nearby: if it appears "
        'exactly once within two lines of the cited range, the citation is corrected '
        'and the finding is kept (shown as "Citation adjusted" on that finding). '
        "Otherwise the finding is dropped and listed here so nothing disappears "
        "silently."
    ) in section
    # The old unconditional wording is gone: the drop is now the "Otherwise" case.
    assert "When they don't, the finding is dropped" not in section


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


def _failure(**overrides) -> VerificationFailure:
    base = {
        "finding_id": "f-9",
        "file": "src/app.py",
        "line_range": LineRange(start_line=10, start_col=1, end_line=12, end_col=1),
        "reason": "cited lines hold different code",
    }
    base.update(overrides)
    return VerificationFailure(**base)


def _report_with(*failures: VerificationFailure) -> Report:
    report = _report()
    report.summary.verification_failures = list(failures)
    report.summary.verification_failure_count = len(failures)
    return report


def _verification_section(md: str) -> str:
    start = md.index("### Verification failures (FR-013)")
    end = md.index("## Code Findings")
    return md[start:end]


def _reason_code_legend_rows(section: str) -> list[str]:
    """Data rows of the "Reason codes" legend table, in render order."""
    assert "**Reason codes**" in section
    table = section[section.index("**Reason codes**"):].split("## ")[0]
    lines = [line for line in table.splitlines() if line.startswith("|")]
    return lines[2:]


def test_old_shape_failure_renders_with_empty_code_cell_and_no_legend_or_details():
    md = render_markdown(_report_with(_failure()))
    section = _verification_section(md)
    assert "| f-9 | src/app.py | 10:12 |  | cited lines hold different code |" in section
    assert "**Reason codes**" not in section
    assert "<details>" not in section
    assert "<summary>" not in section


def test_snippet_found_elsewhere_renders_code_cell_legend_row_and_details():
    failure = _failure(
        reason_code=VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE,
        reason="the snippet is at lines 88-90, not 10-12",
        claimed_snippet="import os",
        actual_snippet="result = compute()",
        found_at_lines=[88, 89, 90],
    )
    section = _verification_section(render_markdown(_report_with(failure)))
    assert "| `snippet_found_elsewhere` |" in section
    assert _reason_code_legend_rows(section) == [
        "| `snippet_found_elsewhere` | The quoted code exists in the file, but not at the cited lines "
        "(often a wrong line number). |"
    ]
    assert "<summary>f-9 — src/app.py:10-12 — snippet_found_elsewhere</summary>" in section
    assert "**Reason**: the snippet is at lines 88-90, not 10-12" in section
    assert "**Found at line(s)**: 88, 89, 90" in section
    assert "**Claimed** (what the reviewer quoted):\n\n```\nimport os\n```" in section
    assert "**Actual** (what is at the cited lines):\n\n```\nresult = compute()\n```" in section
    assert section.index("**Found at line(s)**") < section.index("**Claimed**")


def test_reason_and_found_at_lines_are_separate_paragraphs():
    failure = _failure(
        reason_code=VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE,
        reason="the snippet is at lines 88-90, not 10-12",
        claimed_snippet="import os",
        actual_snippet="result = compute()",
        found_at_lines=[88, 89, 90],
    )
    section = _verification_section(render_markdown(_report_with(failure)))
    assert (
        "**Reason**: the snippet is at lines 88-90, not 10-12\n\n"
        "**Found at line(s)**: 88, 89, 90\n\n"
        "**Claimed** (what the reviewer quoted):\n\n```\nimport os\n```"
    ) in section


def test_snippet_not_found_renders_both_snippets_without_found_at_lines():
    failure = _failure(
        reason_code=VerificationReasonCode.SNIPPET_NOT_FOUND,
        reason="the quoted snippet appears nowhere in the file",
        claimed_snippet="eval(payload)",
        actual_snippet="return None",
    )
    section = _verification_section(render_markdown(_report_with(failure)))
    assert "**Reason**: the quoted snippet appears nowhere in the file" in section
    assert "**Found at line(s)**" not in section
    assert "**Claimed** (what the reviewer quoted):\n\n```\neval(payload)\n```" in section
    assert "**Actual** (what is at the cited lines):\n\n```\nreturn None\n```" in section


def test_evidence_not_confirmed_gets_legend_row_but_no_details():
    failure = _failure(
        finding_id="REQ-1",
        reason="the cited evidence line does not exist",
        reason_code=VerificationReasonCode.EVIDENCE_NOT_CONFIRMED,
    )
    section = _verification_section(render_markdown(_report_with(failure)))
    assert "| `evidence_not_confirmed` | A requirement's cited evidence could not be confirmed. |" in section
    assert "<details>" not in section


def test_snippet_containing_backtick_run_is_fenced_one_longer():
    failure = _failure(
        reason_code=VerificationReasonCode.SNIPPET_NOT_FOUND,
        reason="the quoted snippet appears nowhere in the file",
        claimed_snippet='before\n```\nmid\nafter',
    )
    md = render_markdown(_report_with(failure))
    section = _verification_section(md)
    assert "````\nbefore\n```\nmid\nafter\n````" in section
    assert "\n````\n\n</details>" in section
    assert md.count("````") == 2
    assert md.index("</details>") < md.index("## Code Findings")
    after = md[md.index("</details>") + len("</details>"):]
    assert after.startswith("\n\n## Code Findings\n")
    assert "**Cited snippet**:\n\n```text\nimport os\n```" in after


def test_pipe_in_reason_adds_no_extra_table_column():
    failure = _failure(
        reason_code=VerificationReasonCode.SNIPPET_NOT_FOUND,
        reason="expected `a | b` but found `c | d`",
        claimed_snippet="a | b",
    )
    section = _verification_section(render_markdown(_report_with(failure)))
    header = next(line for line in section.splitlines() if "Finding ID" in line)
    assert header == "| Finding ID | File | Lines | Code | Reason |"
    assert "| expected `a \\| b` but found `c \\| d` |" in section
    assert "````" not in section


def test_html_special_chars_in_finding_id_escaped_in_summary():
    failure = _failure(
        finding_id='<script>alert("x&y")</script>',
        reason_code=VerificationReasonCode.SNIPPET_NOT_FOUND,
        reason="the quoted snippet appears nowhere in the file",
        claimed_snippet="boom()",
    )
    section = _verification_section(render_markdown(_report_with(failure)))
    assert (
        "<summary>&lt;script&gt;alert(\"x&amp;y\")&lt;/script&gt; — "
        "src/app.py:10-12 — snippet_not_found</summary>"
    ) in section
    assert "<script>" not in section[section.index("<details>"):]
    assert '| <script>alert("x&y")</script> | src/app.py | 10:12 |' in section


# ---------------------------------------------------------------------------
# T071 — citation correction is visible in the report (FR-013)
# ---------------------------------------------------------------------------

def _corrected(**overrides) -> CodeFinding:
    """The standard finding with its citation corrected from 240-240 to 239-239."""
    finding = CodeFinding(
        id="f-2",
        file="src/app.py",
        line_range=LineRange(start_line=239, start_col=1, end_line=239, end_col=1),
        severity=Severity.WARNING,
        category=Category.CODE_QUALITY,
        source=FindingSource.LLM_IDENTIFIED,
        title="Unused import",
        description="os is imported but unused.",
        recommendation="Remove it.",
        confidence=0.9,
        cited_snippet="import os",
        citation_adjusted_from=LineRange(start_line=240, start_col=1, end_line=240, end_col=1),
    )
    if overrides:
        finding = finding.model_copy(update=overrides)
    return finding


def _field_rows(md: str) -> list[str]:
    """Data rows of the first code finding's Field/Value table, in render order."""
    table = md[md.index("| Field | Value |"):].split("**Description**")[0]
    return [line for line in table.splitlines() if line.startswith("|")][2:]


def _metrics_rows(md: str) -> list[str]:
    """Data rows of the Summary Metric/Count table, in render order."""
    table = md[md.index("| Metric | Count |"):].split("### Requirement status counts")[0]
    return [line for line in table.splitlines() if line.startswith("|")][2:]


def test_corrected_citation_renders_citation_adjusted_row():
    md = render_markdown(_report(code_findings=[_corrected()]))
    # The heading shows the corrected location, and the row shows the correction.
    assert "### `src/app.py`:239: Unused import *(LLM-verified)*" in md
    assert "| Citation adjusted | from 240-240 to 239-239 |" in md


def test_citation_adjusted_row_is_last_field_row():
    rows = _field_rows(render_markdown(_report(code_findings=[_corrected()])))
    assert len(rows) == 8  # the 7 standard field rows plus the correction row
    assert rows[-1] == "| Citation adjusted | from 240-240 to 239-239 |"
    assert rows[-2].startswith("| Suppressed |")


def test_uncorrected_finding_has_no_citation_adjusted_row():
    md = render_markdown(_report())
    # Scoped to the Field/Value table: the verification-failure note mentions the
    # "Citation adjusted" label by name even with no corrected finding present.
    rows = _field_rows(md)
    assert len(rows) == 7
    assert not [row for row in rows if row.startswith("| Citation adjusted |")]
    assert "from 240-240 to 239-239" not in md


def test_metrics_citations_adjusted_counts_corrected_findings():
    both = render_markdown(_report(code_findings=[_corrected(), _corrected(id="f-3")]))
    assert "| Citations adjusted | 2 |" in both
    none = render_markdown(_report())
    assert "| Citations adjusted | 0 |" in none


def test_metrics_citations_adjusted_rendered_even_with_no_findings():
    md = render_markdown(_report(code_findings=[], requirement_findings=[]))
    assert "| Citations adjusted | 0 |" in md


def test_metrics_citations_adjusted_row_immediately_after_verification_failures():
    labels = [
        row.split("|")[1].strip() for row in _metrics_rows(render_markdown(_report(code_findings=[_corrected()])))
    ]
    assert labels.index("Citations adjusted") == labels.index("Verification failures") + 1


def test_citation_adjusted_value_spans_multiple_lines():
    finding = _corrected(
        line_range=LineRange(start_line=19, start_col=1, end_line=23, end_col=1),
        citation_adjusted_from=LineRange(start_line=19, start_col=1, end_line=19, end_col=1),
    )
    md = render_markdown(_report(code_findings=[finding]))
    assert "| Citation adjusted | from 19-19 to 19-23 |" in md

