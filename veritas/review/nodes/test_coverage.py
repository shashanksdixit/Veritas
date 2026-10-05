"""Test-coverage judgment node (T039, FR-004).

Judges whether existing tests exercise the business logic without executing the
test suite; category=test_coverage; source stays None; findings carry concrete
recommendation text and are redacted.

Every batch carries the test index ranked for that batch (T082, FR-004). Without
it the reviewer only sees the tests that happen to share its batch, and reports
the others as missing; with no test files in scope it is told so instead, so a
gap is reported as "not in the reviewed scope" rather than as a missing test.

Test-coverage findings are capped at warning (FR-004): a missing test is a real
risk, not a defect in the code under review, and an error here would make the run's
verdict RequiresModification for something that is not broken. The cap is applied
in code rather than trusted to the prompt, and what it lowered is recorded on the
finding and counted in the log, so the report never shows a warning that the
reviewer actually called an error.
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import Category, CodeFinding, Severity
from veritas.review.nodes.common import llm_findings
from veritas.review.state import ReviewState
from veritas.review.test_index import NO_TEST_FILES_NOTICE


def _cap_severity(findings: list[CodeFinding], log) -> tuple[list[CodeFinding], int]:
    """Lower every error-severity finding to warning, recording the original.

    ``model_copy`` rather than a mutation: a finding is shared state by the time
    it leaves this node, and the original object must keep the severity the
    reviewer assigned. Returns the (possibly new) list and how many were lowered,
    so the caller can log the count only when something changed.
    """
    capped: list[CodeFinding] = []
    lowered = 0
    for finding in findings:
        if finding.severity is Severity.ERROR:
            lowered += 1
            capped.append(
                finding.model_copy(
                    update={
                        "severity": Severity.WARNING,
                        "severity_adjusted_from": Severity.ERROR,
                    }
                )
            )
        else:
            capped.append(finding)
    if lowered and log is not None:
        log.info(f"test-coverage: lowered {lowered} finding(s) from error to warning (FR-004)")
    return capped, lowered


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
        # FR-004: capped in code, after the review, before verification or the
        # summary counts it - so nothing downstream ever sees an error here.
        findings, _lowered = _cap_severity(findings, runtime.log)
        runtime.log.info(f"test-coverage: {len(findings)} findings")
        # errors rides the shared FR-027 channel: any recorded error makes the
        # run's report status incomplete.
        return {"code_findings": findings, "errors": errors}

    return test_coverage_node