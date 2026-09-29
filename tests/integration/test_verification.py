"""Integration tests — FR-013 citation re-verification (T041, SC-002/SC-005).

Covers the structured failure detail added in T066: `reason_code`, redacted +
truncated `claimed_snippet` / `actual_snippet`, and `found_at_lines`.
"""

from dataclasses import dataclass

from veritas.models.entities import (
    Category,
    CodeFinding,
    FindingSource,
    LineRange,
    RequirementFinding,
    RequirementStatus,
    Severity,
    VerificationReasonCode,
)
from veritas.review.nodes.verification import (
    FOUND_AT_MAX,
    SNIPPET_MAX_CHARS,
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
    failure = verify_code_finding(_finding(snippet="def totally_different(): pass"), FILES)
    assert failure is not None
    assert failure.reason_code == VerificationReasonCode.SNIPPET_NOT_FOUND
    assert failure.reason == "snippet does not occur anywhere in src/app.py"


def test_missing_file_fails():
    finding = _finding(snippet="print(1)")
    finding.file = "src/nope.py"
    failure = verify_code_finding(finding, FILES)
    assert failure is not None
    assert failure.reason_code == VerificationReasonCode.FILE_NOT_IN_SCOPE
    assert failure.reason == "file src/nope.py is not in the reviewed file set"


def test_line_out_of_range_fails():
    failure = verify_code_finding(_finding(snippet="x", start=99, end=99), FILES)
    assert failure is not None
    assert failure.reason_code == VerificationReasonCode.LINE_OUT_OF_RANGE
    assert failure.reason == "cited start line 99 exceeds file length (3 lines)"


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


# ---------------------------------------------------------------------------
# T066 — structured failure detail (FR-013)
# ---------------------------------------------------------------------------

# "TARGET" appears on lines 8-14, so a citation to line 1 misses the window
# while the snippet itself is present seven times in the file.
OFFSET_FILES = {"src/app.py": "filler\n" * 7 + "TARGET\n" * 7}

# Three lines of real content, so a snippet covering all of them can be cited
# against a one-line window.
MULTILINE_FILES = {"src/app.py": "alpha\nbeta\ngamma\n"}


def test_reason_code_file_not_in_scope():
    finding = _finding(snippet="print(1)", id="f-scope")
    finding.file = "src/elsewhere.py"
    failure = verify_code_finding(finding, FILES)
    assert failure.reason_code is VerificationReasonCode.FILE_NOT_IN_SCOPE
    assert failure.claimed_snippet == "print(1)"
    assert failure.actual_snippet is None
    assert failure.found_at_lines is None


def test_reason_code_line_out_of_range():
    failure = verify_code_finding(_finding(snippet="x", start=99, end=99), FILES)
    assert failure.reason_code is VerificationReasonCode.LINE_OUT_OF_RANGE
    assert failure.actual_snippet is None
    assert failure.found_at_lines is None


def test_reason_code_snippet_not_found():
    failure = verify_code_finding(_finding(snippet="def nowhere(): pass"), FILES)
    assert failure.reason_code is VerificationReasonCode.SNIPPET_NOT_FOUND
    assert failure.found_at_lines is None
    # The window is still recorded so a reader can see what was actually there.
    assert failure.actual_snippet == 'print("hello")'


def test_reason_code_snippet_found_elsewhere():
    failure = verify_code_finding(_finding(snippet="TARGET", start=1, end=1), OFFSET_FILES)
    assert failure.reason_code is VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE
    assert failure.found_at_lines == [8, 9, 10, 11, 12]
    assert failure.reason == (
        "snippet found at line(s) 8, 9, 10, 11, 12 (7 occurrence(s)), "
        "not at cited lines 1-1"
    )


def test_reason_code_evidence_not_confirmed():
    rf = RequirementFinding(
        id="r-bad",
        requirement_ref="REQ-1",
        requirement_text="x",
        status=RequirementStatus.PARTIAL,
        evidence=["src/missing.py"],
        explanation="e",
    )
    out = make_verify_node(_Runtime())(
        {"files": FILES, "code_findings": [], "requirement_findings": [rf]}
    )
    assert len(out["verification_failures"]) == 1
    assert out["verification_failures"][0].reason_code is VerificationReasonCode.EVIDENCE_NOT_CONFIRMED


def test_every_node_failure_has_a_reason_code():
    missing = _finding(snippet="print(1)", id="f-a")
    missing.file = "src/elsewhere.py"
    node = make_verify_node(_Runtime())
    out = node(
        {
            "files": FILES,
            "code_findings": [
                missing,
                _finding(snippet="x", start=99, end=99, id="f-b"),
                _finding(snippet="def nowhere(): pass", id="f-c"),
            ],
            "requirement_findings": [
                RequirementFinding(
                    id="r-a",
                    requirement_ref="REQ-1",
                    requirement_text="x",
                    status=RequirementStatus.PARTIAL,
                    evidence=["src/missing.py"],
                    explanation="e",
                )
            ],
        }
    )
    assert len(out["verification_failures"]) == 4
    assert all(f.reason_code is not None for f in out["verification_failures"])


def test_empty_cited_snippet_still_verifies():
    # No snippet means nothing to re-check, so the finding passes on its
    # citation alone — unchanged by T066.
    assert verify_code_finding(_finding(snippet=None, start=1, end=2), FILES) is None
    assert verify_code_finding(_finding(snippet="", start=1, end=2), FILES) is None


def test_snippet_found_elsewhere_when_snippet_overruns_cited_window():
    # The snippet really does start inside the cited window; it just runs past
    # the cited end line, which is a different (and clearer) diagnosis.
    failure = verify_code_finding(
        _finding(snippet="alpha\nbeta\ngamma", start=1, end=1), MULTILINE_FILES
    )
    assert failure.reason_code is VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE
    assert failure.found_at_lines == [1]
    assert failure.reason == "snippet starts at line 1 but extends past cited end line 1"


def test_found_at_lines_ascending_and_capped_at_five():
    failure = verify_code_finding(_finding(snippet="TARGET", start=1, end=1), OFFSET_FILES)
    found = failure.found_at_lines
    assert found == sorted(found)
    assert len(found) == FOUND_AT_MAX == 5
    # The cap is on reported lines only; the true occurrence count still shows.
    assert "(7 occurrence(s))" in failure.reason


def test_snippet_truncated_to_200_chars_with_ellipsis():
    failure = verify_code_finding(_finding(snippet="A" * 500), FILES)
    assert len(failure.claimed_snippet) == SNIPPET_MAX_CHARS == 200
    assert failure.claimed_snippet == "A" * 199 + "…"


def test_redaction_runs_before_truncation():
    # The secret starts at char 150 and ends past char 200, so a truncate-first
    # implementation would slice it in half and leak the surviving prefix.
    secret = ("s3cret" * 20)[:100]
    # The space is load-bearing: `\b` in the api_key pattern needs a non-word
    # character before "api_key".
    snippet = "A" * 149 + ' api_key = "' + secret + '"' + "B" * 50
    assert snippet.index(secret) < 200 < snippet.index(secret) + len(secret)

    failure = verify_code_finding(_finding(snippet=snippet), FILES)
    assert "[REDACTED]" in failure.claimed_snippet
    for length in range(5, len(secret) + 1):
        assert secret[:length] not in failure.claimed_snippet


def test_whitespace_drift_across_multiple_lines_verifies():
    # Same drift-tolerance guarantee as test_whitespace_drift_tolerated, over a
    # window spanning more than one line.
    assert (
        verify_code_finding(_finding(snippet='print(  "hello"  )\n  secret=42', start=2, end=3), FILES)
        is None
    )