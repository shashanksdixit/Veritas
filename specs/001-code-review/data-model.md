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
    NOT_ADDRESSED = "not_addressed"   # PR scope only (FR-007): no code for this requirement is part of this PR; never affects the verdict (FR-015)

class ReportStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"

class Verdict(str, Enum):
    """Overall report verdict, derived per FR-015 from the collected findings."""
    REQUIRES_MODIFICATION = "RequiresModification"  # any error-severity CodeFinding, or any RequirementFinding with status=gap
    REQUIRES_REVIEW = "RequiresReview"                # (none of the above) and any warning-severity CodeFinding, or any RequirementFinding with status in {partial, unclear}
    CLEAN = "Clean"                                    # none of the above. not_addressed is in neither list: a requirement the PR does not address never changes the verdict (FR-015)

class VerificationReasonCode(str, Enum):
    """Why a VerificationFailure was raised (FR-013). The reason text names the
    specific cause; this is the stable machine-readable key for it."""
    FILE_NOT_IN_SCOPE = "file_not_in_scope"              # cited file not in the reviewed file set
    LINE_OUT_OF_RANGE = "line_out_of_range"              # cited start line exceeds the file length
    SNIPPET_FOUND_ELSEWHERE = "snippet_found_elsewhere"  # whitespace-normalized cited snippet occurs in the file, but not within the cited line window
    SNIPPET_NOT_FOUND = "snippet_not_found"              # snippet occurs nowhere in the file
    EVIDENCE_NOT_CONFIRMED = "evidence_not_confirmed"    # a RequirementFinding evidence citation could not be confirmed
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
    input_revision: str | None = None  # PR head SHA for scope=pr; for ad-hoc scopes (project/module/file) "sha256:" + the hex SHA-256 digest of the reviewed files after exclusion (FR-024). Computed from this run's input only, so no state is kept between runs (constitution Principle IV)
    config_hash: str                 # hash of config used (for determinism audit)
    started_at: datetime
    completed_at: datetime | None = None
    # Provenance (constitution Principle I / FR-024)
    model_name: str                  # e.g. "gpt-4o-mini" (the default; "openai/gpt-4o-mini" when configured through OpenRouter)
    prompt_version: str              # versioned prompt hash or tag
    # Status
    report_status: ReportStatus = ReportStatus.INCOMPLETE
    error: str | None = None         # set if LLM/provider fails mid-run (FR-027)
    # Report.schema_version 1.7.0: the SAST rules source the run scanned with
    # ([security] opengrep_rules - a registry name or a local rules file or
    # directory). None when the run never reached SAST, so a report written
    # before this field existed still validates.
    sast_rules: str | None = None
    # Report.schema_version 1.9.0 (FR-012): sast_status is "ran" (OpenGrep
    # finished and its JSON was parsed) or "not_run"; sast_result_count is the
    # number of results mapped to reviewed files, set only when it ran;
    # sast_reason is one line, set when it did not run or ran degraded: results
    # mapped to no reviewed file ("1 result(s) unmapped") and reviewed files
    # OpenGrep reported in paths.skipped, one clause per reason (e.g. "2 file(s)
    # not scanned (OpenGrep's default ignore list: tests/)"), joined by "; "
    # (T123, no schema change). All None when the run never reached SAST, so a
    # pre-1.9.0 report still validates.
    sast_status: SastStatus | None = None   # "ran" | "not_run"
    sast_result_count: int | None = None
    sast_reason: str | None = None
    # Report.schema_version 1.10.0 (FR-009): set by the context node.
    # project_context_status is "detected", "partial" or "unavailable";
    # project_context_sources names the files that contributed to the context
    # the model received (a manifest that parsed to nothing does not count);
    # project_context_reason is one line, set for partial and unavailable (an
    # absent file is never a reason by itself); project_context_origin is the
    # project root the files were read from (T119; the start directory when no
    # root was found), each source named relative to it, as the report shows
    # it: relative to the working
    # directory ("." for the directory itself), or only the directory name when
    # it lies outside it, never an absolute path; for a PR review (T120)
    # "repository root at <first 8 characters of the head SHA>", unshortened. All
    # None/empty when the run never reached the context node, so a pre-1.10.0
    # report still validates; consumers treat None as "not recorded".
    project_context_status: ProjectContextStatus | None = None  # "detected" | "partial" | "unavailable"
    project_context_sources: list[str] = []
    project_context_reason: str | None = None
    project_context_origin: str | None = None
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
    # FR-013 citation correction. Backward-compatible addition introduced in
    # Report.schema_version 1.2.0: defaults to None, so a 1.0.0/1.1.0-shaped
    # finding deserializes unchanged and still renders.
    citation_adjusted_from: "LineRange | None" = None
                                      # the original line range cited by the
                                      # reviewer, kept when verification
                                      # corrected the citation (FR-013);
                                      # None when the citation verified as cited
    # FR-004 test-coverage severity cap. Backward-compatible addition introduced
    # in Report.schema_version 1.4.0: defaults to None, so a pre-1.4.0-shaped
    # finding deserializes unchanged and still renders.
    severity_adjusted_from: "Severity | None" = None
                                      # the severity the reviewer assigned
                                      # before a policy cap lowered it (test-
                                      # coverage findings are capped at
                                      # warning); None when the finding kept
                                      # the severity it was given
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
    evidence: list[str] = Field(default_factory=list)  # "path", "path:N" or "path:A-B" refs; each confirmed only if the file was reviewed and every cited line is in 1..file length (FR-013)
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
    reason: str                      # e.g. "file not found", "line mismatch"; MUST
                                      # name the specific cause, not a generic
                                      # mismatch sentence (FR-013)
    # FR-013 structured detail. Backward-compatible additions introduced in
    # Report.schema_version 1.1.0: all four default to None, so a 1.0.0-shaped
    # failure deserializes unchanged and still renders.
    reason_code: "VerificationReasonCode | None" = None  # see VerificationReasonCode enum
    claimed_snippet: str | None = None  # the snippet the reviewer cited; redact_secrets()
                                         # then truncated to 200 chars before it
                                         # enters state or the report
    actual_snippet: str | None = None   # the text actually at the cited lines, when
                                         # those lines exist; same redaction +
                                         # 200-char truncation as claimed_snippet
    found_at_lines: list[int] | None = None  # 1-based start lines where the snippet
                                             # occurs, at most 5, ascending;
                                             # snippet_found_elsewhere only

