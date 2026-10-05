"""Integration tests — FR-013 citation re-verification (T041, SC-002/SC-005).

Covers the structured failure detail added in T066: `reason_code`, redacted +
truncated `claimed_snippet` / `actual_snippet`, and `found_at_lines`.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from veritas.models.entities import (
    Category,
    CodeFinding,
    FindingSource,
    LineRange,
    RequirementFinding,
    RequirementStatus,
    Severity,
    VerificationReasonCode,
    compute_fingerprint,
)
from veritas.review.nodes import verification as verification_module
from veritas.review.nodes.common import build_code_finding
from veritas.review.nodes.verification import (
    FOUND_AT_MAX,
    SNIPPET_MAX_CHARS,
    _find_snippet_spans,
    correct_citation,
    make_verify_node,
    partition_requirement_evidence,
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
# T089 — a bad reference must not delete the requirement (FR-013)
# ---------------------------------------------------------------------------


def _requirement(status, evidence, ref="FR-001", id="rf-1"):
    return RequirementFinding(
        id=id,
        requirement_ref=ref,
        requirement_text="the system must log to stdout",
        status=status,
        evidence=list(evidence),
        explanation="the logger writes to stdout in every entry point",
    )


def test_partition_keeps_the_good_reference_and_names_the_bad_one():
    rf = _requirement(RequirementStatus.SATISFIED, ["src/app.py:2", "src/missing.py:1"])
    confirmed, unconfirmed = partition_requirement_evidence(rf, FILES)
    assert confirmed == ["src/app.py:2"]
    assert unconfirmed == ["src/missing.py:1 (file not in scope)"]


def test_one_bad_reference_keeps_the_requirement_satisfied_with_the_good_one():
    log = _RecordingLog()
    rf = _requirement(RequirementStatus.SATISFIED, ["src/app.py:2", "src/app.py:99"])
    out = make_verify_node(_NodeRuntime(log))(
        {"files": FILES, "code_findings": [], "requirement_findings": [rf]}
    )
    kept = out["verified_requirement_findings"]
    assert len(kept) == 1
    assert kept[0].status is RequirementStatus.SATISFIED
    assert kept[0].evidence == ["src/app.py:2"]
    assert kept[0].explanation == rf.explanation
    assert len(out["verification_failures"]) == 1
    failure = out["verification_failures"][0]
    assert failure.reason_code is VerificationReasonCode.EVIDENCE_NOT_CONFIRMED
    assert failure.finding_id == rf.id
    assert "src/app.py:99 (line out of range)" in failure.reason
    assert any("1 with evidence removed" in line for line in log.info_lines)


def test_every_reference_bad_keeps_the_requirement_as_unclear():
    rf = _requirement(
        RequirementStatus.SATISFIED, ["src/missing.py:1", "src/other.py:4"], id="rf-all-bad"
    )
    out = make_verify_node(_NodeRuntime(_RecordingLog()))(
        {"files": FILES, "code_findings": [], "requirement_findings": [rf]}
    )
    kept = out["verified_requirement_findings"]
    assert len(kept) == 1
    assert kept[0].status is RequirementStatus.UNCLEAR
    assert kept[0].evidence == []
    assert kept[0].explanation == "Its cited evidence could not be confirmed against the reviewed files."
    assert len(out["verification_failures"]) == 1


def test_a_partial_requirement_left_with_no_evidence_is_demoted_too():
    rf = _requirement(RequirementStatus.PARTIAL, ["src/missing.py:1"], ref="FR-002", id="rf-partial")
    out = make_verify_node(_NodeRuntime(_RecordingLog()))(
        {"files": FILES, "code_findings": [], "requirement_findings": [rf]}
    )
    kept = out["verified_requirement_findings"][0]
    assert kept.status is RequirementStatus.UNCLEAR
    assert kept.evidence == []


def test_a_gap_or_unclear_finding_is_left_alone():
    gap = _requirement(RequirementStatus.GAP, [], ref="FR-003", id="rf-gap")
    gap.explanation = "No code implementing this requirement was found in any reviewed batch."
    unclear = _requirement(RequirementStatus.UNCLEAR, ["src/missing.py:1"], ref="FR-004", id="rf-unc")
    out = make_verify_node(_NodeRuntime(_RecordingLog()))(
        {"files": FILES, "code_findings": [], "requirement_findings": [gap, unclear]}
    )
    assert out["verified_requirement_findings"] == [gap, unclear]
    assert out["verification_failures"] == []


def test_every_requirement_reaches_the_report_even_with_bad_evidence():
    findings = [
        _requirement(RequirementStatus.SATISFIED, ["src/app.py:2", "src/missing.py:1"], ref="FR-001", id="rf-a"),
        _requirement(RequirementStatus.SATISFIED, ["src/missing.py:1"], ref="FR-002", id="rf-b"),
        _requirement(RequirementStatus.GAP, [], ref="FR-003", id="rf-c"),
        _requirement(RequirementStatus.UNCLEAR, [], ref="FR-004", id="rf-d"),
    ]
    out = make_verify_node(_NodeRuntime(_RecordingLog()))(
        {"files": FILES, "code_findings": [], "requirement_findings": findings}
    )
    kept = out["verified_requirement_findings"]
    assert [f.requirement_ref for f in kept] == ["FR-001", "FR-002", "FR-003", "FR-004"]
    assert len(out["verification_failures"]) == 2


def test_the_rendered_report_has_one_heading_per_extracted_requirement(tmp_path, settings):
    """End to end, through run_review: what FR-013 protects is what a reader sees.

    The node-level tests above can only see what the verify node returns. Here a
    fake LLM answers three extracted requirements — one with a good and a bad
    reference, one whose only reference is bad, one with no evidence at all — and
    the written Markdown must still carry exactly one heading per requirement the
    scope node extracted.
    """
    from tests.conftest import FakeLLM

    from veritas.models.entities import ReviewScope
    from veritas.review.graph import run_review
    from veritas.review.requirements_source import extract_requirements

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        'import os\nprint("hello")\n', encoding="utf-8"
    )
    feature = tmp_path / "specs" / "001-demo"
    feature.mkdir(parents=True)
    spec_text = (
        "# Feature\n"
        "\n"
        "- **FR-001**: The system MUST log to stdout.\n"
        "- **FR-002**: The system MUST log to stderr.\n"
        "- **FR-003**: The system MUST exit cleanly.\n"
    )
    (feature / "spec.md").write_text(spec_text, encoding="utf-8")
    extracted, _ = extract_requirements("specs/001-demo/spec.md", spec_text)
    assert [r.id for r in extracted] == ["FR-001", "FR-002", "FR-003"]

    def _answer(identifier, evidence):
        return {
            "id": identifier,
            "answer": "implemented" if evidence else "not_in_this_batch",
            "evidence": evidence,
            "explanation": "the code shows it",
        }

    llm = FakeLLM(
        {
            "extracted from the project's spec": json.dumps(
                [
                    _answer("FR-001", ["src/app.py:2", "src/gone.py:1"]),
                    _answer("FR-002", ["src/gone.py:1"]),
                    _answer("FR-003", []),
                ]
            )
        }
    )
    outcome = run_review(settings, ReviewScope.PROJECT, str(tmp_path), llm=llm)
    assert outcome.exit_code == 0

    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert re.findall(r"^### (FR-\d+) — ", md, flags=re.MULTILINE) == [
        r.id for r in extracted
    ]
    # The good reference is kept and the bad one is gone from the finding...
    assert "### FR-001 — *satisfied*" in md
    assert "- `src/app.py:2`" in md
    # ...while the requirement left with nothing is reported as unclear, not dropped.
    assert "### FR-002 — *unclear*" in md
    assert (
        "Its cited evidence could not be confirmed against the reviewed files." in md
    )
    # Both bad references are named in the verification-failure notes instead.
    assert md.count("src/gone.py:1 (file not in scope)") == 2
    assert "### FR-003 — *gap*" in md


# ---------------------------------------------------------------------------
# T086 — redaction is applied to both sides of the comparison (FR-013)
# ---------------------------------------------------------------------------

# A file that really does hardcode a key. The finding's snippet reached state
# already masked (build_code_finding redacts), so verification compares a masked
# snippet against this raw text; before T086 the comparison failed and dropped the
# exact finding a security review most needs to report.
_API_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_FILES = {
    "src/conf.py": f'import os\nAWS = "aws_access_key_id = {_API_KEY}"\nprint(AWS)\n',
}

# What the masked snippet looks like once it has been through the same redaction:
# the key itself is replaced, the assignment around it survives.
REDACTED_SECRET_LINE = 'AWS = "aws_access_key_id = [REDACTED]"'


def test_finding_quoting_a_secret_still_verifies():
    finding = _finding(snippet=REDACTED_SECRET_LINE, start=2, end=2, id="f-secret")
    finding.file = "src/conf.py"
    assert verify_code_finding(finding, SECRET_FILES) is None


def test_the_raw_secret_never_reaches_the_finding_that_is_kept():
    finding = _finding(snippet=f'AWS = "aws_access_key_id = {_API_KEY}"', start=2, end=2, id="f-secret")
    finding.file = "src/conf.py"
    # build_code_finding is what mangles the snippet on the way in; this is the
    # state verification then compares against.
    built = build_code_finding(
        {
            "file": "src/conf.py",
            "start_line": 2,
            "end_line": 2,
            "severity": "error",
            "title": "Hardcoded AWS key",
            "description": f"the key {_API_KEY} is committed in source",
            "recommendation": "Read the key from the environment.",
            "cited_snippet": f'AWS = "aws_access_key_id = {_API_KEY}"',
        },
        category=Category.SECURITY,
    )
    assert _API_KEY not in built.cited_snippet
    assert _API_KEY not in built.description
    assert verify_code_finding(built, SECRET_FILES) is None


def test_a_secret_snippet_is_found_elsewhere_when_the_citation_is_off():
    # The reviewer quoted the masked key but cited the wrong line. Correction has
    # to search the redacted file too, or it cannot find the snippet it verified.
    finding = _finding(snippet=REDACTED_SECRET_LINE, start=1, end=1, id="f-off")
    finding.file = "src/conf.py"
    assert verify_code_finding(finding, SECRET_FILES) is not None
    corrected = correct_citation(finding, SECRET_FILES)
    assert corrected is not None
    assert corrected.line_range.start_line == 2
    assert _API_KEY not in corrected.cited_snippet


def test_a_secret_free_snippet_is_unaffected_by_the_redaction():
    # Secret-free files redact to themselves, so every T066 outcome still holds.
    assert verify_code_finding(_finding(snippet='print("hello")'), FILES) is None
    failure = verify_code_finding(_finding(snippet="def totally_different(): pass"), FILES)
    assert failure is not None
    assert failure.reason_code == VerificationReasonCode.SNIPPET_NOT_FOUND


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


# ---------------------------------------------------------------------------
# T070 — citation correction (FR-013)
# ---------------------------------------------------------------------------

def _at(line: int) -> str:
    """A file where line `line` holds NEEDLE and every other line is filler."""
    return "\n".join("NEEDLE" if number == line else "filler" for number in range(1, 301))


# A snippet occupying lines 19-23, so a citation to line 19 alone misses it.
BLOCK_FILES = {"src/app.py": "\n".join(
    ["filler"] * 18 + ["def handler():", "    x = 1", "    y = 2", "    z = 3", "    return x"] + ["filler"] * 5
)}

BLOCK_SNIPPET = "def handler():\n    x = 1\n    y = 2\n    z = 3\n    return x"


def _correcting_finding(snippet: str, start: int, end: int, id: str = "f-corr") -> CodeFinding:
    finding = _finding(snippet=snippet, start=start, end=end, id=id)
    finding.file = "src/app.py"
    return finding


def test_find_snippet_spans_returns_first_and_last_line():
    # The needle is whitespace-normalized by the caller, matching the existing
    # _find_snippet_start_lines contract.
    lines = BLOCK_FILES["src/app.py"].splitlines()
    assert _find_snippet_spans("x=1y=2z=3", lines) == [(20, 22)]
    # Uncapped, unlike found_at_lines: every occurrence is reported.
    repeated = "dup\n" * 4
    assert _find_snippet_spans("dup", repeated.splitlines()) == [(1, 1), (2, 2), (3, 3), (4, 4)]
    assert _find_snippet_spans("", lines) == []
    assert _find_snippet_spans("absent", lines) == []


def test_citation_corrected_when_snippet_overlaps_cited_range():
    # Snippet spans 19-23; the reviewer cited only line 19.
    finding = _correcting_finding(BLOCK_SNIPPET, 19, 19)
    corrected = correct_citation(finding, BLOCK_FILES)
    assert corrected is not None
    assert (corrected.line_range.start_line, corrected.line_range.end_line) == (19, 23)
    assert corrected.citation_adjusted_from is not None
    assert (
        corrected.citation_adjusted_from.start_line,
        corrected.citation_adjusted_from.end_line,
    ) == (19, 19)


def test_citation_corrected_when_cited_range_one_line_short():
    # Cited 60-66 but the snippet really runs to line 67.
    files = {"src/app.py": "\n".join(
        ["filler"] * 59 + [f"line_{n} = {n}" for n in range(60, 68)] + ["filler"] * 3
    )}
    snippet = "\n".join(f"line_{n} = {n}" for n in range(60, 68))
    finding = _correcting_finding(snippet, 60, 66, id="f-short")
    corrected = correct_citation(finding, files)
    assert corrected is not None
    assert (corrected.line_range.start_line, corrected.line_range.end_line) == (60, 67)
    assert (corrected.citation_adjusted_from.start_line, corrected.citation_adjusted_from.end_line) == (60, 66)


def test_citation_corrected_within_two_lines_above_cited_line():
    # Snippet at 239, cited 240 — one line past the cited start.
    finding = _correcting_finding("NEEDLE", 240, 240, id="f-above")
    files = {"src/app.py": _at(239)}
    corrected = correct_citation(finding, files)
    assert corrected is not None
    assert (corrected.line_range.start_line, corrected.line_range.end_line) == (239, 239)


def test_citation_corrected_exactly_two_lines_away():
    # Snippet at 10, cited 12 — the boundary case, inclusive per FR-013.
    files = {"src/app.py": _at(10)}
    finding = _correcting_finding("NEEDLE", 12, 12, id="f-2away")
    corrected = correct_citation(finding, files)
    assert corrected is not None
    assert (corrected.line_range.start_line, corrected.line_range.end_line) == (10, 10)


def test_citation_not_corrected_three_lines_away():
    # Snippet at 10, cited 13 — one line beyond tolerance, so the finding fails
    # verification as before.
    files = {"src/app.py": _at(10)}
    finding = _correcting_finding("NEEDLE", 13, 13, id="f-3away")
    assert correct_citation(finding, files) is None
    failure = verify_code_finding(finding, files)
    assert failure is not None
    assert failure.reason_code == VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE


def test_citation_not_corrected_when_snippet_occurs_twice():
    # Two NEEDLEs, one of them adjacent to the cited line. Ambiguity wins over
    # proximity: the reviewer may have meant either occurrence.
    content = "\n".join(
        "NEEDLE" if number in (11, 12) else "filler" for number in range(1, 21)
    )
    files = {"src/app.py": content}
    finding = _correcting_finding("NEEDLE", 10, 10, id="f-dup")
    assert correct_citation(finding, files) is None
    failure = verify_code_finding(finding, files)
    assert failure is not None
    assert failure.reason_code == VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE


def test_citation_not_corrected_without_snippet_or_in_scope_file():
    assert correct_citation(_finding(snippet=None), FILES) is None
    assert correct_citation(_finding(snippet=""), FILES) is None
    orphan = _finding(snippet="NEEDLE", start=1, end=1, id="f-orphan")
    orphan.file = "src/missing.py"
    assert correct_citation(orphan, FILES) is None


def test_correct_citation_does_not_mutate_input_finding():
    finding = _correcting_finding(BLOCK_SNIPPET, 19, 19, id="f-pure")
    before = finding.model_copy(deep=True)
    corrected = correct_citation(finding, BLOCK_FILES)
    assert corrected is not None
    assert finding == before
    assert finding.line_range.start_line == 19
    assert finding.line_range.end_line == 19
    assert finding.citation_adjusted_from is None


def test_corrected_finding_keeps_id_and_suppression_fingerprint():
    finding = _correcting_finding(BLOCK_SNIPPET, 19, 19, id="f-fp")
    before = compute_fingerprint(finding.file, finding.category.value, finding.cited_snippet)
    corrected = correct_citation(finding, BLOCK_FILES)
    assert corrected is not None
    assert corrected.id == "f-fp"
    assert compute_fingerprint(
        corrected.file, corrected.category.value, corrected.cited_snippet
    ) == before


def test_corrected_finding_uses_whole_line_columns():
    # Matches build_code_finding's convention (common.py): start_col=1/end_col=1.
    corrected = correct_citation(_correcting_finding(BLOCK_SNIPPET, 19, 19), BLOCK_FILES)
    assert corrected is not None
    assert (corrected.line_range.start_col, corrected.line_range.end_col) == (1, 1)


class _RecordingLog:
    """Captures log calls so tests can assert on the literal emitted text."""

    def __init__(self) -> None:
        self.info_lines: list[str] = []
        self.warn_lines: list[str] = []

    def info(self, message: str, **structured) -> None:
        self.info_lines.append(message)

    def warn(self, message: str, **structured) -> None:
        self.warn_lines.append(message)

    def error(self, message: str, **structured) -> None:
        pass


@dataclass
class _NodeRuntime:
    log: _RecordingLog | None = None


def test_verify_node_corrects_citation_and_reports_it():
    # One file holding three distinct situations at known line numbers:
    # NEEDLE at 10 (verifies as cited), the block snippet at 19-23 (cited 19
    # only — correctable), and FARCODE at 30 cited at 33 (three lines away —
    # uncorrectable).
    body = ["filler"] * 9 + ["NEEDLE"] + ["filler"] * 8 + BLOCK_SNIPPET.splitlines()
    mixed = body + ["filler"] * 6 + ["FARCODE"] + ["filler"] * 6
    assert mixed[9] == "NEEDLE" and mixed[29] == "FARCODE"
    files = {"src/app.py": "\n".join(mixed)}

    log = _RecordingLog()
    verified = _correcting_finding("NEEDLE", 10, 10, id="f-verified")
    correctable = _correcting_finding(BLOCK_SNIPPET, 19, 19, id="f-correctable")
    uncorrectable = _correcting_finding("FARCODE", 33, 33, id="f-uncorrectable")
    out = make_verify_node(_NodeRuntime(log))(
        {"files": files, "code_findings": [verified, correctable, uncorrectable], "requirement_findings": []}
    )
    assert len(out["verified_code_findings"]) == 2
    assert len(out["verification_failures"]) == 1
    assert out["verification_failures"][0].finding_id == "f-uncorrectable"

    by_id = {f.id: f for f in out["verified_code_findings"]}
    assert by_id["f-verified"].line_range.start_line == 10
    assert (by_id["f-correctable"].line_range.start_line, by_id["f-correctable"].line_range.end_line) == (19, 23)
    assert by_id["f-correctable"].citation_adjusted_from is not None

    assert log.info_lines == [
        "citation corrected: f-correctable src/app.py 19-19 -> 19-23",
        "verification: 2/3 code findings kept (1 citation(s) corrected), 0/0 requirement findings kept "
        "(0 with evidence removed, 0 demoted to unclear)",
    ]


def test_verify_node_keeps_sast_without_correcting(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("correct_citation must not run for SAST findings")

    monkeypatch.setattr(verification_module, "correct_citation", _fail_if_called)
    sast = _finding(snippet="NEEDLE", start=13, end=13, id="f-sast", source=FindingSource.SAST)
    sast.file = "src/app.py"
    before = sast.model_copy(deep=True)
    out = make_verify_node(_NodeRuntime(_RecordingLog()))(
        {
            "files": {"src/app.py": _at(10)},
            "code_findings": [sast],
            "requirement_findings": [],
        }
    )
    assert len(out["verified_code_findings"]) == 1
    assert out["verification_failures"] == []
    assert out["verified_code_findings"][0] == before


def test_verify_node_correction_only_applies_to_found_elsewhere(monkeypatch):
    # A snippet that occurs nowhere is a fabricated claim, not a near miss: the
    # correction path must not be consulted at all.
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("correction must not run for snippet_not_found")

    monkeypatch.setattr(verification_module, "correct_citation", _fail_if_called)
    fabricated = _correcting_finding("def nowhere(): pass", 1, 1, id="f-fake")
    out = make_verify_node(_NodeRuntime(_RecordingLog()))(
        {"files": BLOCK_FILES, "code_findings": [fabricated], "requirement_findings": []}
    )
    assert out["verified_code_findings"] == []
    assert out["verification_failures"][0].reason_code == VerificationReasonCode.SNIPPET_NOT_FOUND


def test_verification_summary_reports_zero_corrections_when_none_needed():
    log = _RecordingLog()
    out = make_verify_node(_NodeRuntime(log))(
        {"files": FILES, "code_findings": [_finding(snippet='print("hello")', id="f-ok")], "requirement_findings": []}
    )
    assert len(out["verified_code_findings"]) == 1
    assert log.info_lines == [
        "verification: 1/1 code findings kept (0 citation(s) corrected), 0/0 requirement findings kept "
        "(0 with evidence removed, 0 demoted to unclear)"
    ]