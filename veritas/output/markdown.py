"""Markdown report renderer (T018, FR-016/FR-024/FR-025).

Output MUST be valid GitHub-Flavored Markdown postable directly as a PR
comment, and MUST emit ``Report.schema_version`` as an HTML comment at the top
so the version is inspectable without parsing the report body."""

from __future__ import annotations

from veritas.models.entities import (
    Category,
    CodeFinding,
    Report,
    RequirementStatus,
    Severity,
    Summary,
)


def _severity_label(sev: Severity) -> str:
    return {
        Severity.ERROR: "🔴 error",
        Severity.WARNING: "🟠 warning",
        Severity.INFO: "🔵 info",
    }.get(sev, str(sev))


def _category_label(cat: Category) -> str:
    return {
        Category.CODE_QUALITY: "Code Quality",
        Category.SECURITY: "Security",
        Category.REQUIREMENT: "Requirements",
        Category.TEST_COVERAGE: "Test Coverage",
        Category.PERFORMANCE: "Performance",
    }.get(cat, str(cat))


def _source_label(finding: CodeFinding) -> str:
    if finding.source is None:
        return ""
    return " *(SAST)*" if finding.source.value == "sast" else " *(LLM-verified)*"


def _code_block(text: str | None) -> str:
    if not text:
        return ""
    return f"```text\n{text}\n```\n"


_VERDICT_LEGEND: tuple[tuple[str, str], ...] = (
    (
        "`RequiresModification`",
        "At least one error-severity finding or one requirement gap. Changes are needed before this is ready.",
    ),
    (
        "`RequiresReview`",
        "No errors or gaps, but at least one warning-severity finding or a requirement that is partial or "
        "unclear. A person should look at these before approving.",
    ),
    (
        "`Clean`",
        "No errors, warnings, gaps, or partial/unclear requirements. Info-level notes may still be listed; "
        "they do not affect the verdict.",
    ),
)

_VERIFICATION_FAILURE_NOTE = (
    "> Each proposed finding must cite a file, line range, and the exact code snippet it refers to. "
    "Before a finding is allowed into this report, Veritas re-reads the file and checks that those lines "
    "really contain that snippet. When they don't, the finding is dropped and listed here so nothing "
    "disappears silently. A dropped finding does not mean your code has a problem; it means the reviewer's "
    "claim could not be confirmed."
)


def _triggered_by_line(summary: Summary) -> str:
    """Say which counts produced the verdict (FR-015)."""
    errors = summary.severity_counts.get(Severity.ERROR, 0)
    warnings = summary.severity_counts.get(Severity.WARNING, 0)
    gaps = summary.requirement_status_counts.get(RequirementStatus.GAP, 0)
    partial_unclear = summary.requirement_status_counts.get(
        RequirementStatus.PARTIAL, 0
    ) + summary.requirement_status_counts.get(RequirementStatus.UNCLEAR, 0)
    return (
        f"**Triggered by**: {errors} error(s), {gaps} requirement gap(s), "
        f"{warnings} warning(s), {partial_unclear} partial/unclear requirement(s)"
    )


def _render_verdict_legend(out: list[str]) -> None:
    """Static definition of all three verdict values (FR-015)."""
    out.append("**What the verdicts mean**")
    out.append("")
    _render_table(out, ("Verdict", "Meaning"), list(_VERDICT_LEGEND))


