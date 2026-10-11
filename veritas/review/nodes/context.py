"""Project-context node (T057, FR-009/FR-010/FR-011).

Gathers manifest versions + conventions + natural-language rules before the
review nodes run. When nothing is detectable, produces the "project context
unavailable" note (US4 edge).
"""

from __future__ import annotations

import os
from typing import Callable

from veritas.models.entities import ReviewScope
from veritas.review.state import ReviewState
from veritas.utils.project_context import build_project_context


def _context_root(runtime, state: ReviewState) -> str:
    target = state["target"]
    scope = state["scope"]
    if scope == ReviewScope.FILE:
        return os.path.dirname(os.path.abspath(target)) or os.getcwd()
    return os.path.abspath(target)


def make_context_node(runtime) -> Callable[[ReviewState], dict]:
    def node(state: ReviewState) -> dict:
        if state["scope"] == ReviewScope.PR:
            ctx_note = (
                "Project context unavailable for a remote PR review (no local "
                "manifest/conventions access); using generic guidance."
            )
            rendered = f"Note: {ctx_note}"
            runtime.log.info(ctx_note)
        else:
            ctx = build_project_context(_context_root(runtime, state))
            for entry in ctx.language_versions:
                runtime.log.info(f"Detected: {entry}")
            rendered = ctx.render()
        return {"project_context": rendered}

    return node