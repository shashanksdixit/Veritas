"""Verification node (T041, FR-013 / SC-002 / SC-005).

Re-reads every non-SAST finding's cited file/line — including LLM-identified
security findings and RequirementFinding evidence citations — to confirm the
citation is real and matches the claim. Confirmed → kept; unconfirmed →
excluded + VerificationFailure recorded (never dropped silently). SAST findings
are ground truth and exempt per-finding by actual source.

Each recorded failure carries a structured cause (`reason_code`) plus the
redacted, truncated snippets and off-window line numbers that explain it (T066),
so a reader can tell *why* a claim was dropped without re-running the tool.
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
    VerificationReasonCode,
)
from veritas.review.state import ReviewState
from veritas.utils.redaction import redact_secrets

_REF_LINE = re.compile(r"^(.+?):(\d+)$")

# Snippets recorded on a VerificationFailure are redacted first, then capped at
# this many characters (FR-013). Redaction must run first: truncating first
# could cut a secret in half and let the surviving prefix through.
SNIPPET_MAX_CHARS = 200

# Upper bound on how many off-window start lines a single failure reports.
FOUND_AT_MAX = 5


def _normalize(text: str) -> str:
    """Compaction tolerant to whitespace-only drift from LLM narration.

    Every whitespace character is dropped (verified separately against the
    citation's line window), so ``print(  "hello" )`` matches ``print("hello")``
    while meaningful text differences still fail. Anchoring is preserved by the
    cited file + line range.
    """
    return "".join(text.split())


def _prepare_snippet(text: str | None) -> str | None:
    """Redact then truncate a snippet before it enters state or the report.

    ``None`` stays ``None`` so "not recorded" is distinguishable from "recorded
    as empty". Redaction runs before truncation, never after: truncating first
    would slice a secret in half and leave its unredacted prefix behind.
    """
    if text is None:
        return None
    redacted = redact_secrets(text)
    if len(redacted) <= SNIPPET_MAX_CHARS:
        return redacted
    return redacted[: SNIPPET_MAX_CHARS - 1] + "…"


def _find_snippet_start_lines(expected_normalized: str, lines: list[str]) -> tuple[list[int], int]:
    """Locate a normalized snippet in the file, ignoring whitespace.

    Returns ``(distinct start lines ascending, capped at FOUND_AT_MAX; total
    occurrences)``. Searching a whitespace-stripped copy of the file lets the
    same matcher decide both "does it match at all" and "where does it actually
    live"; the per-character line map translates an offset back to a 1-based
    line number. The search advances by one character after each hit so
    overlapping occurrences are all counted.
    """
    if not expected_normalized:
        return ([], 0)
    stripped_chars: list[str] = []
    origin_lines: list[int] = []
    for number, line in enumerate(lines, start=1):
        for char in line:
            if not char.isspace():
                stripped_chars.append(char)
                origin_lines.append(number)
    stripped = "".join(stripped_chars)

    start_lines: list[int] = []
    total = 0
    position = stripped.find(expected_normalized)
    while position != -1:
        total += 1
        if len(start_lines) < FOUND_AT_MAX:
            line_number = origin_lines[position]
            # Occurrences are visited in increasing offset order, so origin
            # lines never decrease; only an exact repeat needs dropping.
            if not start_lines or start_lines[-1] != line_number:
                start_lines.append(line_number)
        position = stripped.find(expected_normalized, position + 1)
    return (start_lines, total)


def verify_code_finding(finding: CodeFinding, files: dict[str, str]) -> VerificationFailure | None:
    """Return a VerificationFailure naming the specific cause, or None when the
    citation verifies.

    Which findings pass or fail is unchanged (T066 only records more about the
    failures): unknown file → fail; cited start line past EOF → fail; otherwise
    the whitespace-normalized cited snippet must appear in the cited window. A
    finding with no ``cited_snippet`` has nothing to check and passes.
    """
    claimed = _prepare_snippet(finding.cited_snippet)
    start = finding.line_range.start_line
    end = finding.line_range.end_line

    def failure(
        reason_code: VerificationReasonCode,
        reason: str,
        *,
        actual_snippet: str | None = None,
        found_at_lines: list[int] | None = None,
    ) -> VerificationFailure:
        return VerificationFailure(
            finding_id=finding.id,
            file=finding.file,
            line_range=finding.line_range,
            reason=reason,
            reason_code=reason_code,
            claimed_snippet=claimed,
            actual_snippet=actual_snippet,
            found_at_lines=found_at_lines,
        )

    content = files.get(finding.file)
    if content is None:
        return failure(
            VerificationReasonCode.FILE_NOT_IN_SCOPE,
            f"file {finding.file} is not in the reviewed file set",
        )
    lines = content.splitlines()
    if start > len(lines):
        return failure(
            VerificationReasonCode.LINE_OUT_OF_RANGE,
            f"cited start line {start} exceeds file length ({len(lines)} lines)",
        )
    if finding.cited_snippet:
        expected = _normalize(finding.cited_snippet)
        # Confirm the normalized snippet appears within (or covering) the cited
        # lines, tolerating whitespace-only drift from LLM narration.
        window = "\n".join(lines[start - 1 : end])
        if expected and expected not in _normalize(window):
            actual = _prepare_snippet(window)
            start_lines, total = _find_snippet_start_lines(expected, lines)
            if total == 0:
                return failure(
                    VerificationReasonCode.SNIPPET_NOT_FOUND,
                    f"snippet does not occur anywhere in {finding.file}",
                    actual_snippet=actual,
                )
            # The snippet exists, just not where it was claimed: say where.
            if start <= start_lines[0] <= end:
                reason = (
                    f"snippet starts at line {start_lines[0]} but extends past "
                    f"cited end line {end}"
                )
            else:
                located = ", ".join(str(number) for number in start_lines)
                reason = (
                    f"snippet found at line(s) {located} ({total} occurrence(s)), "
                    f"not at cited lines {start}-{end}"
                )
            return failure(
                VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE,
                reason,
                actual_snippet=actual,
                found_at_lines=start_lines,
            )
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
            failure = verify_code_finding(finding, files)
            if failure is None:
                kept.append(finding)
            else:
                failures.append(failure)
                _warn(f"verification failed: {finding.id} {finding.file} — {failure.reason}")

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
                        reason_code=VerificationReasonCode.EVIDENCE_NOT_CONFIRMED,
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