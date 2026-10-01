"""Unit tests — Pydantic model validation (T005)."""

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
    assert report.schema_version == "1.2.0"
    restored = Report.model_validate_json(report.model_dump_json())
    assert restored.run.id == report.run.id