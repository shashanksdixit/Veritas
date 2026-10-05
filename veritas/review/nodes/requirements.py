"""Requirements review node (T038, FR-006/FR-007/FR-008; B2a/B2b).

Two paths, chosen by whether the scope node extracted structured requirements
(FR-008):

* structured - every batch answers every requirement (FR-007) and the answers are
  merged into one finding per requirement; see :func:`merge_answers` for the rule.
* free text - the requirements source is handed to the reviewer whole, exactly as
  before (T038), for a project whose documentation has no parseable FR lines.

Both paths produce RequirementFinding with status + evidence; explanations are
redacted on the way in (constitution Privacy & Data Handling).
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import RequirementFinding, RequirementStatus
from veritas.review.nodes.common import (
    build_requirement_finding,
    find_requirements_source,
    llm_requirement_answers,
    llm_requirement_findings,
)
from veritas.review.requirements_source import Requirement
from veritas.review.state import ReviewState

# The four answers a batch may give (FR-007). Anything else the model returns is
# treated as an omitted answer, which the merge reads as cannot_judge.
_ANSWERS = ("implemented", "partially_implemented", "not_in_this_batch", "cannot_judge")

# FR-007 caps the union of cited references; the cap is on the finding, not on one
# answer, so a requirement implemented across four batches still cites five.
MAX_EVIDENCE = 5

_GAP_EXPLANATION = "No code implementing this requirement was found in any reviewed batch."
_CANNOT_JUDGE_REASON = "The code shown could not be judged against this requirement."
_NOT_MENTIONED_REASON = (
    "Not every reviewed batch answered for this requirement, so no single status "
    "could be concluded."
)
_UNREVIEWED_REASON = (
    "Coverage was incomplete: not every in-scope file was reviewed, so a gap could "
    "not be concluded."
)
_FAILED_BATCH_REASON = (
    "Coverage was incomplete: a requirements batch failed, so a gap could not be "
    "concluded."
)


def merge_answers(
    requirements: list[Requirement],
    payloads: list[dict],
    *,
    total_batches: int,
    not_reviewed_files: tuple[str, ...] = (),
    batch_failed: bool = False,
) -> list[RequirementFinding]:
    """Collapse every batch's answers into one finding per requirement (FR-007).

    ``payloads`` is every answer object from every batch, in batch order. An id the
    requirements list does not contain is ignored (and counted by the caller):
    a model that invents "FR-999" has not been asked about a requirement and must
    not create one. An id a batch never mentions counts as cannot_judge, which is
    why the merge looks at how many answers there are and not only at what they say.

    The status rule, in order:

    * any ``implemented`` -> satisfied
    * else any ``partially_implemented`` -> partial
    * else gap **only** when every batch answered ``not_in_this_batch`` and no
      in-scope file went unreviewed and no batch failed
    * else unclear

    That gap clause is why the question is asked per batch. One batch saying "not in
    here" proves nothing about the others, and neither does a file the planner never
    reached or a batch whose call failed - so those are unclear with the reason
    named, rather than a gap a reader would act on.

    Evidence is the union of the references cited by ``implemented`` and
    ``partially_implemented`` answers, in batch order, capped at
    :data:`MAX_EVIDENCE`. A gap or an unclear finding cites nothing: there is
    nothing to point at.
    """
    answers: dict[str, list[tuple[str, dict]]] = {r.id: [] for r in requirements}
    for payload in payloads:
        identifier = str(payload.get("id", "")).strip()
        answer = str(payload.get("answer", "")).strip()
        if identifier in answers and answer in _ANSWERS:
            answers[identifier].append((answer, payload))

    findings: list[RequirementFinding] = []
    for requirement in requirements:
        given = answers[requirement.id]
        kinds = {answer for answer, _ in given}
        complete = _every_batch_answered_not_in_this_batch(kinds, given, total_batches)

        evidence: list[str] = []
        for answer, payload in given:
            if answer not in ("implemented", "partially_implemented"):
                continue
            for reference in payload.get("evidence") or []:
                reference = str(reference).strip()
                if reference and reference not in evidence:
                    evidence.append(reference)

        if "implemented" in kinds:
            status = RequirementStatus.SATISFIED
            explanation = _deciding_explanation(given, "implemented")
        elif "partially_implemented" in kinds:
            status = RequirementStatus.PARTIAL
            explanation = _deciding_explanation(given, "partially_implemented")
        elif complete and not_reviewed_files:
            # Every batch said no, but there was code nobody was shown.
            status = RequirementStatus.UNCLEAR
            explanation = _UNREVIEWED_REASON
        elif complete and batch_failed:
            status = RequirementStatus.UNCLEAR
            explanation = _FAILED_BATCH_REASON
        elif complete:
            status = RequirementStatus.GAP
            explanation = _GAP_EXPLANATION
            evidence = []
        else:
            status = RequirementStatus.UNCLEAR
            explanation = _unclear_reason(kinds, given, total_batches)

        findings.append(
            build_requirement_finding(
                {
                    "requirement_ref": requirement.id,
                    "requirement_text": requirement.text,
                    "status": status.value,
                    "evidence": evidence[:MAX_EVIDENCE],
                    "explanation": explanation,
                }
            )
        )
    return findings


def _every_batch_answered_not_in_this_batch(
    kinds: set[str], given: list[tuple[str, dict]], total_batches: int
) -> bool:
    """True only when all ``total_batches`` batches said not_in_this_batch.

    Fewer answers than batches means a batch did not mention the requirement at
    all, which FR-007 reads as cannot_judge, so it can never be a gap.
    """
    return total_batches > 0 and kinds == {"not_in_this_batch"} and len(given) == total_batches


def _deciding_explanation(given: list[tuple[str, dict]], deciding: str) -> str:
    """The explanation of the first answer that decided the status."""
    for answer, payload in given:
        if answer == deciding:
            explanation = str(payload.get("explanation", "")).strip()
            if explanation:
                return explanation
    return _CANNOT_JUDGE_REASON


def _unclear_reason(kinds: set[str], given: list[tuple[str, dict]], total_batches: int) -> str:
    """Name why nothing could be concluded (FR-007)."""
    if "cannot_judge" in kinds or not kinds:
        return _CANNOT_JUDGE_REASON
    if len(given) < total_batches:
        # A batch that did not mention this requirement at all (FR-007).
        return _NOT_MENTIONED_REASON
    return _CANNOT_JUDGE_REASON


def _no_documentation_finding() -> RequirementFinding:
    return RequirementFinding(
        requirement_ref="No requirements documentation found",
        requirement_text="(no requirements documentation present)",
        status=RequirementStatus.UNCLEAR,
        evidence=[],
        explanation=(
            "No requirements documentation was found in the reviewed "
            "scope; status is unclear rather than inventing requirements (FR-008)."
        ),
    )


def make_requirements_node(runtime) -> Callable[[ReviewState], dict]:
    def requirements_node(state: ReviewState) -> dict:
        requirements = state.get("requirements") or []
        if requirements:
            return _structured_node(runtime, state, requirements)
        return _free_text_node(runtime, state)

    return requirements_node


def _structured_node(
    runtime, state: ReviewState, requirements: list[Requirement]
) -> dict:
    """Evaluate the extracted requirements against every batch (FR-007)."""
    plan = state.get("batch_plan")
    total_batches = len(plan.batches) if plan is not None else 0
    payloads, errors = llm_requirement_answers(
        runtime.llm,
        plan,
        requirements,
        state.get("project_context"),
        log=runtime.log,
    )

    known = {r.id for r in requirements}
    unknown = sum(1 for payload in payloads if str(payload.get("id", "")).strip() not in known)
    if unknown:
        runtime.log.info(f"requirements: ignored {unknown} unknown requirement id(s)")

    findings = merge_answers(
        requirements,
        payloads,
        total_batches=total_batches,
        not_reviewed_files=tuple(plan.not_reviewed_files) if plan is not None else (),
        batch_failed=bool(errors),
    )
    counts = {status: 0 for status in RequirementStatus}
    for finding in findings:
        counts[finding.status] += 1
    runtime.log.info(
        f"requirements: {len(findings)} evaluated: "
        f"{counts[RequirementStatus.SATISFIED]} satisfied, "
        f"{counts[RequirementStatus.PARTIAL]} partial, "
        f"{counts[RequirementStatus.GAP]} gap, "
        f"{counts[RequirementStatus.UNCLEAR]} unclear"
    )
    # errors rides the shared FR-027 channel and also holds back every gap (FR-007):
    # a batch that failed is a batch whose "not in here" we never heard.
    return {"requirement_findings": findings, "errors": errors}


def _free_text_node(runtime, state: ReviewState) -> dict:
    """The T038 path: hand the reviewer the documentation and judge it as prose."""
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
        findings = [_no_documentation_finding()]
    runtime.log.info(f"requirements: {len(findings)} findings")
    return {"requirement_findings": findings}