class Summary(BaseModel):
    """Report summary counts (FR-015)."""
    total_code_findings: int
    severity_counts: dict[Severity, int]
    category_counts: dict[Category, int]
    total_requirement_findings: int
    requirement_status_counts: dict[RequirementStatus, int]
    verification_failure_count: int
    verification_failures: list[VerificationFailure] = Field(default_factory=list)
    # Duplicate code findings merged after verification (FR-013). Introduced in
    # Report.schema_version 1.6.0: earlier reports have no such count, so the
    # field defaults to 0 and a 1.5.0-shaped summary deserializes unchanged.
    duplicates_merged: int = 0
    verdict: Verdict                 # derived per FR-015; see Verdict enum

class ExcludedFile(BaseModel):
    """Exactly one of pattern and reason is set (model validator)."""
    path: str                   # relative path that was excluded
    pattern: str | None = None  # the exclusion pattern that matched (FR-029)
    reason: str | None = None   # schema 1.11.0 (T124): why a file was left
                                # out without a pattern, e.g. "content not
                                # provided by the hosting API (file over 1 MB)"
                                # for a PR file, or "file over 1 MB, not read"
                                # for a local file (T130);
                                # a pre-1.11.0 report has pattern only

class Coverage(BaseModel):
    """Code-review coverage for a run (FR-029)."""
    batch_chars: int
    max_batches: int
    batches_used: int
    reviewed_files: list[str]        # every file reviewed in full, including split files
    split_files: list[str]           # subset of reviewed_files reviewed across more than one batch
    excluded_files: list[ExcludedFile]
    not_reviewed_files: list[str]    # in scope but cut by max_batches
    unsupported_files: list[str] = []    # schema 1.12.0 (T130, FR-022): files in a
                                         # language Veritas does not review; not
                                         # excluded and not a requirement source read
    requirement_sources: list[str] = []  # schema 1.12.0 (T130, FR-008): sources read,
                                         # in FR-008 discovery order; a pre-1.12.0
                                         # report loads with both lists empty

