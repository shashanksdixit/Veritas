"""Test-coverage judgment node (T039, FR-004).

Judges whether existing tests exercise the business logic without executing the
test suite; category=test_coverage; source stays None; findings carry concrete
recommendation text and are redacted.
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import Category
from veritas.review.nodes.common import llm_findings
from veritas.review.state import ReviewState


def make_test_coverage_node(runtime) -> Callable[[ReviewState], dict]:
    def test_coverage_node(state: ReviewState) -> dict:
        findings = llm_findings(
            runtime.llm,
            "test_coverage",
            state["files"],
            state["project_context"],
            category=Category.TEST_COVERAGE,
        )
        runtime.log.info(f"test-coverage: {len(findings)} findings")
        return {"code_findings": findings}

    return test_coverage_node