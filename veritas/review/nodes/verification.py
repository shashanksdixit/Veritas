"""Verification node (T041, FR-013 / SC-002 / SC-005).

Re-reads every non-SAST finding's cited file/line — including LLM-identified
security findings and RequirementFinding evidence citations — to confirm the
citation is real and matches the claim. SAST findings are ground truth and exempt
per-finding by actual source.

The two finding shapes are treated differently on failure (FR-013), because they
mean different things when a citation is wrong:

* a code finding that cannot be confirmed is excluded and its failure recorded —
  an unlocatable claim is not a claim;
* a requirement finding is kept with its unconfirmable references removed and the
  failure recorded. A requirement is extracted from the spec, not proposed by the
  reviewer, so dropping it would let a bad citation silently delete a requirement
  from the report. When that leaves a satisfied or partial requirement with no
  evidence at all, its status becomes unclear — the requirement is still reported,
  but the claim that it is met is no longer one the code supports.

Each recorded failure carries a structured cause (`reason_code`) plus the
redacted, truncated snippets and off-window line numbers that explain it (T066),
so a reader can tell *why* a claim was dropped without re-running the tool.

Comparison is symmetric under redaction (T086, FR-013): a finding's snippet was
masked before it entered state, so the file is masked the same way before the two
are compared. Without that, every finding quoting a hardcoded secret — the ones a
report most needs — would fail to verify against its own file and be dropped.

A near-miss citation is not dropped: when the snippet occurs exactly once and
sits within two lines of the cited range, the citation is corrected to the real
location and the reviewer's original range is preserved in
``citation_adjusted_from`` (T070, FR-013). Corrections are logged and surfaced in
the report — never silent.

What survives verification is then merged (FR-013): two review types (or two
batches) report the same flagged code often enough that the report would repeat
one issue several times. Findings sharing a suppression fingerprint — file,
category and normalized snippet — are folded into one, the count is logged and
carried into the report summary, and everything else is left alone.
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
    Severity,
    VerificationFailure,
    VerificationReasonCode,
    compute_fingerprint,
)
from veritas.review.nodes.common import snippet_for
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


def _redact_lines(lines: list[str]) -> list[str]:
    """The same lines with secrets masked, one line at a time (FR-013).

    Verification compares a snippet the reviewer quoted against the file it came
    from, and that snippet was redacted before it reached state — a hardcoded
    ``api_key`` arrives as ``api_key = [REDACTED]``. Comparing that against the raw
    file text fails on exactly the findings that most need reporting, so both
    sides go through ``redact_secrets()`` and the redaction cancels out.

    Redaction is applied line by line here, not to the joined text, because
    :func:`_strip_with_line_map` and :func:`_find_snippet_spans` report findings
    by line number and must keep them aligned. The accepted consequence: a
    multi-line secret — a PEM or private-key block — is redacted as a whole when a
    reviewer quotes it in one snippet, but not when the file is redacted line by
    line, so a snippet quoting such a block can still fail to verify. That is a
    real finding being dropped rather than a secret being leaked, which is the
    right way round.
    """
    return [redact_secrets(line) for line in lines]


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


def _strip_with_line_map(lines: list[str]) -> tuple[str, list[int]]:
    """Whitespace-stripped file text plus a per-character 1-based line map.

    Shared by every matcher below so "does this snippet match" and "where does it
    live" can never drift apart: both read the same text through the same map.
    """
    stripped_chars: list[str] = []
    origin_lines: list[int] = []
    for number, line in enumerate(lines, start=1):
        for char in line:
            if not char.isspace():
                stripped_chars.append(char)
                origin_lines.append(number)
    return "".join(stripped_chars), origin_lines


def _find_snippet_start_lines(expected_normalized: str, lines: list[str]) -> tuple[list[int], int]:
    """Locate a normalized snippet in the file, ignoring whitespace.

    Returns ``(distinct start lines ascending, capped at FOUND_AT_MAX; total
    occurrences)``. Searching a whitespace-stripped copy of the file lets the
    same matcher decide both "does it match at all" and "where does it actually
    live"; the per-character line map translates an offset back to a 1-based
    line number. The search advances by one character after each hit so
    overlapping occurrences are all counted.

    ``lines`` must already be redacted (:func:`_redact_lines`) to match against a
    snippet that was redacted before it reached state (FR-013).
    """
    if not expected_normalized:
        return ([], 0)
    stripped, origin_lines = _strip_with_line_map(lines)

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

    The snippet and the window are compared after both are redacted
    (:func:`_redact_lines`, FR-013): the stored snippet is already masked, so
    matching it against raw file text would fail on every finding that quotes a
    secret — the ones a report most needs.
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
        # Both sides redacted: the stored snippet is masked, so the file must be
        # masked the same way for the two to be comparable (FR-013). `actual` below
        # stays raw here because _prepare_snippet redacts it on the way out.
        redacted_lines = _redact_lines(lines)
        # Confirm the normalized snippet appears within (or covering) the cited
        # lines, tolerating whitespace-only drift from LLM narration.
        window = "\n".join(lines[start - 1 : end])
        redacted_window = "\n".join(redacted_lines[start - 1 : end])
        if expected and expected not in _normalize(redacted_window):
            actual = _prepare_snippet(window)
            start_lines, total = _find_snippet_start_lines(expected, redacted_lines)
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


def _find_snippet_spans(expected_normalized: str, lines: list[str]) -> list[tuple[int, int]]:
    """Every occurrence of a normalized snippet as an inclusive 1-based span.

    Unlike :func:`_find_snippet_start_lines` this returns the full ``(s, e)`` span
    of each hit — ``s`` the line of the occurrence's first character, ``e`` the
    line of its last — and is deliberately uncapped, because citation correction
    needs to know that a snippet occurs *exactly* once before it will move a
    finding. Advances one character per hit so overlapping occurrences all count.

    ``lines`` must already be redacted (:func:`_redact_lines`), so the span is
    found for the same reason the window check passes (FR-013).
    """
    if not expected_normalized:
        return []
    stripped, origin_lines = _strip_with_line_map(lines)
    spans: list[tuple[int, int]] = []
    position = stripped.find(expected_normalized)
    while position != -1:
        spans.append((origin_lines[position], origin_lines[position + len(expected_normalized) - 1]))
        position = stripped.find(expected_normalized, position + 1)
    return spans


# How far (in lines) a snippet's real location may sit from the cited range and
# still count as a near-miss the reviewer meant to point at (FR-013).
CITATION_TOLERANCE_LINES = 2


def correct_citation(finding: CodeFinding, files: dict[str, str]) -> CodeFinding | None:
    """Return a copy of ``finding`` whose citation is corrected, or None.

    A citation that misses by a line or two is not a fabricated finding: the code
    the reviewer cited is real, they just pointed at it imprecisely. When the
    whitespace-normalized snippet occurs *exactly once* in the file and that
    occurrence is within :data:`CITATION_TOLERANCE_LINES` of the cited range, the
    citation is corrected to the real span ``(s, e)`` and the range the reviewer
    originally gave is preserved in ``citation_adjusted_from`` so the adjustment
    stays visible in the report (FR-013 — corrections are never silent).

    Returns None — leaving the finding to fail verification as before — when the
    snippet is absent, the file is out of scope, the occurrence is ambiguous
    (more than one match), or the real location is too far from the cited range to
    be the same citation. The input finding is never mutated.

    The file is searched line by line through :func:`_redact_lines`, so a snippet
    that quotes a secret is located in the same masked text the window check
    compares against (FR-013).
    """
    if not finding.cited_snippet:
        return None
    content = files.get(finding.file)
    if content is None:
        return None
    expected = _normalize(finding.cited_snippet)
    if not expected:
        return None

    spans = _find_snippet_spans(expected, _redact_lines(content.splitlines()))
    if len(spans) != 1:
        return None  # ambiguous: cannot attribute the snippet to one location
    start, end = spans[0]

    cited_start = finding.line_range.start_line
    cited_end = finding.line_range.end_line
    if start > cited_end + CITATION_TOLERANCE_LINES or end < cited_start - CITATION_TOLERANCE_LINES:
        return None  # too far from the cited range to be the same citation

    # Columns follow build_code_finding's convention (common.py): a citation that
    # spans whole lines records start_col=1 / end_col=1, the LLM default.
    corrected_range = LineRange(
        start_line=start,
        start_col=1,
        end_line=end,
        end_col=1,
    )
    return finding.model_copy(
        update={"line_range": corrected_range, "citation_adjusted_from": finding.line_range}
    )


# Explanation a satisfied or partial requirement falls back to once its every
# cited reference has been removed as unconfirmable (FR-013). The requirement stays
# in the report; only the claim that the code satisfies it is withdrawn.
EVIDENCE_UNCONFIRMED_EXPLANATION = "Its cited evidence could not be confirmed against the reviewed files."


def partition_requirement_evidence(
    rf: RequirementFinding, files: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Split a requirement's evidence into ``(confirmed, unconfirmed)`` (FR-013).

    The checks are the ones this node has always made on a ``file:line`` reference
    — the file must be in the reviewed set and the line must exist in it — and a
    bare filename must name a reviewed file. Each unconfirmed entry carries the
    cause as today (``"src/missing.py (file not in scope)"``) so the recorded
    VerificationFailure still names the specific reference that failed.

    An ``unclear`` requirement needs no evidence and always passes, unchanged.
    """
    confirmed: list[str] = []
    unconfirmed: list[str] = []
    if rf.status == RequirementStatus.UNCLEAR:
        return (list(rf.evidence), unconfirmed)
    for ref in rf.evidence:
        match = _REF_LINE.match(ref.strip())
        if match:
            path, line = match.group(1), int(match.group(2))
            content = files.get(path)
            if content is None:
                unconfirmed.append(f"{ref} (file not in scope)")
            elif line > len(content.splitlines()):
                unconfirmed.append(f"{ref} (line out of range)")
            else:
                confirmed.append(ref)
        elif ref.strip():
            content = files.get(ref.strip())
            if content is None:
                unconfirmed.append(f"{ref} (file not in scope)")
            else:
                confirmed.append(ref)
        else:
            confirmed.append(ref)
    return (confirmed, unconfirmed)