class FailedBatch(BaseModel):
    """One LLM batch call that failed (FR-027, FR-029)."""
    review_type: str     # prompt name: code_quality, security, performance,
                         # test_coverage, requirements
    batch: int           # 1-based position in the batch plan
    total: int           # number of batches in the plan
    files: list[str]     # redacted paths of the files in the batch
    reason: str          # redacted one-line provider summary (summarize_llm_error)
    # message (derived, not serialized):
    #   "{review_type}: batch {batch}/{total} failed (files: ...): {reason}"

class Report(BaseModel):
    """The full review deliverable (FR-016)."""
    schema_version: str = "1.12.0"   # semver; bump on any breaking change to
                                      # Report/CodeFinding/RequirementFinding shape,
                                      # per CHANGELOG.md (constitution Principle VIII)
    run: ReviewRun
    code_findings: list[CodeFinding]
    requirement_findings: list[RequirementFinding]
    summary: Summary
    # FR-029 coverage. Backward-compatible addition introduced in
    # Report.schema_version 1.3.0: older reports have no coverage data.
    coverage: Coverage | None = None
    # FR-027 failed LLM batch calls, one record each. Backward-compatible
    # addition in Report.schema_version 1.8.0: older reports have none.
    # run.error only summarises these (at most 500 characters with the
    # status-line prefix); this list keeps the per-batch detail.
    failed_batches: list[FailedBatch] = []
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

### Project sources (T116)

`project_sources: ProjectSources | None` is an overwrite channel written only
by the scope node; it starts as None and the scope node sets it for every
scope, `--scope pr` included (T120). The context node renders project context
from it the same way for every scope and never reads the filesystem or the
network; it also records the `ReviewRun.project_context_*` fields from it
(T117).

`ProjectSources` (in `veritas/review/state.py`) is
`@dataclass(frozen=True, slots=True)`:

| Field | Type | Meaning |
|-------|------|---------|
| `origin` | `str` | The project root the reader walked up to, or the start directory when it found none (T119); for `--scope pr`, `repository root at <first 8 characters of the head SHA>` (T120) |
| `files` | `tuple[tuple[str, str], ...]` | `(name, text)` pairs in read order, each name the path relative to `origin` with `/` separators, for example `("src/pkg/pyproject.toml", "...")`, `("AGENTS.md", "...")` or `(".veritas/rules.md", "...")`; names are unique; `text(name)` returns one or None by exact name; `role(key)` returns the `(name, text)` whose name is `key` or ends with `/key`, or None; at most one file plays each role key (each manifest name, each conventions name and `.veritas/rules.md`; `*.csproj` files are exempt), else `ValueError` |
| `problems` | `tuple[str, ...]` | One short line per file the reader had to skip, in read order (T118): `<name> over 200000 bytes, skipped`, `<name> could not be read, skipped`, `<name> is not valid UTF-8, skipped` (pyproject.toml), each `<name>` relative to `origin` |

`files` is a tuple of pairs rather than a mapping so that nothing can change
it after the scope node builds it and so it compares, copies and pickles like
any other state value.

