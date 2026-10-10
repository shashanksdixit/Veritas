"""Pydantic v2 entity models (T005) — shared contract between review nodes,
the verification node, and the report renderer. Mirrors data-model.md."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field

from veritas.config.constants import SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ReviewScope(str, Enum):
    PROJECT = "project"
    MODULE = "module"
    FILE = "file"
    PR = "pr"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Category(str, Enum):
    CODE_QUALITY = "code_quality"
    SECURITY = "security"
    REQUIREMENT = "requirement"
    TEST_COVERAGE = "test_coverage"
    PERFORMANCE = "performance"


class FindingSource(str, Enum):
    """A CodeFinding's provenance. Only meaningful for category=SECURITY today
    (FR-012); non-security findings leave CodeFinding.source as None.
    LLM_IDENTIFIED is the label a security review node assigns at finding-creation
    time, before the verification node (FR-013) has run. By the time a finding
    reaches the final report, it has necessarily passed verification, so the same
    value doubles correctly as the post-verification label."""

    SAST = "sast"
    LLM_IDENTIFIED = "llm-verified"


class VerificationReasonCode(str, Enum):
    """Machine-readable cause of a VerificationFailure (FR-013)."""

    FILE_NOT_IN_SCOPE = "file_not_in_scope"
    LINE_OUT_OF_RANGE = "line_out_of_range"
    SNIPPET_FOUND_ELSEWHERE = "snippet_found_elsewhere"
    SNIPPET_NOT_FOUND = "snippet_not_found"
    EVIDENCE_NOT_CONFIRMED = "evidence_not_confirmed"


class RequirementStatus(str, Enum):
    SATISFIED = "satisfied"
    PARTIAL = "partial"
    GAP = "gap"
    UNCLEAR = "unclear"
    # PR scope only (FR-007): nothing in this PR implements the requirement. Not a
    # verdict input — a requirement the PR does not touch is not a reason to hold
    # the PR back, so it never reaches RequiresModification or RequiresReview.
    NOT_ADDRESSED = "not_addressed"


class ReportStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class Verdict(str, Enum):
    """Overall report verdict, derived per FR-015 from the collected findings."""

    REQUIRES_MODIFICATION = "RequiresModification"
    REQUIRES_REVIEW = "RequiresReview"
    CLEAN = "Clean"


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------

class ReviewRun(BaseModel):
    """A single invocation of Veritas."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ReviewScope
    target: str
    input_revision: str | None = None  # PR head SHA for scope=pr; None for ad-hoc scopes
    config_hash: str
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    model_name: str
    prompt_version: str
    report_status: ReportStatus = ReportStatus.INCOMPLETE
    error: str | None = None
    # Report.schema_version 1.7.0: the SAST rules source the run scanned with
    # ([security] opengrep_rules - a registry name or a local rules file or
    # directory); None when the run never reached SAST, so a report written
    # before this field existed still validates.
    sast_rules: str | None = None


class LineRange(BaseModel):
    start_line: int
    start_col: int
    end_line: int
    end_col: int


class SuppressionEntry(BaseModel):
    """A git-tracked allowlist record for a suppressed finding."""

    fingerprint: str
    file: str
    category: Category
    title: str
    added_at: datetime = Field(default_factory=datetime.now)
    reason: str | None = None


class CodeFinding(BaseModel):
    """A location-based finding with verified or SAST-grounded citation."""

    id: str = Field(default_factory=lambda: str(uuid4()).replace("-", ""))
    file: str
    line_range: LineRange
    severity: Severity
    category: Category
    source: FindingSource | None = None
    owasp_id: str | None = None
    cwe_id: str | None = None
    title: str
    description: str
    recommendation: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    cited_snippet: str | None = None
    # FR-013 citation correction — backward-compatible addition in schema 1.2.0.
    # The reviewer's original range when verification corrected the citation;
    # None when the citation verified as cited. Defaults to None, so a
    # 1.0.0/1.1.0-shaped finding still validates and renders.
    citation_adjusted_from: LineRange | None = None
    # FR-004 test-coverage severity cap - backward-compatible addition in schema
    # 1.4.0. The severity the reviewer assigned before the policy cap lowered it;
    # None when no cap applied, so a finding keeps the severity it was given.
    severity_adjusted_from: Severity | None = None
    is_suppressed: bool = False
    suppression_entry: SuppressionEntry | None = None


