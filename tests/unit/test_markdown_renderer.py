"""Unit tests — Markdown renderer contract (T018, FR-016/SC-007)."""

from veritas.models.entities import (
    Category,
    CodeFinding,
    Coverage,
    ExcludedFile,
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
    assert md.startswith("<!-- veritas-report-schema: 1.7.0 -->")


def test_sections_present():
    md = render_markdown(_report())
    for heading in ("# Veritas Code Review", "## Summary", "## Code Findings", "## Requirement Findings"):
        assert heading in md


def test_source_label_sast():
    report = _report()
    report.code_findings[0].source = FindingSource.SAST
    md = render_markdown(report)
    assert "*(SAST)*" in md
    assert "*(LLM-identified, citation verified)*" not in md


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


# --- not_addressed is listed compactly, not as a section each (FR-007) ---


def _not_addressed(ref: str, text: str = "The system MUST log to stdout.") -> RequirementFinding:
    return RequirementFinding(
        id=f"r-{ref}",
        requirement_ref=ref,
        requirement_text=text,
        status=RequirementStatus.NOT_ADDRESSED,
        evidence=[],
        explanation="No code for this requirement is part of this PR.",
    )


def _requirement_section(md: str) -> str:
    """The body of the ``## Requirement Findings`` section, up to the next ``## ``."""
    start = md.index("## Requirement Findings")
    rest = md[start + len("## Requirement Findings") :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_not_addressed_gets_one_compact_block_after_the_other_requirements():
    md = render_markdown(
        _report(requirement_findings=[_report_requirement(), _not_addressed("FR-002")])
    )
    section = _requirement_section(md)
    assert "1 requirement(s) are not addressed by this PR:" in section
    # The compact list carries the id and the text, not a heading per requirement.
    assert "- FR-002: The system MUST log to stdout." in section
    assert "### FR-002" not in section
    # The full section for the requirement the PR does address is untouched.
    assert "### REQ-1" in section


def test_the_compact_block_is_collapsible_with_blank_lines_inside():
    md = render_markdown(_report(requirement_findings=[_not_addressed("FR-002")]))
    section = _requirement_section(md)
    assert (
        "<details>\n<summary>Not addressed (1)</summary>\n\n"
        "- FR-002: The system MUST log to stdout.\n\n</details>"
    ) in section


def test_a_not_addressed_requirement_text_is_cut_to_100_characters():
    long_text = "MUST " + "x" * 300
    md = render_markdown(
        _report(requirement_findings=[_not_addressed("FR-002", text=long_text)])
    )
    assert "- FR-002: " + long_text[:100] in md
    assert long_text not in md


def test_the_compact_block_says_nothing_when_nothing_is_not_addressed():
    md = render_markdown(_report(requirement_findings=[_report_requirement()]))
    assert "not addressed by this PR" not in md
    assert "<details>" not in md


def test_every_requirement_appears_exactly_once_with_mixed_statuses():
    findings = [
        _report_requirement(),
        _not_addressed("FR-002"),
        _not_addressed("FR-003"),
    ]
    md = render_markdown(_report(requirement_findings=findings))
    section = _requirement_section(md)
    assert "2 requirement(s) are not addressed by this PR:" in section
    # One heading for the addressed requirement, one list entry for each other, so
    # every requirement is present exactly once and none is in both places.
    assert section.count("### REQ-1") == 1
    assert section.count("- FR-002:") == 1
    assert section.count("- FR-003:") == 1
    # The compact list is id and text only: the per-finding explanation is not
    # repeated once per untouched requirement.
    assert section.count("No code for this requirement is part of this PR.") == 0


def test_the_status_counts_table_lists_not_addressed():
    report = _report(requirement_findings=[_report_requirement(), _not_addressed("FR-002")])
    report.summary.requirement_status_counts = {
        RequirementStatus.PARTIAL: 1,
        RequirementStatus.NOT_ADDRESSED: 1,
    }
    md = render_markdown(report)
    assert "| Requirement status: not_addressed | 1 |" in md


def _report_requirement() -> RequirementFinding:
    return RequirementFinding(
        id="r-1",
        requirement_ref="REQ-1",
        requirement_text="Supports project scope.",
        status=RequirementStatus.PARTIAL,
        evidence=["src/app.py:2"],
        explanation="mostly there",
    )


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
    assert "### `src/app.py`:239: Unused import *(LLM-identified, citation verified)*" in md
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


# --- FR-004: the capped severity is shown, not just applied ---


def _capped(**overrides) -> CodeFinding:
    """A test-coverage finding the node lowered from error to warning."""
    base = {
        "id": "f-7",
        "file": "src/app.py",
        "line_range": LineRange(start_line=2, start_col=1, end_line=2, end_col=18),
        "severity": Severity.WARNING,
        "severity_adjusted_from": Severity.ERROR,
        "category": Category.TEST_COVERAGE,
        "title": "No test found for the retry path",
        "description": "The retry branch is untested.",
        "recommendation": "Add a test for the retry path.",
        "confidence": 0.7,
        "cited_snippet": "import os",
    }
    base.update(overrides)
    return CodeFinding(**base)


def test_capped_finding_renders_severity_adjusted_row():
    md = render_markdown(_report(code_findings=[_capped()]))
    assert (
        "| Severity adjusted | from \N{LARGE RED CIRCLE} error to "
        "\N{LARGE ORANGE CIRCLE} warning (test-coverage findings are capped at warning) |"
    ) in md


def test_severity_adjusted_row_sits_directly_after_severity():
    rows = _field_rows(render_markdown(_report(code_findings=[_capped()])))
    labels = [row.split("|")[1].strip() for row in rows]
    assert labels[0] == "Severity"
    assert labels[1] == "Severity adjusted"
    assert labels[2] == "Category"
    assert len(rows) == 8, "the 7 standard field rows plus the cap row"


def test_uncapped_finding_has_no_severity_adjusted_row():
    rows = _field_rows(render_markdown(_report()))
    assert len(rows) == 7
    assert not [row for row in rows if row.startswith("| Severity adjusted |")]
    assert "test-coverage findings are capped at warning" not in render_markdown(_report())


def test_the_severity_row_still_shows_the_severity_the_finding_carries():
    rows = _field_rows(render_markdown(_report(code_findings=[_capped()])))
    assert rows[0] == "| Severity | \N{LARGE ORANGE CIRCLE} warning |"


def test_an_uncapped_warning_gets_no_row_because_nothing_was_lowered():
    # Same severity, no cap: the row must not appear, or every warning in the report
    # would claim to have been capped.
    rows = _field_rows(render_markdown(_report(code_findings=[_capped(severity_adjusted_from=None)])))
    assert len(rows) == 7
    assert not [row for row in rows if row.startswith("| Severity adjusted |")]


# --- Coverage subsection (T077, FR-029) ---


def _coverage(**overrides) -> Coverage:
    base = {
        "batch_chars": 40000,
        "max_batches": 8,
        "batches_used": 3,
        "reviewed_files": ["src/app.py", "src/util.py", "tests/test_app.py"],
        "split_files": ["src/big.py"],
        "excluded_files": [ExcludedFile(path="vendor/lib.py", pattern="vendor/")],
        "not_reviewed_files": ["src/huge.py"],
    }
    base.update(overrides)
    return Coverage(**base)


def _section(md: str, heading: str) -> str:
    """The body of one ``### `` section, up to the next heading of any level."""
    body = md.split(f"{heading}\n", 1)[1]
    for next_heading in ("\n### ", "\n## "):
        body = body.split(next_heading, 1)[0]
    return body


def test_no_coverage_section_when_coverage_is_none():
    md = render_markdown(_report())
    assert "### Coverage" not in md
    assert "Files reviewed" not in md
    assert "reviewed files (" not in md.lower()


def test_coverage_renders_plain_language_sentence():
    md = render_markdown(_report(coverage=_coverage(batch_chars=40000, max_batches=8)))
    assert (
        "Code is reviewed in batches of up to 40000 characters, at most 8 batches "
        "per review type, with application code before test files. Files matching "
        "an exclusion pattern are not reviewed." in md
    )


def test_coverage_renders_counts_table():
    coverage = _coverage(
        reviewed_files=["a.py", "b.py", "c.py"],
        split_files=["big.py"],
        excluded_files=[
            ExcludedFile(path="vendor/lib.py", pattern="vendor/"),
            ExcludedFile(path="dist/out.js", pattern="dist/"),
        ],
        not_reviewed_files=["huge.py"],
        batches_used=5,
        max_batches=8,
    )
    section = _section(render_markdown(_report(coverage=coverage)), "### Coverage (FR-029)")
    assert "| Files reviewed | 3 |" in section
    assert "| Files split across batches | 1 |" in section
    assert "| Files excluded | 2 |" in section
    assert "| Files not reviewed (batch limit) | 1 |" in section
    assert "| Batches used | 5 of 8 |" in section


def test_coverage_details_block_for_each_non_empty_list():
    coverage = _coverage(
        reviewed_files=["a.py", "b.py", "c.py"],
        split_files=["big.py"],
        excluded_files=[ExcludedFile(path="vendor/lib.py", pattern="vendor/")],
        not_reviewed_files=["huge.py"],
    )
    section = _section(render_markdown(_report(coverage=coverage)), "### Coverage (FR-029)")
    assert section.count("<details>") == 4
    # One block per list, in the documented order.
    for label, count in (
        ("Reviewed files", 3),
        ("Split files", 1),
        ("Excluded files", 1),
        ("Not reviewed files", 1),
    ):
        assert f"<summary>{label} ({count})</summary>" in section
    labels = [line for line in section.splitlines() if line.startswith("<summary>")]
    assert [line.split(" (")[0] for line in labels] == [
        "<summary>Reviewed files",
        "<summary>Split files",
        "<summary>Excluded files",
        "<summary>Not reviewed files",
    ]
    # Every path appears, in backticks.
    for path in ("a.py", "b.py", "c.py", "big.py", "vendor/lib.py", "huge.py"):
        assert f"- `{path}`" in section


def test_coverage_details_blocks_have_blank_lines_inside_and_before_close():
    section = _section(
        render_markdown(_report(coverage=_coverage(reviewed_files=["a.py"]))),
        "### Coverage (FR-029)",
    )
    assert (
        "<details>\n<summary>Reviewed files (1)</summary>\n\n- `a.py`\n\n</details>" in section
    )


def test_coverage_omits_details_block_for_an_empty_list():
    section = _section(
        render_markdown(
            _report(
                coverage=_coverage(
                    split_files=[],
                    not_reviewed_files=[],
                )
            )
        ),
        "### Coverage (FR-029)",
    )
    assert section.count("<details>") == 2
    assert "<summary>Reviewed files (3)</summary>" in section
    assert "<summary>Excluded files (1)</summary>" in section
    assert "Split files" not in section
    assert "Not reviewed files" not in section
    # The counts table still accounts for them.
    assert "| Files split across batches | 0 |" in section
    assert "| Files not reviewed (batch limit) | 0 |" in section


def test_coverage_not_reviewed_warning_is_visible_outside_details():
    section = _section(
        render_markdown(_report(coverage=_coverage(not_reviewed_files=["huge.py", "more.py"]))),
        "### Coverage (FR-029)",
    )
    warning = (
        "**2 file(s) were not reviewed** because the batch limit was reached. "
        "Raise `max_batches` or `batch_chars` in the `[review]` config section "
        "to include them."
    )
    assert warning in section
    # Visible, not hidden: the warning precedes the first collapsed block.
    assert section.index(warning) < section.index("<details>")


def test_coverage_not_reviewed_warning_absent_when_nothing_was_withheld():
    section = _section(
        render_markdown(_report(coverage=_coverage(not_reviewed_files=[]))),
        "### Coverage (FR-029)",
    )
    assert "were not reviewed**" not in section
    assert "batch limit was reached" not in section


def test_coverage_excluded_bullet_shows_path_and_pattern():
    section = _section(
        render_markdown(
            _report(
                coverage=_coverage(
                    excluded_files=[
                        ExcludedFile(path="vendor/lib.py", pattern="vendor/"),
                        ExcludedFile(path="web/app.min.js", pattern="*.min.js"),
                    ]
                )
            )
        ),
        "### Coverage (FR-029)",
    )
    assert "- `vendor/lib.py` (pattern `vendor/`)" in section
    assert "- `web/app.min.js` (pattern `*.min.js`)" in section
    assert "<summary>Excluded files (2)</summary>" in section


def test_coverage_path_with_markdown_metacharacters_stays_literal():
    section = _section(
        render_markdown(
            _report(
                coverage=_coverage(
                    reviewed_files=["src/my_module.py", "src/*_generated.py", "a_b*c.py"]
                )
            )
        ),
        "### Coverage (FR-029)",
    )
    # Inside backticks, so neither the underscore nor the asterisk starts emphasis.
    assert "- `src/my_module.py`" in section
    assert "- `src/*_generated.py`" in section
    assert "- `a_b*c.py`" in section
    assert "<em>" not in section
    assert "<strong>" not in section


def test_coverage_section_sits_between_requirement_counts_and_verification_failures():
    md = render_markdown(_report(coverage=_coverage()))
    assert (
        md.index("### Requirement status counts")
        < md.index("### Coverage (FR-029)")
        < md.index("### Verification failures (FR-013)")
    )
    # Still inside the Summary area, before the next top-level section.
    assert md.index("## Summary") < md.index("### Coverage (FR-029)") < md.index("## Code Findings")


# ---------------------------------------------------------------------------
# T092 — source label prose and duplicate-merge metric (FR-012 / FR-013)
# ---------------------------------------------------------------------------


def test_duplicate_findings_merged_row_always_shown_and_defaults_to_zero():
    for md in (
        render_markdown(_report()),
        render_markdown(_report(code_findings=[], requirement_findings=[])),
    ):
        assert "| Duplicate findings merged | 0 |" in md


def test_duplicate_findings_merged_row_counts_merged_findings():
    report = _report()
    report.summary = report.summary.model_copy(update={"duplicates_merged": 2})
    md = render_markdown(report)
    assert "| Duplicate findings merged | 2 |" in md


def test_duplicate_findings_merged_row_immediately_after_citations_adjusted():
    labels = [
        row.split("|")[1].strip() for row in _metrics_rows(render_markdown(_report()))
    ]
    assert labels.index("Duplicate findings merged") == labels.index("Citations adjusted") + 1


def test_llm_identified_finding_labeled_in_heading_and_source_row():
    # The default _report finding is LLM-identified (FR-012): the heading carries
    # the suffix and the field table spells the honest provenance out.
    md = render_markdown(_report())
    assert "### `src/app.py`:2: Unused import *(LLM-identified, citation verified)*" in md
    assert "| Source | LLM-identified (citation verified) |" in md
    # The stored enum value is schema/JSON territory, not report prose.
    assert "| Source | llm-verified |" not in md


def test_citation_verified_explanatory_sentence_under_code_findings():
    sentence = (
        "Citation verified means the quoted code was found at the cited lines; "
        "it does not mean the finding's claim was confirmed."
    )
    assert "## Code Findings\n\n" + sentence in render_markdown(_report())


def test_sast_rules_line_rendered_when_recorded():
    report = _report()
    report.run = report.run.model_copy(update={"sast_rules": "r/corp-pack"})
    md = render_markdown(report)
    assert "- **SAST rules**: `r/corp-pack`" in md


def test_sast_rules_line_absent_when_sast_did_not_run():
    assert "- **SAST rules**" not in render_markdown(_report())