For `--scope pr` (T120) the scope node reads through `ScopeContext.fetcher`
only, and only the repository root, each file keeping its bare name.
Manifests come from the root at the PR head commit: the head listing
(`list_root`) says which of the named manifests exist as files, then up to
three `*.csproj` files, sorted. Conventions and `.veritas/rules.md` come from
the root at the PR base commit: the first of AGENTS.md, CONVENTIONS.md and
STYLE.md in the base listing whose text is not blank, and the rules file when
`read_optional` finds it and it is not blank. A file whose listed size is over
200,000 bytes is not fetched; GitLab lists no sizes, so there the limit is
applied to the UTF-8 length of the fetched text. pyproject.toml is fetched
with `strict_utf8`, so it decodes as the local reader decodes it. More
`problems` lines are possible for a PR: `reading <name> at the head|base commit
failed: <error>; skipped`, `reading the repository root at the head|base commit
failed: <error>; manifests not read|conventions and rules not read`,
`head commit unknown; manifests not read`, `base commit unknown; conventions and
rules not read`, `<name> changed in this PR; the base version was used`, and
`<name> added in this PR; not used until it is in the base commit`.

For a local review the scope node reads from the start directory (the
target, or the file's directory for `--scope file`, by `os.path.abspath`, links
not resolved) up to the project root (T119): the first directory at or above
it holding `.git` (a directory or a file) or a `.veritas/config.toml` file; a
bare `.veritas/` directory is not a marker. Nothing above the root is read.
With no marker before the filesystem root or within 32 levels
(`MAX_WALK_UP`), only the start directory is read, with no reason and no log
line. The nearest directory holding any manifest supplies all of them (the
named manifests, then `*.csproj` files sorted), never mixed across levels; a
manifest that exists there but cannot be used still decides the directory and
its problem is stated. The conventions file (AGENTS.md, CONVENTIONS.md,
STYLE.md in that order within a directory) and `.veritas/rules.md` are each
the nearest whose text is not blank; blank files are passed over, in the same
directory and then upward, and are not kept. A missing file is left out silently. A file
over 200,000 bytes (pyproject.toml included), an unreadable one, or a
pyproject.toml that is not valid UTF-8 is left out with a line in `problems`.
pyproject.toml is decoded as `utf-8-sig`, so a leading byte-order mark is
dropped. `project_context_problems` (in `veritas/utils/project_context.py`)
adds what only parsing can tell, `<name> is not valid TOML` or
`<name> has an unexpected [project] layout` for the pyproject.toml read
(`src/pkg/pyproject.toml is not valid TOML`; `[project]` not a table,
`requires-python` not a string, `dependencies` not a list; non-string items in
the list are skipped), and a
`<name> truncated to 4000 characters` line for conventions or rules longer
than the 4,000 characters the model receives (measured after redaction). The
context node logs each as one `[warn]` line, and they make the run's
project context partial (something still detected) or unavailable (nothing),
joined by "; " as `project_context_reason`.

---

## Node contexts

Each graph node factory receives exactly one context (FR-026), defined in
`veritas/review/contexts.py`. All three are `@dataclass(frozen=True,
slots=True)`: built once in `run_review` before the graph, never assigned to
during the run, and without a `__dict__`.

| Context | Given to | Fields |
|---------|----------|--------|
| `ReviewContext` | context, code_quality, security, requirements, test_coverage, performance, verify | `llm: LLMClient`, `log: Log` |
| `ScopeContext` | scope (graph metadata `veritas_role: "input"`) | `log: Log`, `exclude: tuple[str, ...]`, `batch_chars: int`, `max_batches: int`, `opengrep_rules: str`, `opengrep_jobs: int`, `fetcher: GitHubFetcher \| GitLabFetcher \| None`, `pr: ParsedPR \| None` |
| `RenderContext` | render (graph metadata `veritas_role: "output"`) | `log: Log`, `report_path: str`, `batch_chars: int`, `max_batches: int`, `store: SuppressionStore`, `poster: GitHubPoster \| GitLabPoster \| None`, `pr: ParsedPR \| None` |

`fetcher` and `pr` are set only for `--scope pr`; `poster` only when `--post`
is set as well. Only `RenderContext` holds the suppression store and the
poster.

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
