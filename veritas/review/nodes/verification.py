"""Verification node (T041, FR-013 / SC-002 / SC-005).

Re-reads every non-SAST finding's cited file/line — including LLM-identified
security findings and RequirementFinding evidence citations — to confirm the
citation is real and matches the claim. Confirmed → kept; unconfirmed →
excluded + VerificationFailure recorded (never dropped silently). SAST findings
are ground truth and exempt per-finding by actual source.
"""

from __future__ import annotations

import re
from typing import Callable

from veritas.models.entities import (
    CodeFinding,
    FindingSource,
    LineRange,
    RequirementFinding,
    RequirementStatus,
    VerificationFailure,
)
from veritas.review.state import ReviewState

_REF_LINE = re.compile(r"^(.+?):(\d+)$")


def _normalize(text: str) -> str:
    """Compaction tolerant to whitespace-only drift from LLM narration.

    Every whitespace character is dropped (verified separately against the
    citation's line window), so ``print(  "hello" )`` matches ``print("hello")``
    while meaningful text differences still fail. Anchoring is preserved by the
    cited file + line range.
    """
    return "".join(text.split())


def verify_code_finding(finding: CodeFinding, files: dict[str, str]) -> str | None:
    """Return a failure reason, or None when the citation verifies."""
    content = files.get(finding.file)
    if content is None:
        return "file not found in reviewed scope"
    lines = content.splitlines()
    if finding.line_range.start_line > len(lines):
        return f"line {finding.line_range.start_line} out of range"
    if finding.cited_snippet:
        expected = _normalize(finding.cited_snippet)
        # Confirm the normalized snippet appears within (or covering) the cited
        # lines, tolerating whitespace-only drift from LLM narration.
        window = "\n".join(lines[finding.line_range.start_line - 1 : finding.line_range.end_line])
        if expected and expected not in _normalize(window):
            return "cited snippet does not match the actual code at the cited lines"
    return None


def verify_requirement_finding(rf: RequirementFinding, files: dict[str, str]) -> list[str]:
    """Return evidence refs that fail to verify (FR-013 / SC-005).

    ``unclear`` status requires no evidence and always passes.
    """
    failures: list[str] = []
    if rf.status == RequirementStatus.UNCLEAR:
        return failures
    for ref in rf.evidence:
        match = _REF_LINE.match(ref.strip())
        if match:
            path, line = match.group(1), int(match.group(2))
            content = files.get(path)
            if content is None:
                failures.append(f"{ref} (file not in scope)")
            elif line > len(content.splitlines()):
                failures.append(f"{ref} (line out of range)")
        elif ref.strip():
            content = files.get(ref.strip())
            if content is None:
                failures.append(f"{ref} (file not in scope)")
    return failures


def make_verify_node(runtime) -> Callable[[ReviewState], dict]:
    log = runtime.log

    def _warn(message: str) -> None:
        if log is not None:
            log.warn(message)

    def verify_node(state: ReviewState) -> dict:
        files = state["files"]
        kept: list[CodeFinding] = []
        failures: list[VerificationFailure] = []
        for finding in state["code_findings"]:
            if finding.source == FindingSource.SAST:
                kept.append(finding)  # ground truth (FR-013 exemption by actual source)
                continue
            reason = verify_code_finding(finding, files)
            if reason is None:
                kept.append(finding)
            else:
                failures.append(
                    VerificationFailure(
                        finding_id=finding.id,
                        file=finding.file,
                        line_range=finding.line_range,
                        reason=reason,
                    )
                )
                _warn(f"verification failed: {finding.id} {finding.file} — {reason}")

        req_kept: list[RequirementFinding] = []
        for rf in state["requirement_findings"]:
            bad_refs = verify_requirement_finding(rf, files)
            if bad_refs:
                failures.append(
                    VerificationFailure(
                        finding_id=rf.id,
                        file="",
                        line_range=LineRange(start_line=0, start_col=0, end_line=0, end_col=0),
                        reason="evidence not confirmed: " + "; ".join(bad_refs),
                    )
                )
                _warn(f"verification failed: {rf.id} {rf.requirement_ref} — {'; '.join(bad_refs)}")
            else:
                req_kept.append(rf)

        if log is not None:
            log.info(
                f"verification: {len(kept)}/{len(state['code_findings'])} code findings, "
                f"{len(req_kept)}/{len(state['requirement_findings'])} requirement findings kept"
            )
        return {
            "verified_code_findings": kept,
            "verified_requirement_findings": req_kept,
            "verification_failures": failures,
        }

    return verify_node