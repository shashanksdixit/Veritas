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
    verdict: Verdict


class Report(BaseModel):
    """The full review deliverable (FR-016)."""

    schema_version: str = SCHEMA_VERSION
    run: ReviewRun
    code_findings: list[CodeFinding] = Field(default_factory=list)
    requirement_findings: list[RequirementFinding] = Field(default_factory=list)
    summary: Summary
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