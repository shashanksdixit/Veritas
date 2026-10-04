"""Markdown report renderer (T018, FR-016/FR-024/FR-025).

Output MUST be valid GitHub-Flavored Markdown postable directly as a PR
comment, and MUST emit ``Report.schema_version`` as an HTML comment at the top
so the version is inspectable without parsing the report body."""

from __future__ import annotations

import html

from veritas.models.entities import (
    Category,
    CodeFinding,
    Coverage,
    Report,
    RequirementStatus,
    Severity,
    Summary,
    VerificationFailure,
    VerificationReasonCode,
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


def _fence(text: str) -> str:
    """Fence ``text`` so a backtick run inside it cannot close the block early.

    The fence is one backtick longer than the longest run of consecutive
    backticks in ``text``, with a floor of three. No language tag: these
    snippets are prose from a review, not a specific language.
    """
    longest = 0
    run = 0
    for char in text:
        if char == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text}\n{ticks}"


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

_REASON_CODE_MEANINGS: dict[VerificationReasonCode, str] = {
    VerificationReasonCode.FILE_NOT_IN_SCOPE: "The cited file was not among the files reviewed.",
    VerificationReasonCode.LINE_OUT_OF_RANGE: "The cited line is past the end of the file.",
    VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE: (
        "The quoted code exists in the file, but not at the cited lines (often a wrong line number)."
    ),
    VerificationReasonCode.SNIPPET_NOT_FOUND: (
        "The quoted code does not appear anywhere in the file (the reviewer likely invented it)."
    ),
    VerificationReasonCode.EVIDENCE_NOT_CONFIRMED: "A requirement's cited evidence could not be confirmed.",
}


_VERIFICATION_FAILURE_NOTE = (
    "> Each proposed finding must cite a file, line range, and the exact code snippet it refers to. "
    "Before a finding is allowed into this report, Veritas re-reads the file and checks that those lines "
    "really contain that snippet. When they don't, Veritas looks for the quoted code nearby: if it appears "
    "exactly once within two lines of the cited range, the citation is corrected and the finding is kept "
    '(shown as "Citation adjusted" on that finding). Otherwise the finding is dropped and listed here so '
    "nothing disappears silently. A dropped finding does not mean your code has a problem; it means the "
    "reviewer's claim could not be confirmed."
)


def _citations_adjusted(report: Report) -> int:
    """How many code findings had their citation corrected (FR-013).

    Counted from the findings themselves rather than read off ``Summary``: this is
    derivable state, so it does not need a schema field of its own.
    """
    return sum(1 for finding in report.code_findings if finding.citation_adjusted_from is not None)


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


def _render_reason_code_legend(out: list[str], failures: list[VerificationFailure]) -> None:
    """Explain only the reason codes this report actually uses (FR-013)."""
    present = {vf.reason_code for vf in failures}
    rows = [
        (f"`{code.value}`", _REASON_CODE_MEANINGS[code])
        for code in VerificationReasonCode
        if code in present
    ]
    if not rows:
        return
    out.append("**Reason codes**")
    out.append("")
    _render_table(out, ("Code", "Meaning"), rows)
    out.append("")


def _render_failure_detail(out: list[str], vf: VerificationFailure) -> None:
    """Collapsed per-failure block with the snippets, when there are any."""
    if vf.claimed_snippet is None and vf.actual_snippet is None:
        return
    summary_parts = [
        vf.finding_id,
        f"{vf.file}:{vf.line_range.start_line}-{vf.line_range.end_line}",
    ]
    if vf.reason_code is not None:
        summary_parts.append(vf.reason_code.value)
    out.append("<details>")
    out.append(f"<summary>{html.escape(' — '.join(summary_parts), quote=False)}</summary>")
    out.append("")
    out.append(f"**Reason**: {vf.reason}")
    out.append("")
    if vf.found_at_lines is not None:
        out.append(f"**Found at line(s)**: {', '.join(str(line) for line in vf.found_at_lines)}")
        out.append("")
    for label, snippet in (
        ("**Claimed** (what the reviewer quoted):", vf.claimed_snippet),
        ("**Actual** (what is at the cited lines):", vf.actual_snippet),
    ):
        if snippet is None:
            continue
        out.append(label)
        out.append("")
        out.append(_fence(snippet))
        out.append("")
    out.append("</details>")
    out.append("")


def _coverage_details(out: list[str], label: str, bullets: list[str]) -> None:
    """One collapsed list of paths, or nothing at all for an empty list.

    A <details> block for an empty list would be a heading a reader can expand to
    find nothing, so empty lists are skipped entirely.
    """
    if not bullets:
        return
    out.append("<details>")
    out.append(f"<summary>{label} ({len(bullets)})</summary>")
    out.append("")
    for bullet in bullets:
        out.append(f"- {bullet}")
    out.append("")
    out.append("</details>")
    out.append("")


