# Data Model: Veritas Code Review (001-code-review)

**Date**: 2026-09-16
**Branch**: 001-code-review

All models are Pydantic v2 `BaseModel` classes. They serve as the shared contract between review nodes, the verification node, and the report renderer. Field names match the spec's Key Entities and FRs.

---

## Enums

```python
from enum import Enum

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
    reaches the final report, it has necessarily passed verification -- an
    unverified LLM-identified finding is excluded, never reported (FR-013) -- so
    the same value doubles correctly as the post-verification label without a
    separate enum member."""
    SAST = "sast"
    LLM_IDENTIFIED = "llm-verified"

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
    REQUIRES_MODIFICATION = "RequiresModification"  # any error-severity CodeFinding, or any RequirementFinding with status=gap
    REQUIRES_REVIEW = "RequiresReview"                # (none of the above) and any warning-severity CodeFinding, or any RequirementFinding with status in {partial, unclear}
    CLEAN = "Clean"                                    # none of the above
```

---

## Core Entities

### ReviewRun (spec Key Entity: "Review Run")

```python
from pydantic import BaseModel, Field
from datetime import datetime

class ReviewRun(BaseModel):
    """A single invocation of Veritas."""
    id: str                          # uuid4, stable across serializations
    scope: ReviewScope
    target: str                      # path or PR url/number
    input_revision: str | None = None  # PR head SHA for scope=pr; always None for ad-hoc scopes (project/module/file) — revision tracking is a PR-only concept per constitution Principle IV (no diff-aware state between runs)
    config_hash: str                 # hash of config used (for determinism audit)
    started_at: datetime
    completed_at: datetime | None = None
    # Provenance (constitution Principle I / FR-024)
    model_name: str                  # e.g. "openai/gpt-4o-mini"
    prompt_version: str              # versioned prompt hash or tag
    # Status
    report_status: ReportStatus = ReportStatus.INCOMPLETE
    error: str | None = None         # set if LLM/provider fails mid-run (FR-027)
```

### CodeFinding (spec Key Entity: "Code Finding")

```python
class LineRange(BaseModel):
    start_line: int
    start_col: int
    end_line: int
    end_col: int

class CodeFinding(BaseModel):
    """A location-based finding with verified or SAST-grounded citation."""
    id: str                          # stable finding ID (uuid or content-hash)
    file: str                        # relative path within scope
    line_range: LineRange
    severity: Severity
    category: Category
    # FR-012 / SC-003: source label required for security findings, None otherwise
    source: FindingSource | None = None   # "sast" or "llm-verified"; None for
                                           # code_quality/test_coverage/performance
                                           # findings, since no non-SAST tool exists
                                           # today for those categories
    # Taxonomy (optional)
    owasp_id: str | None = None      # e.g. "A03:2021"
    cwe_id: str | None = None        # e.g. "CWE-78"
    # Content
    title: str
    description: str
    recommendation: str
    confidence: float                # 0.0–1.0, reviewer's confidence
    # Citation (for verification, FR-013)
    cited_snippet: str | None = None  # the code snippet the finding references
    # Suppression
    is_suppressed: bool = False
    suppression_entry: "SuppressionEntry | None" = None
```

### RequirementFinding (spec Key Entity: "Requirement Finding")

```python
class RequirementFinding(BaseModel):
    """A requirement-based finding with traceability to project docs."""
    id: str
    requirement_ref: str             # reference to requirement (title or ID)
    requirement_text: str            # the requirement statement
    status: RequirementStatus
    evidence: list[str] = Field(default_factory=list)  # file:line refs
    explanation: str
```

### SuppressionEntry (spec Key Entity: "Suppression Entry")

```python
import hashlib

def compute_fingerprint(file: str, category: str, flagged_snippet: str) -> str:
    """Fingerprint = SHA-256 of (file + category + normalized snippet).
    
    Normalization: strip leading/trailing whitespace per line,
    collapse multiple whitespace to single space, strip blank lines.
    FR-017/FR-018: keyed by code, not line numbers, so suppression
    survives unrelated edits but lapses when flagged code changes.
    """
    normalized = "\n".join(
        " ".join(line.split()) 
        for line in flagged_snippet.strip().splitlines() 
        if line.strip()
    )
    raw = f"{file}|{category}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

class SuppressionEntry(BaseModel):
    """A git-tracked allowlist record for a suppressed finding."""
    fingerprint: str                 # SHA-256 hash (see compute_fingerprint)
    file: str                        # for display; not used in matching
    category: Category
    title: str                       # human-readable description
    added_at: datetime
    reason: str | None = None        # why it was suppressed
```

### Report (spec Key Entity: "Report")

```python
class VerificationFailure(BaseModel):
    """A finding that failed FR-013 citation re-verification."""
    finding_id: str
    file: str
    line_range: LineRange
    reason: str                      # e.g. "file not found", "line mismatch"

class Summary(BaseModel):
    """Report summary counts (FR-015)."""
    total_code_findings: int
    severity_counts: dict[Severity, int]
    category_counts: dict[Category, int]
    total_requirement_findings: int
    requirement_status_counts: dict[RequirementStatus, int]
    verification_failure_count: int
    verification_failures: list[VerificationFailure] = Field(default_factory=list)
    verdict: Verdict                 # derived per FR-015; see Verdict enum

class Report(BaseModel):
    """The full review deliverable (FR-016)."""
    schema_version: str = "1.0.0"    # semver; bump on any breaking change to
                                      # Report/CodeFinding/RequirementFinding shape,
                                      # per CHANGELOG.md (constitution Principle VIII)
    run: ReviewRun
    code_findings: list[CodeFinding]
    requirement_findings: list[RequirementFinding]
    summary: Summary
    # Markdown content (generated by renderer, not stored in state)
    markdown_content: str | None = None
```

---

## State Schema (LangGraph)

```python
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class ReviewState(TypedDict):
    """Shared state flowing through the LangGraph review pipeline."""
    messages: Annotated[list, add_messages]
    scope: ReviewScope
    target: str
    # Collected findings from review nodes
    code_findings: list[CodeFinding]
    requirement_findings: list[RequirementFinding]
    # Verification
    verification_failures: list[VerificationFailure]
    # Run metadata
    run: ReviewRun
    # Phase marker (for routing)
    phase: str  # "scope" | "review" | "verify" | "write" | "done"
```

---

## Relationships

```
ReviewRun 1────* CodeFinding
ReviewRun 1────* RequirementFinding
ReviewRun 1────* SuppressionEntry (via finding matches at runtime)
ReviewRun 1────1 Report
Report contains Summary
Summary contains VerificationFailure[]

CodeFinding may match SuppressionEntry via fingerprint at render time.
SuppressionEntry is persisted to .veritas/suppressions.json (git-tracked).
```
