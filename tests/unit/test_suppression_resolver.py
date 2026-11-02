"""Unit tests — suppression resolution rules (T016, FR-017)."""

from veritas.models.entities import (
    Category,
    CodeFinding,
    LineRange,
    Report,
    ReportStatus,
    ReviewRun,
    ReviewScope,
    Severity,
    Summary,
    Verdict,
)
from veritas.suppression.resolver import resolve_by_id, resolve_by_location


def _finding(id_: str, file: str, start: int, end: int = 1) -> CodeFinding:
    return CodeFinding(
        id=id_,
        file=file,
        line_range=LineRange(start_line=start, start_col=1, end_line=end, end_col=1),
        severity=Severity.WARNING,
        category=Category.CODE_QUALITY,
        title="t",
        description="d",
        recommendation="r",
        confidence=0.5,
    )


def _report(findings) -> Report:
    return Report(
        run=ReviewRun(
            scope=ReviewScope.PROJECT,
            target=".",
            config_hash="h",
            model_name="m",
            prompt_version="1.0.0",
            report_status=ReportStatus.COMPLETE,
        ),
        code_findings=findings,
        summary=Summary(
            total_code_findings=len(findings),
            total_requirement_findings=0,
            verification_failure_count=0,
            verdict=Verdict.CLEAN,
        ),
    )


def test_resolve_by_id_single_match():
    report = _report([_finding("f1", "a.py", 1), _finding("f2", "a.py", 5)])
    resolution = resolve_by_id(report, "f2")
    assert resolution.status == "matched"
    assert resolution.finding.id == "f2"


def test_resolve_by_id_no_match():
    report = _report([_finding("f1", "a.py", 1)])
    assert resolve_by_id(report, "nope").status == "no_match"


def test_resolve_by_location_single():
    report = _report([_finding("f1", "a.py", 3, end=5)])
    resolution = resolve_by_location(report, "a.py", 4)
    assert resolution.status == "matched"
    assert resolution.finding.id == "f1"


def test_resolve_by_location_ambiguous():
    report = _report([_finding("f1", "a.py", 1, end=10), _finding("f2", "a.py", 2, end=9)])
    resolution = resolve_by_location(report, "a.py", 5)
    assert resolution.status == "ambiguous"
    assert len(resolution.candidates) == 2


def test_resolve_by_location_no_match():
    report = _report([_finding("f1", "a.py", 1)])
    assert resolve_by_location(report, "a.py", 99).status == "no_match"