def verify_requirement_finding(rf: RequirementFinding, files: dict[str, str]) -> list[str]:
    """Return evidence refs that fail to verify (FR-013 / SC-005).

    Kept as the "which references failed" view of
    :func:`partition_requirement_evidence`, which is what the node consumes.
    """
    return partition_requirement_evidence(rf, files)[1]


# Which severity survives a merge: the most serious report of the same code.
_SEVERITY_RANK = {
    Severity.ERROR: 2,
    Severity.WARNING: 1,
    Severity.INFO: 0,
}


def _merge_rank(finding: CodeFinding) -> tuple[int, float, int]:
    """Rank a group's survivor: severity, then confidence, then earliest line.

    Returned as one sortable key so the order is total and the same input
    always yields the same survivor (FR-013).
    """
    return (
        _SEVERITY_RANK.get(finding.severity, 0),
        finding.confidence,
        -finding.line_range.start_line,
    )


def merge_duplicate_findings(
    findings: list[CodeFinding], files: dict[str, str]
) -> tuple[list[CodeFinding], int]:
    """Fold kept findings that share a suppression fingerprint (FR-013).

    The key is the fingerprint suppression already uses — file, category and
    the normalized flagged snippet (:func:`snippet_for`, so the finding's own
    citation when it has one) — which is exactly "the same code, flagged the
    same way". The same code under two categories (security and performance,
    say) is two findings and is never merged; so is the same code in two files.

    The survivor is the strongest report of that code: highest severity first,
    then highest confidence, then the earliest citation. Its citation is left
    exactly as verified — merging reports one duplicate, it does not re-point
    the finding that stays.

    Returns ``(merged, duplicates)`` where ``duplicates`` is how many findings
    were folded away (0 when nothing was merged), for the caller to log and to
    carry into the report summary.
    """
    groups: dict[str, list[CodeFinding]] = {}
    order: list[str] = []
    for finding in findings:
        key = compute_fingerprint(
            finding.file, finding.category.value, snippet_for(finding, files)
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(finding)

    merged: list[CodeFinding] = []
    duplicates = 0
    for key in order:
        group = groups[key]
        if len(group) == 1:
            merged.append(group[0])
            continue
        duplicates += len(group) - 1
        merged.append(max(group, key=_merge_rank))
    return (merged, duplicates)


def make_verify_node(runtime) -> Callable[[ReviewState], dict]:
    log = runtime.log

    def _warn(message: str) -> None:
        if log is not None:
            log.warn(message)

    def verify_node(state: ReviewState) -> dict:
        files = state["files"]
        kept: list[CodeFinding] = []
        failures: list[VerificationFailure] = []
        corrections = 0
        for finding in state["code_findings"]:
            if finding.source == FindingSource.SAST:
                kept.append(finding)  # ground truth (FR-013 exemption by actual source)
                continue
            failure = verify_code_finding(finding, files)
            if failure is None:
                kept.append(finding)
                continue
            # A near-miss citation is a real finding pointed at imprecisely, so
            # try to correct it rather than drop it (FR-013). Only the
            # found-elsewhere case is correctable: the snippet exists somewhere in
            # the file, which is what makes a corrected citation verifiable.
            corrected = None
            if failure.reason_code is VerificationReasonCode.SNIPPET_FOUND_ELSEWHERE:
                corrected = correct_citation(finding, files)
            if corrected is not None:
                kept.append(corrected)
                corrections += 1
                if log is not None:
                    log.info(
                        f"citation corrected: {corrected.id} {corrected.file} "
                        f"{finding.line_range.start_line}-{finding.line_range.end_line} -> "
                        f"{corrected.line_range.start_line}-{corrected.line_range.end_line}"
                    )
                continue
            failures.append(failure)
            _warn(f"verification failed: {finding.id} {finding.file} — {failure.reason}")

        # Merge only what survived verification, so a discarded citation can
        # never win a merge, and log every non-zero count (FR-013).
        kept, duplicates = merge_duplicate_findings(kept, files)
        if duplicates > 0 and log is not None:
            log.info(f"verification: merged {duplicates} duplicate finding(s)")

        req_kept: list[RequirementFinding] = []
        stripped = 0
        demoted = 0
        for rf in state["requirement_findings"]:
            confirmed, bad_refs = partition_requirement_evidence(rf, files)
            if not bad_refs:
                req_kept.append(rf)
                continue
            # The requirement came out of the spec, so it stays in the report; only
            # the references the reviewed files cannot back are removed (FR-013).
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
            kept_finding = rf.model_copy(update={"evidence": confirmed})
            if confirmed != rf.evidence:
                stripped += 1
            if not confirmed and rf.status in (RequirementStatus.SATISFIED, RequirementStatus.PARTIAL):
                # "Satisfied" with nothing left to point at is not a claim the
                # reviewed files support, so the requirement is reported as
                # unclear instead of dropped or left confidently green (FR-013).
                kept_finding = kept_finding.model_copy(
                    update={
                        "status": RequirementStatus.UNCLEAR,
                        "explanation": EVIDENCE_UNCONFIRMED_EXPLANATION,
                    }
                )
                demoted += 1
            req_kept.append(kept_finding)

        if log is not None:
            log.info(
                f"verification: {len(kept)}/{len(state['code_findings'])} code findings kept "
                f"({corrections} citation(s) corrected), "
                f"{len(req_kept)}/{len(state['requirement_findings'])} requirement findings kept "
                f"({stripped} with evidence removed, {demoted} demoted to unclear)"
            )
        return {
            "verified_code_findings": kept,
            "verified_requirement_findings": req_kept,
            "verification_failures": failures,
            "duplicates_merged": duplicates,
        }

    return verify_node