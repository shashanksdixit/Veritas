"""Summary aggregation (T041a, FR-015)."""

from __future__ import annotations

from veritas.models.entities import (
    Category,
    CodeFinding,
    RequirementFinding,
    RequirementStatus,
    Severity,
    Summary,
    VerificationFailure,
    Verdict,
)

# The requirement statuses that force each verdict (FR-015), stated as sets so the
# statuses that deliberately do NOT appear are as visible as the ones that do.
# RequirementStatus.NOT_ADDRESSED is in neither: a requirement the PR carries no
# code for is not a reason to hold that PR back, so it is counted in the report but
# never reaches a verdict. Adding a status therefore forces a decision here.
_MODIFICATION_STATUSES = frozenset({RequirementStatus.GAP})
_REVIEW_STATUSES = frozenset({RequirementStatus.PARTIAL, RequirementStatus.UNCLEAR})


def compute_summary(
    code_findings: list[CodeFinding],
    requirement_findings: list[RequirementFinding],
    verification_failures: list[VerificationFailure] | None = None,
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