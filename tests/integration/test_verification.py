"""Integration tests — FR-013 citation re-verification (T041, SC-002/SC-005)."""

from dataclasses import dataclass

from veritas.models.entities import (
    Category,
    CodeFinding,
    FindingSource,
    LineRange,
    RequirementFinding,
    RequirementStatus,
    Severity,
)
from veritas.review.nodes.verification import (
    make_verify_node,
    verify_code_finding,
    verify_requirement_finding,
)

FILES = {
    "src/app.py": "import os\nprint(\"hello\")\nsecret = 42\n",
}


@dataclass
class _Runtime:
    log = None


def _finding(snippet=None, start=2, end=2, source=FindingSource.LLM_IDENTIFIED, id="f-check") -> CodeFinding:
    return CodeFinding(
        id=id,
        file="src/app.py",
        line_range=LineRange(start_line=start, start_col=1, end_line=end, end_col=18),
        severity=Severity.WARNING,
        category=Category.CODE_QUALITY,
        source=source,
        title="t",
        description="d",
        recommendation="r",
        confidence=0.9,
        cited_snippet=snippet,
    )


def test_matching_snippet_verifies():
    assert verify_code_finding(_finding(snippet="print(\"hello\")"), FILES) is None


def test_whitespace_drift_tolerated():
    assert verify_code_finding(_finding(snippet='print(  "hello" )'), FILES) is None


def test_mismatched_snippet_fails():
    reason = verify_code_finding(_finding(snippet="def totally_different(): pass"), FILES)
    assert reason is not None and "does not match" in reason


def test_missing_file_fails():
    finding = _finding(snippet="print(1)")
    finding.file = "src/nope.py"
    assert "file not found" in (verify_code_finding(finding, FILES) or "")


def test_line_out_of_range_fails():
    assert "out of range" in (verify_code_finding(_finding(snippet="x", start=99, end=99), FILES) or "")


def test_sast_exempt_from_verification():
    finding = _finding(snippet="does-not-matter", source=FindingSource.SAST)
    # SAST is ground truth: verify still confirms, but the verify node keeps it
    # unconditionally (tested below via node).
    assert verify_code_finding(finding, FILES) is not None


def test_verify_node_keeps_sast_and_confirmed():
    runtime = _Runtime()
    node = make_verify_node(runtime)
    kept_missing = _finding(snippet="nope nope nope", id="f1")
    kept_confirmed = _finding(snippet="print(\"hello\")", id="f2")
    kept_sast = _finding(snippet="nope nope nope", id="f3", source=FindingSource.SAST)
    out = node(
        {
            "files": FILES,
            "code_findings": [kept_missing, kept_confirmed, kept_sast],
            "requirement_findings": [],
        }
    )
    kept_ids = {f.id for f in out["verified_code_findings"]}
    assert kept_ids == {"f2", "f3"}
    assert len(out["verification_failures"]) == 1
    assert out["verification_failures"][0].reason is not None


def test_unclear_requirement_passes_without_evidence():
    rf = RequirementFinding(
        requirement_ref="REQ-1",
        requirement_text="x",
        status=RequirementStatus.UNCLEAR,
        evidence=[],
        explanation="e",
    )
    assert verify_requirement_finding(rf, FILES) == []


def test_requirement_bad_evidence_fails():
    rf = RequirementFinding(
        requirement_ref="REQ-1",
        requirement_text="x",
        status=RequirementStatus.PARTIAL,
        evidence=["src/missing.py", "src/app.py:99"],
        explanation="e",
    )
    failures = verify_requirement_finding(rf, FILES)
    assert len(failures) == 2


def test_requirement_good_evidence_passes():
    rf = RequirementFinding(
        requirement_ref="REQ-1",
        requirement_text="x",
        status=RequirementStatus.PARTIAL,
        evidence=["src/app.py:2"],
        explanation="e",
    )
    assert verify_requirement_finding(rf, FILES) == []