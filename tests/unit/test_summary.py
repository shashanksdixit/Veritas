"""Unit tests — summary aggregation + verdict derivation (T041b, FR-015)."""

from veritas.models.entities import (
    Category,
    CodeFinding,
    FindingSource,
    LineRange,
    RequirementFinding,
    RequirementStatus,
    Severity,
    VerificationFailure,
    Verdict,
)
from veritas.output.summary import (
    _MODIFICATION_STATUSES,
    _REVIEW_STATUSES,
    compute_summary,
)


def _code_finding(severity: Severity, category: Category = Category.CODE_QUALITY) -> CodeFinding:
    return CodeFinding(
        file="src/app.py",
        line_range=LineRange(start_line=1, start_col=1, end_line=2, end_col=1),
        severity=severity,
        category=category,
        title="t",
        description="d",
        recommendation="r",
        confidence=0.9,
    )


def _req(status: RequirementStatus, ref: str = "REQ-1") -> RequirementFinding:
    return RequirementFinding(
        requirement_ref=ref,
        requirement_text="req text",
        status=status,
        evidence=[],
        explanation="e",
    )


def test_error_finding_forces_requires_modification():
    summary = compute_summary([_code_finding(Severity.ERROR)], [])
    assert summary.verdict is Verdict.REQUIRES_MODIFICATION


def test_warning_only_forces_requires_review():
    summary = compute_summary([_code_finding(Severity.WARNING)], [])
    assert summary.verdict is Verdict.REQUIRES_REVIEW


def test_info_finding_is_clean():
    summary = compute_summary([_code_finding(Severity.INFO)], [])
    assert summary.verdict is Verdict.CLEAN


def test_requirement_gap_forces_requires_modification():
    summary = compute_summary([], [_req(RequirementStatus.GAP)])
    assert summary.verdict is Verdict.REQUIRES_MODIFICATION


def test_partial_requirement_forces_requires_review():
    summary = compute_summary([], [_req(RequirementStatus.PARTIAL)])
    assert summary.verdict is Verdict.REQUIRES_REVIEW


def test_unclear_requirement_forces_requires_review():
    summary = compute_summary([], [_req(RequirementStatus.UNCLEAR)])
    assert summary.verdict is Verdict.REQUIRES_REVIEW


def test_not_addressed_never_reaches_a_verdict():
    """A requirement the PR carries no code for is not a reason to hold it back."""
    summary = compute_summary([], [_req(RequirementStatus.NOT_ADDRESSED)])
    assert summary.verdict is Verdict.CLEAN


def test_satisfied_and_not_addressed_requirements_are_clean_with_no_code_findings():
    summary = compute_summary(
        [],
        [
            _req(RequirementStatus.SATISFIED, "FR-001"),
            _req(RequirementStatus.NOT_ADDRESSED, "FR-002"),
            _req(RequirementStatus.NOT_ADDRESSED, "FR-003"),
        ],
    )
    assert summary.verdict is Verdict.CLEAN
    assert summary.requirement_status_counts[RequirementStatus.NOT_ADDRESSED] == 2
    assert summary.total_requirement_findings == 3


def test_not_addressed_does_not_mask_a_gap_or_a_warning():
    # It is not a verdict input, so it cannot absorb one: the other statuses decide.
    assert (
        compute_summary(
            [], [_req(RequirementStatus.NOT_ADDRESSED, "FR-001"), _req(RequirementStatus.GAP, "FR-002")]
        ).verdict
        is Verdict.REQUIRES_MODIFICATION
    )
    assert (
        compute_summary(
            [_code_finding(Severity.WARNING)],
            [_req(RequirementStatus.NOT_ADDRESSED)],
        ).verdict
        is Verdict.REQUIRES_REVIEW
    )


def test_every_requirement_status_is_classified_or_the_verdict_would_change():
    """A new status must be a deliberate decision here, not an omission.

    The verdict is derived from two explicit sets of statuses, so this is the tripwire
    for a future status being added to the enum and silently reaching no verdict.
    Exactly two statuses are deliberately outside both: the two that describe the
    PR being fine, so neither can hold it back.
    """
    classified = _MODIFICATION_STATUSES | _REVIEW_STATUSES
    assert classified == {
        RequirementStatus.GAP,
        RequirementStatus.PARTIAL,
        RequirementStatus.UNCLEAR,
    }
    assert set(RequirementStatus) - classified == {
        RequirementStatus.SATISFIED,
        RequirementStatus.NOT_ADDRESSED,
    }


def test_clean_verdict():
    summary = compute_summary([], [])
    assert summary.verdict is Verdict.CLEAN
    assert summary.total_code_findings == 0


def test_satisfied_requirements_count_only():
    summary = compute_summary([], [_req(RequirementStatus.SATISFIED, "REQ-1")])
    assert summary.verdict is Verdict.CLEAN
    assert summary.requirement_status_counts[RequirementStatus.SATISFIED] == 1


def test_counts_aggregated():
    findings = [
        _code_finding(Severity.ERROR),
        _code_finding(Severity.WARNING),
        _code_finding(Severity.INFO, Category.SECURITY),
    ]
    summary = compute_summary(findings, [])
    assert summary.total_code_findings == 3
    assert summary.severity_counts[Severity.ERROR] == 1
    assert summary.severity_counts[Severity.WARNING] == 1
    assert summary.category_counts[Category.SECURITY] == 1


def test_verification_failure_count_reported():
    failure = VerificationFailure(
        finding_id="f1",
        file="src/app.py",
        line_range=LineRange(start_line=1, start_col=1, end_line=1, end_col=1),
        reason="x",
    )
    summary = compute_summary([], [], [failure])
    assert summary.verification_failure_count == 1
    assert summary.verification_failures == [failure]


def test_sast_source_flag_flows_through():
    finding = _code_finding(Severity.ERROR, Category.SECURITY)
    finding.source = FindingSource.SAST
    summary = compute_summary([finding], [])
    assert summary.total_code_findings == 1