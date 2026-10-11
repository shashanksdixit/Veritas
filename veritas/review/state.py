"""LangGraph state schema (T012) — mirrors data-model.md ReviewState."""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages

from veritas.models.entities import (
    CodeFinding,
    ExcludedFile,
    FailedBatch,
    RequirementFinding,
    ReviewRun,
    ReviewScope,
    VerificationFailure,
)
from veritas.review.batching import BatchPlan
from veritas.review.requirements_source import Requirement


class ReviewState(TypedDict):
    """Shared state flowing through the LangGraph review pipeline."""

    messages: Annotated[list, add_messages]
    scope: ReviewScope
    target: str
    # Raw findings accumulated by parallel review nodes (add reducer).
    code_findings: Annotated[list[CodeFinding], add]
    requirement_findings: Annotated[list[RequirementFinding], add]
    # Verified findings produced by the verification node (overwrite channels —
    # the only consumer is render; FR-013 excludes unverifiable findings here).
    # None means the verification node never ran; a list (possibly empty) is its
    # verdict. Render must test for None, not for emptiness, so that "kept
    # nothing" is never mistaken for "never ran".
    verified_code_findings: list[CodeFinding] | None
    verified_requirement_findings: list[RequirementFinding] | None
    # Duplicate code findings the verification node merged after verifying
    # (FR-013), so the report can say how many were folded together. Written by
    # the verification node; 0 before it runs, like the other plain channels.
    duplicates_merged: int
    verification_failures: Annotated[list[VerificationFailure], add]
    run: ReviewRun
    phase: str
    # Files scoped for review: relative path -> decoded content.
    files: dict[str, str]
    skipped_languages: Annotated[list[str], add]
    # Files withheld before fetching by an exclusion pattern (FR-029), each with
    # the pattern that matched. The scope node is the only writer and is the
    # sole authority on the set, so this is an overwrite channel.
    excluded_files: list[ExcludedFile]
    # Planned code-review batches over the post-exclusion source files (FR-029).
    # None until the scope node builds it; every code review type uses this one
    # plan, so the scope node is the only writer.
    batch_plan: BatchPlan | None
    # One test index per batch (FR-004), keyed by the batch's 1-based index and
    # ranked for the code that batch holds. None means the scope holds no test
    # files; the scope node is the only writer.
    test_indexes: dict[int, str] | None
    # Requirement sources found in scope, in FR-008 discovery order (priority
    # then path). The scope node is the only writer; an empty list means the scope
    # holds no requirements documentation, which is different from a source that
    # exists but yielded nothing parseable.
    requirement_sources: list[str]
    # Functional requirements extracted from the spec-kit sources among them
    # (FR-008), each with its own file and line so it can be cited as evidence.
    # Empty when every source is free text.
    requirements: list[Requirement]
    project_context: str | None
    degraded_sast: str | None
    sast_findings: list[dict]
    errors: Annotated[list[str], add]
    # One record per failed LLM batch call, additive like errors. Each record's
    # message is also on errors; this keeps the detail for the report (FR-027).
    failed_batches: Annotated[list[FailedBatch], add]
    report_path: str | None
    report_markdown: str | None