def render_markdown(report: Report) -> str:
    """Render the full report as GFM Markdown (SC-007)."""
    run = report.run
    out: list[str] = []
    out.append(f"<!-- veritas-report-schema: {report.schema_version} -->")
    out.append("")
    out.append(f"# Veritas Code Review — {_scope_title(run.scope)}")
    out.append("")
    out.append(f"- **Target**: `{run.target}`")
    out.append(f"- **Scope**: `{run.scope.value}`")
    out.append(f"- **Model**: `{run.model_name}`")
    out.append(f"- **Prompt version**: `{run.prompt_version}`")
    out.append(f"- **Input revision**: `{run.input_revision or 'n/a'}`")
    out.append(f"- **Report status**: `{run.report_status.value}`")
    if run.error:
        out.append(f"- **Error**: {run.error}")
    out.append("")

    out.append("## Summary")
    out.append("")
    summary = report.summary
    out.append(f"**Verdict**: `{summary.verdict.value}`")
    out.append(_triggered_by_line(summary))
    out.append("")
    _render_verdict_legend(out)
    out.append("")
    rows = [
        ("Code findings", str(summary.total_code_findings)),
        ("Requirement findings", str(summary.total_requirement_findings)),
        ("Verification failures", str(summary.verification_failure_count)),
    ]
    for sev in Severity:
        rows.append((f"Severity: {sev.value}", str(summary.severity_counts.get(sev, 0))))
    for cat in Category:
        rows.append((f"Category: {cat.value}", str(summary.category_counts.get(cat, 0))))
    _render_table(out, ("Metric", "Count"), rows)
    out.append("")
    out.append("### Requirement status counts")
    out.append("")
    _render_table(
        out,
        ("Status", "Count"),
        [
            (f"Requirement status: {status.value}", str(summary.requirement_status_counts.get(status, 0)))
            for status in RequirementStatus
        ],
    )
    out.append("")

    if summary.verification_failures:
        out.append("### Verification failures (FR-013)")
        out.append("")
        out.append(_VERIFICATION_FAILURE_NOTE)
        out.append("")
        _render_table(
            out,
            ("Finding ID", "File", "Lines", "Reason"),
            [
                (vf.finding_id, vf.file, f"{vf.line_range.start_line}:{vf.line_range.end_line}", vf.reason)
                for vf in summary.verification_failures
            ],
        )
        out.append("")

    out.append("## Code Findings")
    out.append("")
    if not report.code_findings:
        out.append("No code findings.")
        out.append("")
    for finding in report.code_findings:
        out.append(f"### `{finding.file}`:{finding.line_range.start_line}: {finding.title}{_source_label(finding)}")
        out.append("")
        _render_table(
            out,
            ("Field", "Value"),
            [
                ("Severity", _severity_label(finding.severity)),
                ("Category", _category_label(finding.category)),
                ("Confidence", f"{finding.confidence:.2f}"),
                ("OWASP", finding.owasp_id or "—"),
                ("CWE", finding.cwe_id or "—"),
                ("Source", finding.source.value if finding.source else "—"),
                ("Suppressed", "yes" if finding.is_suppressed else "no"),
            ],
        )
        out.append("")
        out.append("**Description**:")
        out.append("")
        out.append(finding.description)
        out.append("")
        out.append("**Recommendation**:")
        out.append("")
        out.append(finding.recommendation)
        out.append("")
        if finding.cited_snippet:
            out.append("**Cited snippet**:")
            out.append("")
            out.append(_code_block(finding.cited_snippet))
            out.append("")

    out.append("## Requirement Findings")
    out.append("")
    if not report.requirement_findings:
        out.append("No requirement findings.")
        out.append("")
    for rf in report.requirement_findings:
        out.append(f"### {rf.requirement_ref} — *{rf.status.value}*")
        out.append("")
        out.append(f"**Requirement**: {rf.requirement_text}")
        out.append("")
        out.append(f"**Status**: `{rf.status.value}`")
        out.append("")
        out.append("**Explanation**:")
        out.append("")
        out.append(rf.explanation)
        out.append("")
        if rf.evidence:
            out.append("**Evidence**:")
            out.append("")
            for ref in rf.evidence:
                out.append(f"- `{ref}`")
            out.append("")

    result = "\n".join(out)
    report.markdown_content = result
    return result


def _render_table(out: list[str], header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "|".join("---" for _ in header) + "|")
    for row in rows:
        cleaned = tuple(str(cell).replace("|", "\\|").replace("\n", " ") for cell in row)
        out.append("| " + " | ".join(cleaned) + " |")


def _scope_title(scope) -> str:
    return {
        "project": "Project",
        "module": "Module",
        "file": "File",
        "pr": "Pull Request",
    }.get(getattr(scope, "value", str(scope)), str(scope))