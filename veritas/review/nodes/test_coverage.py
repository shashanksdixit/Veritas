"""Test-coverage judgment node (T039, FR-004).

Judges whether existing tests exercise the business logic without executing the
test suite; category=test_coverage; source stays None; findings carry concrete
recommendation text and are redacted.

Every batch carries the test index ranked for that batch (T082, FR-004). Without
it the reviewer only sees the tests that happen to share its batch, and reports
the others as missing; with no test files in scope it is told so instead, so a
gap is reported as "not in the reviewed scope" rather than as a missing test.
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import Category
from veritas.review.nodes.common import llm_findings
from veritas.review.state import ReviewState
from veritas.review.test_index import NO_TEST_FILES_NOTICE


def make_test_coverage_node(runtime) -> Callable[[ReviewState], dict]:
    def test_coverage_node(state: ReviewState) -> dict:
        # get(), not []: a state built without the scope node's output means the
        # same as an empty index - nothing known about the tests.
        indexes = state.get("test_indexes")
        findings, errors = llm_findings(
            runtime.llm,
            state["batch_plan"],
            "test_coverage",
            state["project_context"],
            category=Category.TEST_COVERAGE,
            log=runtime.log,
            # No index at all is a different statement from an empty one: there
            # were no test files in scope, which is not the same as having none.
            extra=NO_TEST_FILES_NOTICE if indexes is None else "",
            batch_extra=indexes,
        )
        runtime.log.info(f"test-coverage: {len(findings)} findings")
        # errors rides the shared FR-027 channel: any recorded error makes the
        # run's report status incomplete.
        return {"code_findings": findings, "errors": errors}

    return test_coverage_node