class RequirementFinding(BaseModel):
    """A requirement-based finding with traceability to project docs."""

    id: str = Field(default_factory=lambda: str(uuid4()).replace("-", ""))
    requirement_ref: str
    requirement_text: str
    status: RequirementStatus
    evidence: list[str] = Field(default_factory=list)
    explanation: str


class VerificationFailure(BaseModel):
    """A finding that failed FR-013 citation re-verification."""

    finding_id: str
    file: str
    line_range: LineRange
    reason: str
    # FR-013 structured detail — backward-compatible additions in schema 1.1.0.
    # All default to None, so a 1.0.0-shaped failure still validates and renders.
    # Truncation and the 5-line cap are enforced by the verification node (T066),
    # not by validators here.
    reason_code: VerificationReasonCode | None = None
    claimed_snippet: str | None = None
    actual_snippet: str | None = None
    found_at_lines: list[int] | None = None


class Summary(BaseModel):
    """Report summary counts (FR-015)."""

    total_code_findings: int
    severity_counts: dict[Severity, int] = Field(default_factory=dict)
    category_counts: dict[Category, int] = Field(default_factory=dict)
    total_requirement_findings: int
    requirement_status_counts: dict[RequirementStatus, int] = Field(default_factory=dict)
    verification_failure_count: int
    verification_failures: list[VerificationFailure] = Field(default_factory=list)
    # Duplicate code findings merged after verification (FR-013). Added in schema
    # 1.6.0, so reports written before it validate with the default of 0.
    duplicates_merged: int = 0
    verdict: Verdict


class ExcludedFile(BaseModel):
    """A path withheld from review by an exclusion pattern (FR-029)."""

    path: str
    pattern: str


class Coverage(BaseModel):
    """Code-review coverage for a run (FR-029)."""

    batch_chars: int
    max_batches: int
    batches_used: int
    reviewed_files: list[str] = Field(default_factory=list)
    split_files: list[str] = Field(default_factory=list)
    excluded_files: list[ExcludedFile] = Field(default_factory=list)
    not_reviewed_files: list[str] = Field(default_factory=list)


class FailedBatch(BaseModel):
    """One LLM batch call that failed (FR-027, FR-029).

    ``files`` and ``reason`` are already redacted when the record is built, so
    the record can be written to the report as it is.
    """

    review_type: str  # the prompt name: code_quality, security, requirements, ...
    batch: int        # 1-based position in the batch plan
    total: int        # number of batches in the plan
    files: list[str] = Field(default_factory=list)
    reason: str       # summarize_llm_error's one-line summary

    @property
    def message(self) -> str:
        """The one-line error text logged for this batch and carried on the
        shared errors channel."""
        return (
            f"{self.review_type}: batch {self.batch}/{self.total} failed "
            f"(files: {', '.join(self.files)}): {self.reason}"
        )


class Report(BaseModel):
    """The full review deliverable (FR-016)."""

    schema_version: str = SCHEMA_VERSION
    run: ReviewRun
    code_findings: list[CodeFinding] = Field(default_factory=list)
    requirement_findings: list[RequirementFinding] = Field(default_factory=list)
    summary: Summary
    # FR-029 coverage — backward-compatible addition in schema 1.3.0. Earlier
    # reports carry no coverage data, so the field defaults to None.
    coverage: Coverage | None = None
    # Schema 1.8.0: every failed LLM batch call, in full (FR-027). run.error
    # only summarises them; earlier reports have none, so it defaults to empty.
    failed_batches: list[FailedBatch] = Field(default_factory=list)
    markdown_content: str | None = None


# ---------------------------------------------------------------------------
# Suppression fingerprint (FR-017 / FR-018)
# ---------------------------------------------------------------------------

def compute_fingerprint(file: str, category: str, flagged_snippet: str) -> str:
    """Fingerprint = SHA-256 of (file + category + normalized snippet).

    Normalization: strip leading/trailing whitespace per line, collapse multiple
    whitespace to a single space, strip blank lines. Keyed by code, not line
    numbers: suppression survives unrelated edits and lapses when flagged code
    changes.
    """
    normalized = "\n".join(
        " ".join(line.split())
        for line in flagged_snippet.strip().splitlines()
        if line.strip()
    )
    raw = f"{file}|{category}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


CodeFinding.model_rebuild()