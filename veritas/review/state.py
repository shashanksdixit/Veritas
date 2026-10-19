"""LangGraph state schema (T012) — mirrors data-model.md ReviewState."""

from __future__ import annotations

from dataclasses import dataclass
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
from veritas.utils.project_context import ROLE_KEYS, plays_role


@dataclass(frozen=True, slots=True)
class ProjectSources:
    """The project-context files the scope node read, for the context node (T116).

    ``origin`` says where they came from: the project root the reader walked
    up to, or the start directory when it found none (T119); for a PR,
    ``repository root at <head sha first 8>`` (T120). ``files`` holds
    ``(name, text)`` pairs in the order they were read, each name the file's
    path relative to ``origin`` with ``/`` separators (``AGENTS.md``,
    ``src/pkg/pyproject.toml``). It is a tuple of pairs rather than a dict, or a
    ``MappingProxyType`` over one, because a tuple cannot be changed in place
    by anyone, compares and hashes by value, and can be copied or pickled like
    any other state value, which a ``MappingProxyType`` cannot. The file count
    is a handful, so looking a name up with ``text()`` by a linear scan costs
    nothing. ``problems`` holds one short line per file the reader had to
    skip (too large, unreadable, pyproject.toml not valid UTF-8; T118) and,
    for a PR, per failed API read or rules file the PR changes (T120), for
    the report and the log.

    The parsers find a file by its role (``role``): ``pyproject.toml`` is
    played by ``pyproject.toml`` or ``src/pkg/pyproject.toml``. At most one
    file may play each role in ``ROLE_KEYS``, so a lookup never has to choose;
    ``*.csproj`` files are exempt, since several in one directory are normal.
    """

    origin: str
    files: tuple[tuple[str, str], ...]
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Coerce to tuples so a caller passing lists still gets an object
        # nothing can change afterwards.
        files = tuple((name, text) for name, text in self.files)
        names = [name for name, _text in files]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate project source names: {names}")
        for key in ROLE_KEYS:
            playing = [name for name in names if plays_role(name, key)]
            if len(playing) > 1:
                raise ValueError(f"more than one project source plays {key}: {playing}")
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "problems", tuple(self.problems))

    def text(self, name: str) -> str | None:
        """The text read for ``name``, or None when it was not read."""
        for candidate, text in self.files:
            if candidate == name:
                return text
        return None

    def role(self, key: str) -> tuple[str, str] | None:
        """The ``(name, text)`` of the file playing role ``key`` (T119), or None.

        ``key`` is a name in ``ROLE_KEYS``; ``__post_init__`` makes sure at
        most one file plays it.
        """
        for name, text in self.files:
            if plays_role(name, key):
                return name, text
        return None


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
    # the pattern that matched, then PR files whose content the hosting API
    # withheld, each with its reason (T124). The scope node is the only writer
    # and is the sole authority on the set, so this is an overwrite channel.
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
    # Project-context files (manifests, conventions, rules) read by the scope
    # node, the only writer (T116). None for a PR review, and before the scope
    # node runs; the context node renders it without touching the filesystem.
    project_sources: ProjectSources | None
    project_context: str | None
    degraded_sast: str | None
    sast_findings: list[dict]
    errors: Annotated[list[str], add]
    # One record per failed LLM batch call, additive like errors. Each record's
    # message is also on errors; this keeps the detail for the report (FR-027).
    failed_batches: Annotated[list[FailedBatch], add]
    report_path: str | None
    report_markdown: str | None