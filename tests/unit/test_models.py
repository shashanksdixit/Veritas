"""Unit tests — Pydantic model validation (T005)."""

import json

import pytest
from pydantic import ValidationError

from veritas.models.entities import (
    LineRange,
    ReportStatus,
    ReviewRun,
    ReviewScope,
    Severity,
    Verdict,
)


def test_review_run_defaults():
    run = ReviewRun(
        scope=ReviewScope.FILE,
        target="src/app.py",
        config_hash="abc",
        model_name="openai/gpt-4o-mini",
        prompt_version="1.0.0",
    )
    assert run.report_status == ReportStatus.INCOMPLETE
    assert run.input_revision is None
    assert isinstance(run.id, str) and run.id


def test_scope_enum_values():
    assert {s.value for s in ReviewScope} == {"project", "module", "file", "pr"}


def test_verdict_enum_values():
    assert Verdict.REQUIRES_MODIFICATION.value == "RequiresModification"
    assert Verdict.REQUIRES_REVIEW.value == "RequiresReview"
    assert Verdict.CLEAN.value == "Clean"


def test_severity_values():
    assert {s.value for s in Severity} == {"error", "warning", "info"}


def test_confidence_bounds_enforced():
    from veritas.models.entities import Category, CodeFinding

    base = {
        "file": "src/app.py",
        "line_range": LineRange(start_line=1, start_col=1, end_line=1, end_col=1),
        "severity": Severity.ERROR,
        "category": Category.SECURITY,
        "title": "t",
        "description": "d",
        "recommendation": "r",
    }
    with pytest.raises(ValidationError):
        CodeFinding(**base, confidence=1.5)
    with pytest.raises(ValidationError):
        CodeFinding(**base, confidence=-0.1)
    assert CodeFinding(**base, confidence=0.5).confidence == 0.5


def test_code_finding_roundtrip():
    from veritas.models.entities import Category, CodeFinding, FindingSource, Severity as Sev

    finding = CodeFinding(
        file="src/app.py",
        line_range=LineRange(start_line=1, start_col=1, end_line=1, end_col=5),
        severity=Sev.ERROR,
        category=Category.SECURITY,
        source=FindingSource.SAST,
        title="t",
        description="d",
        recommendation="r",
        confidence=0.5,
    )
    assert finding.source == FindingSource.SAST
    data = finding.model_dump()
    assert data["severity"] == "error"


def test_report_roundtrip_json():
    from veritas.models.entities import Report, Summary, Severity as Sev, Verdict as V

    run = ReviewRun(
        scope=ReviewScope.PROJECT,
        target=".",
        config_hash="h",
        model_name="m",
        prompt_version="1.0.0",
    )
    report = Report(
        run=run,
        summary=Summary(
            total_code_findings=0,
            severity_counts={Sev.ERROR: 0},
            total_requirement_findings=0,
            verification_failure_count=0,
            verdict=V.CLEAN,
        ),
    )
    assert report.schema_version == "1.6.0"
    restored = Report.model_validate_json(report.model_dump_json())
    assert restored.run.id == report.run.id


def test_report_without_coverage_defaults_to_none():
    """FR-029: a pre-1.3.0-shaped report still validates; coverage is None."""
    from veritas.models.entities import Report, Summary, Verdict as V

    report = Report(
        run=ReviewRun(
            scope=ReviewScope.PROJECT,
            target=".",
            config_hash="h",
            model_name="m",
            prompt_version="1.2.0",
        ),
        summary=Summary(
            total_code_findings=0,
            total_requirement_findings=0,
            verification_failure_count=0,
            verdict=V.CLEAN,
        ),
    )
    assert report.coverage is None
    assert Report.model_validate_json(report.model_dump_json()).coverage is None


def test_report_coverage_roundtrip_json():
    """FR-029: a populated Coverage round-trips unchanged, ExcludedFile included."""
    from veritas.models.entities import (
        Coverage,
        ExcludedFile,
        Report,
        Summary,
        Verdict as V,
    )

    report = Report(
        run=ReviewRun(
            scope=ReviewScope.PROJECT,
            target=".",
            config_hash="h",
            model_name="m",
            prompt_version="1.2.0",
        ),
        summary=Summary(
            total_code_findings=0,
            total_requirement_findings=0,
            verification_failure_count=0,
            verdict=V.CLEAN,
        ),
        coverage=Coverage(
            batch_chars=48000,
            max_batches=8,
            batches_used=2,
            reviewed_files=["src/a.py", "src/b.py"],
            split_files=["src/b.py"],
            excluded_files=[ExcludedFile(path=".specify/tasks/spec.md", pattern=".specify/")],
            not_reviewed_files=["src/c.py"],
        ),
    )
    payload = report.model_dump_json()
    restored = Report.model_validate_json(payload)
    assert restored.coverage == report.coverage
    assert restored.model_dump_json() == payload
    assert restored.coverage.excluded_files[0].pattern == ".specify/"


