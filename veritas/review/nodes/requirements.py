"""Requirements review node (T038, FR-006/FR-007/FR-008).

Reads spec-kit structured docs when present else freeform PRD/markdown
fallback; produces RequirementFinding with status + evidence. When no
requirements documentation exists, reports a single ``unclear`` finding instead
of inventing requirements (edge case). Explanations are redacted.
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import RequirementFinding, RequirementStatus
from veritas.review.nodes.common import find_requirements_source, llm_requirement_findings
from veritas.review.state import ReviewState


def make_requirements_node(runtime) -> Callable[[ReviewState], dict]:
    def requirements_node(state: ReviewState) -> dict:
        files = state["files"]
        req_source = find_requirements_source(files)
        if req_source:
            runtime.log.info("requirements: documentation found in scope")
        else:
            runtime.log.info("requirements: no requirements documentation found")

        findings = llm_requirement_findings(
            runtime.llm,
            req_source,
            files,
            state["project_context"],
        )
        if not findings and not req_source:
            findings = [
                RequirementFinding(
                    requirement_ref="No requirements documentation found",
                    requirement_text="(no requirements documentation present)",
                    status=RequirementStatus.UNCLEAR,
                    evidence=[],
                    explanation=(
                        "No requirements documentation was found in the reviewed "
                        "scope; status is unclear rather than inventing requirements (FR-008)."
                    ),
                )
            ]
        runtime.log.info(f"requirements: {len(findings)} findings")
        return {"requirement_findings": findings}

    return requirements_node