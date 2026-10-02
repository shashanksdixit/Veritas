"""LangGraph state schema (T012) — mirrors data-model.md ReviewState."""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages

from veritas.models.entities import (
    CodeFinding,
    ExcludedFile,
    RequirementFinding,
    ReviewRun,
    ReviewScope,
    VerificationFailure,
)
from veritas.review.batching import BatchPlan


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
    verified_code_findings: list[CodeFinding]
    verified_requirement_findings: list[RequirementFinding]
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
    project_context: str | None
    degraded_sast: str | None
    sast_findings: list[dict]
    errors: Annotated[list[str], add]
    report_path: str | None
    report_markdown: str | None