def test_severity_adjusted_from_roundtrip_json():
    """FR-004: a capped finding keeps the reviewer's own severity through JSON."""
    from veritas.models.entities import Category, CodeFinding, Severity as Sev

    finding = CodeFinding(
        file="src/app.py",
        line_range=LineRange(start_line=4, start_col=1, end_line=6, end_col=1),
        severity=Sev.WARNING,
        severity_adjusted_from=Sev.ERROR,
        category=Category.TEST_COVERAGE,
        title="No test found for the retry path",
        description="d",
        recommendation="r",
    )

    restored = CodeFinding.model_validate_json(finding.model_dump_json())

    assert restored.severity is Sev.WARNING
    assert restored.severity_adjusted_from is Sev.ERROR


def test_a_finding_without_severity_adjusted_from_still_validates():
    """Schema 1.4.0 is additive: a 1.3.0-shaped finding deserializes unchanged."""
    from veritas.models.entities import Category, CodeFinding, Severity as Sev

    # The shape a 1.3.0 report on disk actually has: the key is absent, not null.
    payload = json.dumps(
        {
            "id": "abc123",
            "file": "src/app.py",
            "line_range": {"start_line": 1, "start_col": 1, "end_line": 1, "end_col": 1},
            "severity": "error",
            "category": "code_quality",
            "title": "t",
            "description": "d",
            "recommendation": "r",
        }
    )
    assert "severity_adjusted_from" not in payload

    restored = CodeFinding.model_validate_json(payload)

    assert restored.severity_adjusted_from is None
    assert restored.severity is Sev.ERROR
    assert restored.category is Category.CODE_QUALITY


def test_severity_adjusted_from_defaults_to_none():
    from veritas.models.entities import Category, CodeFinding, Severity as Sev

    finding = CodeFinding(
        file="src/app.py",
        line_range=LineRange(start_line=1, start_col=1, end_line=1, end_col=1),
        severity=Sev.WARNING,
        category=Category.TEST_COVERAGE,
        title="t",
        description="d",
        recommendation="r",
    )

    assert finding.severity_adjusted_from is None


# --- schema 1.5.0 adds not_addressed ---


def _requirement(status: str) -> dict:
    return {
        "requirement_ref": "FR-002",
        "requirement_text": "The system MUST log to stdout.",
        "status": status,
        "evidence": [],
        "explanation": "No code for this requirement is part of this PR.",
    }


def test_not_addressed_round_trips_through_json():
    from veritas.models.entities import RequirementFinding, RequirementStatus

    restored = RequirementFinding.model_validate_json(
        RequirementFinding(
            requirement_ref="FR-002",
            requirement_text="The system MUST log to stdout.",
            status=RequirementStatus.NOT_ADDRESSED,
            evidence=[],
            explanation="No code for this requirement is part of this PR.",
        ).model_dump_json()
    )

    assert restored.status is RequirementStatus.NOT_ADDRESSED
    assert RequirementStatus.NOT_ADDRESSED.value == "not_addressed"


def test_every_pre_1_5_requirement_status_still_validates():
    """The changelog promises earlier reports remain valid, so the five values a
    1.4.0 report could carry must all still parse."""
    from veritas.models.entities import RequirementFinding

    for status in ("satisfied", "partial", "gap", "unclear"):
        assert RequirementFinding.model_validate(_requirement(status)).status.value == status


def test_the_status_counts_table_of_an_older_report_still_validates():
    """A 1.4.0 report has no not_addressed key in its counts; the new key is additive."""
    from veritas.models.entities import RequirementStatus, Summary

    restored = Summary.model_validate(
        {
            "total_code_findings": 0,
            "severity_counts": {},
            "category_counts": {},
            "total_requirement_findings": 1,
            "requirement_status_counts": {"gap": 1},
            "verification_failure_count": 0,
            "verdict": "RequiresModification",
        }
    )

    assert restored.requirement_status_counts[RequirementStatus.GAP] == 1
    assert RequirementStatus.NOT_ADDRESSED not in restored.requirement_status_counts


# --- schema 1.6.0 adds summary.duplicates_merged (FR-013) ---


def test_duplicates_merged_round_trips_through_json():
    from veritas.models.entities import Severity as Sev, Summary, Verdict as V

    summary = Summary(
        total_code_findings=2,
        severity_counts={Sev.WARNING: 2},
        total_requirement_findings=0,
        verification_failure_count=0,
        duplicates_merged=3,
        verdict=V.REQUIRES_REVIEW,
    )

    restored = Summary.model_validate_json(summary.model_dump_json())

    assert restored.duplicates_merged == 3
    assert restored.verdict is V.REQUIRES_REVIEW


def test_a_pre_1_6_summary_without_duplicates_merged_still_validates():
    """Schema 1.6.0 is additive: the key is absent in older reports, not null."""
    from veritas.models.entities import Summary

    restored = Summary.model_validate(
        {
            "total_code_findings": 0,
            "severity_counts": {},
            "category_counts": {},
            "total_requirement_findings": 0,
            "requirement_status_counts": {},
            "verification_failure_count": 0,
            "verdict": "Clean",
        }
    )

    assert restored.duplicates_merged == 0