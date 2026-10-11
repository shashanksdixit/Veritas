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
        findings, failed = llm_findings(
            runtime.llm,
            state["batch_plan"],
            "performance",
            state["project_context"],
            category=Category.PERFORMANCE,
            log=runtime.log,
        )
        runtime.log.info(f"performance: {len(findings)} findings")
        # Each failed batch rides the shared FR-027 errors channel (any recorded
        # error makes the run's report status incomplete) and failed_batches,
        # which keeps the per-batch detail for the report.
        return {
            "code_findings": findings,
            "errors": [f.message for f in failed],
            "failed_batches": failed,
        }

    return performance_node