"""Performance-reasoning node (T040, FR-003).

LLM reasoning only — no profiler integration (constitution YAGNI list);
category=performance; source stays None; concrete recommendations; redacted.
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import Category
from veritas.review.nodes.common import llm_findings
from veritas.review.state import ReviewState


def make_performance_node(runtime) -> Callable[[ReviewState], dict]:
    def performance_node(state: ReviewState) -> dict:
        findings = llm_findings(
            runtime.llm,
            "performance",
            state["files"],
            state["project_context"],
            category=Category.PERFORMANCE,
        )
        runtime.log.info(f"performance: {len(findings)} findings")
        return {"code_findings": findings}

    return performance_node