"""Suppression resolution (T016) — match a suppression command to findings.

Resolution inputs (per FR-017): a stable finding ID from the report, or a
filename plus line reference. Rules (spec edge case + contracts/cli.md):
  * unambiguous single match → suppress it;
  * no match → clear diagnostic, exit code 1;
  * several matches → do NOT suppress, emit a stderr diagnostic listing the
    ambiguous candidate matches, exit code 1 (no interactive prompt).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veritas.models.entities import CodeFinding, Report


@dataclass
class SuppressionResolution:
    finding: CodeFinding | None = None
    candidates: list[CodeFinding] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.finding is not None:
            return "matched"
        if len(self.candidates) > 1:
            return "ambiguous"
        return "no_match"


def resolve_by_id(report: Report, finding_id: str) -> SuppressionResolution:
    matches = [f for f in report.code_findings if f.id == finding_id]
    if len(matches) == 1:
        return SuppressionResolution(finding=matches[0])
    return SuppressionResolution(candidates=matches)


def resolve_by_location(report: Report, file: str, line: int) -> SuppressionResolution:
    matches = [
        f
        for f in report.code_findings
        if f.file == file
        and f.line_range.start_line <= line <= f.line_range.end_line
    ]
    if len(matches) == 1:
        return SuppressionResolution(finding=matches[0])
    return SuppressionResolution(candidates=matches)