"""Project-context node (T057, FR-009/FR-010/FR-011).

Renders manifest versions + conventions + natural-language rules before the
review nodes run. When nothing is detectable, produces the "project context
unavailable" note (US4 edge).

The node never touches the filesystem (T116): the scope node reads the files
into the ``project_sources`` channel, and this node only renders them.

It also records on the run whether project context was detected, partial or
unavailable, and why (T117, FR-009), so the report can say so. The node runs
alone between scope and the fan-out, so nothing else writes ``run`` then.
Each problem behind a partial or unavailable status (a file too large,
unreadable, not valid UTF-8 or TOML, or a truncation) is also logged as one
``[warn]`` line (T118).
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import ProjectContextStatus, ReviewRun, ReviewScope
from veritas.review.contexts import ReviewContext
from veritas.review.state import ProjectSources, ReviewState
from veritas.utils.project_context import (
    build_project_context,
    contributing_sources,
    display_origin,
    project_context_problems,
)

# The one-line reasons recorded on the run (FR-009). An absent file is never a
# reason by itself: these name what the run could not use as a whole.
NOTHING_FOUND_REASON = "no manifest, conventions file or rules file found"
PR_NOT_READ_REASON = "not read for PR reviews yet"


def project_context_record(scope: ReviewScope, sources: ProjectSources | None) -> dict:
    """The ``ReviewRun`` fields stating the project context this review used (FR-009).

    PR review: unavailable (T119 reads the files for PRs). Local review: the
    files that contributed, and detected when there are any. Problems (from
    ``project_context_problems``: reading, parsing and truncation) make it
    partial when something was still detected and unavailable when not, with
    the problems joined by "; " as the reason.
    """
    if scope == ReviewScope.PR or sources is None:
        return {
            "project_context_status": ProjectContextStatus.UNAVAILABLE,
            "project_context_sources": [],
            "project_context_reason": PR_NOT_READ_REASON,
            "project_context_origin": None,
        }
    used = contributing_sources(sources)
    problems = project_context_problems(sources)
    if problems:
        status = ProjectContextStatus.PARTIAL if used else ProjectContextStatus.UNAVAILABLE
        reason: str | None = "; ".join(problems)
    elif used:
        status, reason = ProjectContextStatus.DETECTED, None
    else:
        status, reason = ProjectContextStatus.UNAVAILABLE, NOTHING_FOUND_REASON
    return {
        "project_context_status": status,
        "project_context_sources": used,
        "project_context_reason": reason,
        "project_context_origin": display_origin(sources.origin),
    }


def make_context_node(ctx: ReviewContext) -> Callable[[ReviewState], dict]:
    def node(state: ReviewState) -> dict:
        if state["scope"] == ReviewScope.PR:
            ctx_note = (
                "Project context unavailable for a remote PR review (no local "
                "manifest/conventions access); using generic guidance."
            )
            rendered = f"Note: {ctx_note}"
            ctx.log.info(ctx_note)
        else:
            sources = state["project_sources"]
            project = build_project_context(sources)
            for entry in project.language_versions:
                ctx.log.info(f"Detected: {entry}")
            for problem in project_context_problems(sources):
                ctx.log.warn(f"Project context: {problem}")
            rendered = project.render()
        run: ReviewRun = state["run"].model_copy(
            update=project_context_record(state["scope"], state.get("project_sources"))
        )
        return {"project_context": rendered, "run": run}

    return node
