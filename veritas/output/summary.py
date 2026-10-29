"""Summary aggregation (T041a, FR-015)."""

from __future__ import annotations

from veritas.models.entities import (
    Category,
    CodeFinding,
    RequirementFinding,
    ReportStatus,
    RequirementStatus,
    ReviewRun,
    Severity,
    Summary,
    VerificationFailure,
    VerificationReasonCode,
    Verdict,
)

# The requirement statuses that force each verdict (FR-015), stated as sets so the
# statuses that deliberately do NOT appear are as visible as the ones that do.
# RequirementStatus.NOT_ADDRESSED is in neither: a requirement the PR carries no
# code for is not a reason to hold that PR back, so it is counted in the report but
# never reaches a verdict. Adding a status therefore forces a decision here.
_MODIFICATION_STATUSES = frozenset({RequirementStatus.GAP})
_REVIEW_STATUSES = frozenset({RequirementStatus.PARTIAL, RequirementStatus.UNCLEAR})

# The reason codes FR-013 gives a requirement finding. A requirement finding that
# fails stays in the report; every other code is a code finding's, and a code
# finding that fails is excluded.
_REQUIREMENT_REASON_CODES = frozenset(
    {VerificationReasonCode.EVIDENCE_NOT_CONFIRMED, VerificationReasonCode.EXPLANATION_MISSING}
)

# FR-015/FR-027: what stdout and Markdown add after the verdict of an incomplete report.
INCOMPLETE_VERDICT_QUALIFIER = "incomplete review: covers only what was reviewed"


def verdict_qualifier(run: ReviewRun) -> str | None:
    """The qualifier for a verdict shown to a person, or None (FR-015, FR-027).

    The verdict is derived only from the findings in the report, so a run whose
    every LLM call failed has no findings and derives Clean. Report.summary.verdict
    keeps that derived value; this is what stdout and Markdown add so an
    incomplete report's verdict is not read as covering code nobody reviewed.
    """
    if run.report_status is ReportStatus.INCOMPLETE:
        return INCOMPLETE_VERDICT_QUALIFIER
    return None


def verification_outcomes(failures: list[VerificationFailure]) -> str:
    """What verification did to the findings behind ``failures`` (FR-013, FR-015).

    The two kinds are counted separately because they end differently: a code
    finding that fails is excluded from the report, a requirement finding that
    fails is kept with its unconfirmed evidence removed or marked unclear. Each
    count is of findings, not of failures: one requirement finding can record
    both evidence_not_confirmed and explanation_missing. Only the kinds present
    are named, so the result is empty when ``failures`` is.

    The kind is read from ``reason_code`` alone, so this needs nothing the
    report does not already carry. A failure with no reason_code (a report
    shape older than schema 1.1.0) is counted as a code finding.
    """
    excluded = {vf.finding_id for vf in failures if vf.reason_code not in _REQUIREMENT_REASON_CODES}
    kept = {vf.finding_id for vf in failures if vf.reason_code in _REQUIREMENT_REASON_CODES}
    parts: list[str] = []
    if excluded:
        parts.append(f"{len(excluded)} code finding(s) excluded")
    if kept:
        parts.append(
            f"{len(kept)} requirement finding(s) kept, with unconfirmed evidence removed or marked unclear"
        )
    return "; ".join(parts)


def compute_summary(
    code_findings: list[CodeFinding],
    requirement_findings: list[RequirementFinding],
    verification_failures: list[VerificationFailure] | None = None,
    *,
    duplicates_merged: int = 0,
) -> Summary:
    """Aggregate counts and derive the verdict per FR-015.

    Verdict:
      RequiresModification — any error-severity CodeFinding, or any
        RequirementFinding with status == gap.
      RequiresReview — none of the above, but any warning-severity CodeFinding,
        or any RequirementFinding with status in {partial, unclear}.
      Clean — otherwise.

    not_addressed is counted in ``requirement_status_counts`` and reaches neither
    verdict (FR-015).

    ``duplicates_merged`` is how many code findings verification folded together
    (FR-013): reported, never part of a verdict, defaulting to 0 so callers that
    have nothing to report — and reports written before schema 1.6.0 — are
    unchanged.
    """
    failures = verification_failures or []
    severity_counts: dict[Severity, int] = {}
    category_counts: dict[Category, int] = {}
    for finding in code_findings:
        severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1
        category_counts[finding.category] = category_counts.get(finding.category, 0) + 1

    requirement_status_counts: dict[RequirementStatus, int] = {}
    for rf in requirement_findings:
        requirement_status_counts[rf.status] = requirement_status_counts.get(rf.status, 0) + 1

    verdict = _derive_verdict(code_findings, requirement_findings)
    return Summary(
        total_code_findings=len(code_findings),
        severity_counts=severity_counts,
        category_counts=category_counts,
        total_requirement_findings=len(requirement_findings),
        requirement_status_counts=requirement_status_counts,
        verification_failure_count=len(failures),
        verification_failures=failures,
        duplicates_merged=duplicates_merged,
        verdict=verdict,
    )


def _derive_verdict(
    code_findings: list[CodeFinding],
    requirement_findings: list[RequirementFinding],
) -> Verdict:
    for finding in code_findings:
        if finding.severity == Severity.ERROR:
            return Verdict.REQUIRES_MODIFICATION
    for rf in requirement_findings:
        if rf.status in _MODIFICATION_STATUSES:
            return Verdict.REQUIRES_MODIFICATION
    for finding in code_findings:
        if finding.severity == Severity.WARNING:
            return Verdict.REQUIRES_REVIEW
    for rf in requirement_findings:
        if rf.status in _REVIEW_STATUSES:
            return Verdict.REQUIRES_REVIEW
    return Verdict.CLEAN