def _render_coverage(out: list[str], coverage: Coverage) -> None:
    """What this run actually reviewed, and what it withheld and why (FR-029).

    Only called when the run has coverage data, so a report from a run with no
    batch plan is unchanged.
    """
    out.append("### Coverage (FR-029)")
    out.append("")
    out.append(
        f"Code is reviewed in batches of up to {coverage.batch_chars} characters, "
        f"at most {coverage.max_batches} batches per review type, with application "
        "code before test files. Files matching an exclusion pattern are not reviewed."
    )
    out.append("")

    # A file the batch limit left out is a gap in the review, so the warning sits
    # in the open, outside every <details>: a collapsed block would hide the one
    # thing the reader has to act on.
    if coverage.not_reviewed_files:
        out.append(
            f"**{len(coverage.not_reviewed_files)} file(s) were not reviewed** because "
            "the batch limit was reached. Raise `max_batches` or `batch_chars` in the "
            "`[review]` config section to include them."
        )
        out.append("")

    _render_table(
        out,
        ("Metric", "Count"),
        [
            ("Files reviewed", str(len(coverage.reviewed_files))),
            ("Files split across batches", str(len(coverage.split_files))),
            ("Files excluded", str(len(coverage.excluded_files))),
            ("Files not reviewed (batch limit)", str(len(coverage.not_reviewed_files))),
            ("Batches used", f"{coverage.batches_used} of {coverage.max_batches}"),
        ],
    )
    out.append("")

    _coverage_details(out, "Reviewed files", [f"`{path}`" for path in coverage.reviewed_files])
    _coverage_details(out, "Split files", [f"`{path}`" for path in coverage.split_files])
    _coverage_details(
        out,
        "Excluded files",
        [f"`{item.path}` (pattern `{item.pattern}`)" for item in coverage.excluded_files],
    )
    _coverage_details(
        out, "Not reviewed files", [f"`{path}`" for path in coverage.not_reviewed_files]
    )


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
    out.append("")
    out.append(_triggered_by_line(summary))
    out.append("")
    _render_verdict_legend(out)
    out.append("")
    rows = [
        ("Code findings", str(summary.total_code_findings)),
        ("Requirement findings", str(summary.total_requirement_findings)),
        ("Verification failures", str(summary.verification_failure_count)),
        ("Citations adjusted", str(_citations_adjusted(report))),
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

    if report.coverage is not None:
        _render_coverage(out, report.coverage)

    if summary.verification_failures:
        out.append("### Verification failures (FR-013)")
        out.append("")
        out.append(_VERIFICATION_FAILURE_NOTE)
        out.append("")
        _render_table(
            out,
            ("Finding ID", "File", "Lines", "Code", "Reason"),
            [
                (
                    vf.finding_id,
                    vf.file,
                    f"{vf.line_range.start_line}:{vf.line_range.end_line}",
                    f"`{vf.reason_code.value}`" if vf.reason_code is not None else "",
                    vf.reason,
                )
                for vf in summary.verification_failures
            ],
        )
        out.append("")
        _render_reason_code_legend(out, summary.verification_failures)
        for vf in summary.verification_failures:
            _render_failure_detail(out, vf)

    out.append("## Code Findings")
    out.append("")
    if not report.code_findings:
        out.append("No code findings.")
        out.append("")
    for finding in report.code_findings:
        out.append(f"### `{finding.file}`:{finding.line_range.start_line}: {finding.title}{_source_label(finding)}")
        out.append("")
        field_rows = [
            ("Severity", _severity_label(finding.severity)),
            ("Category", _category_label(finding.category)),
            ("Confidence", f"{finding.confidence:.2f}"),
            ("OWASP", finding.owasp_id or "—"),
            ("CWE", finding.cwe_id or "—"),
            ("Source", finding.source.value if finding.source else "—"),
            ("Suppressed", "yes" if finding.is_suppressed else "no"),
        ]
        # FR-013: a corrected citation must be visible, never a silent adjustment.
        # This table has no location rows — the finding's location is its heading —
        # so the correction row goes last, next to the other provenance fields.
        if finding.citation_adjusted_from is not None:
            field_rows.append(
                (
                    "Citation adjusted",
                    f"from {finding.citation_adjusted_from.start_line}"
                    f"-{finding.citation_adjusted_from.end_line} "
                    f"to {finding.line_range.start_line}-{finding.line_range.end_line}",
                )
            )
        _render_table(out, ("Field", "Value"), field_rows)
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