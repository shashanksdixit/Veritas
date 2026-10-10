"""Code-quality review node (T036) — bound only to read tools.

category=code_quality; CodeFinding.source stays None (no non-SAST tool exists);
every finding carries concrete recommendation text (FR-005) and passes through
redact_secrets() before entering state.
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import Category
from veritas.review.nodes.common import llm_findings
from veritas.review.state import ReviewState


def make_code_quality_node(runtime) -> Callable[[ReviewState], dict]:
    def code_quality_node(state: ReviewState) -> dict:
        findings, failed = llm_findings(
            runtime.llm,
            state["batch_plan"],
            "code_quality",
            state["project_context"],
            category=Category.CODE_QUALITY,
            log=runtime.log,
        )
        runtime.log.info(f"code-quality: {len(findings)} findings")
        # Each failed batch rides the shared FR-027 errors channel (any recorded
        # error makes the run's report status incomplete) and failed_batches,
        # which keeps the per-batch detail for the report.
        return {
            "code_findings": findings,
            "errors": [f.message for f in failed],
            "failed_batches": failed,
        }

    return code_quality_node