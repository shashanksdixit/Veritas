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
        findings = llm_findings(
            runtime.llm,
            "code_quality",
            state["files"],
            state["project_context"],
            category=Category.CODE_QUALITY,
        )
        runtime.log.info(f"code-quality: {len(findings)} findings")
        return {"code_findings": findings}

    return code_quality_node