"""Compact stdout renderer (T019, FR-016).

Counts + headline findings only: every error-severity CodeFinding, capped at
10, with a ``+N more, see full report`` note when the cap is hit. The full
report is always in the report file."""

from __future__ import annotations

from veritas.models.entities import (
    Category,
    CodeFinding,
    Report,
    RequirementStatus,
    Severity,
)

_HEADLINE_CAP = 10


def render_compact(report: Report, report_path: str | None = None) -> str:
    """Render the compact stdout summary (FR-016)."""
    summary = report.summary
    lines: list[str] = []

    total = summary.total_code_findings
    sev = summary.severity_counts
    sev_snippet = ", ".join(
        f"{sev.get(s, 0)} {s.value}" for s in (Severity.ERROR, Severity.WARNING, Severity.INFO)
    )
    lines.append(f"Veritas Review: {total} findings ({sev_snippet})")

    category_bits: list[str] = []
    for cat in (
        Category.CODE_QUALITY,
        Category.SECURITY,
        Category.REQUIREMENT,
        Category.TEST_COVERAGE,
        Category.PERFORMANCE,
    ):
        count = summary.category_counts.get(cat, 0)
        if count:
            category_bits.append(f"{_category_label(cat)}: {count}")
    if category_bits:
        lines.append(" | ".join(category_bits))

    source_bits: list[str] = []
    for finding in report.code_findings:
        if finding.category == Category.SECURITY and finding.source is not None:
            source_bits.append(finding.source.value)
    if source_bits:
        from collections import Counter

        src_counts = Counter(source_bits)
        lines.append(
            "Security sources: "
            + " | ".join(f"{count} {label}" for label, count in sorted(src_counts.items()))
        )

    req_snippet = ", ".join(
        f"{summary.requirement_status_counts.get(s, 0)} {s.value}"
        for s in (
            RequirementStatus.SATISFIED,
            RequirementStatus.PARTIAL,
            RequirementStatus.GAP,
            RequirementStatus.UNCLEAR,
        )
    )
    lines.append(f"Requirements: {req_snippet}")

    lines.append(f"Verdict: {summary.verdict.value}")

    headline = [
        finding
        for finding in report.code_findings
        if finding.severity == Severity.ERROR and not finding.is_suppressed
    ]
    if headline:
        lines.append("")
        lines.append("Headline findings (errors):")
        for finding in headline[:_HEADLINE_CAP]:
            lines.append(f"  - {finding.file}:{finding.line_range.start_line}: {finding.title}")
        extra = len(headline) - _HEADLINE_CAP
        if extra > 0:
            lines.append(f"  +{extra} more, see full report")

    if summary.verification_failure_count:
        lines.append(
            f"Verification failures: {summary.verification_failure_count} finding(s) excluded (FR-013), see full report"
        )

    if report.run.report_status.value == "incomplete":
        lines.append(f"Report status: incomplete — {report.run.error or 'review incomplete'}")
    lines.append(f"Report: {report_path or report.markdown_content or 'n/a'}")
    return "\n".join(lines)


def _category_label(cat: Category) -> str:
    return {
        Category.CODE_QUALITY: "Code Quality",
        Category.SECURITY: "Security",
        Category.REQUIREMENT: "Requirements",
        Category.TEST_COVERAGE: "Test Coverage",
        Category.PERFORMANCE: "Performance",
    }.get(cat, str(cat))