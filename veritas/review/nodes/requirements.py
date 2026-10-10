"""Requirements review node (T038, FR-006/FR-007/FR-008; B2a/B2b).

Two paths, chosen by whether the scope node extracted structured requirements
(FR-008):

* structured - every batch answers every requirement (FR-007) and the answers are
  merged into one finding per requirement; see :func:`merge_answers` for the rule.
* free text - the requirements source is handed to the reviewer whole, exactly as
  before (T038), for a project whose documentation has no parseable FR lines.

Both paths produce RequirementFinding with status + evidence; explanations are
redacted on the way in (constitution Privacy & Data Handling). Both also answer a
scope-dependent question: in PR scope a requirement the pull request carries no code
for is ``not_addressed`` rather than a ``gap``, because the PR review is asking
whether this change implements it, not whether the project does (FR-007).
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import (
    RequirementFinding,
    RequirementStatus,
    ReviewScope,
)
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
_NOT_ADDRESSED_REASON = "No code for this requirement is part of this PR."


def merge_answers(
    requirements: list[Requirement],
    payloads: list[dict],
    *,
    total_batches: int,
    not_reviewed_files: tuple[str, ...] = (),
    batch_failed: bool = False,
    pr_scope: bool = False,
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
    * else a failed batch -> unclear, naming the failed batch, whether or not the
      batches that did answer covered the whole plan
    * else in PR scope -> not_addressed, unless an in-scope file went unreviewed,
      which is still unclear
    * else gap **only** when every batch answered ``not_in_this_batch`` and no
      in-scope file went unreviewed
    * else unclear

    ``pr_scope`` is what separates the two questions a requirement answer can raise.
    A project review asks "does this codebase implement it?" and a "no" from every
    batch is a gap worth acting on. A PR review asks the narrower "does *this
    change* carry any code for it?" and a "no" means the pull request simply is not
    the place that requirement lives - not that the PR is deficient. Reporting that
    as a gap would make every PR touching a subset of a spec require the other 20
    requirements first, so the same evidence yields not_addressed instead (FR-007).
    The narrowness is why the status is scope-specific: it would be a lie about a
    project review, where the whole codebase was in scope and nothing was found.

    Not addressed is decided by *absence of a positive answer*, not by every batch
    saying "not in this batch": in PR scope a batch that answered cannot_judge has
    still shown no code for the requirement, and the PR still does not carry it. The
    coverage exceptions are kept, because both mean the tool did not look at
    everything the PR contains - an unreviewed file or a failed batch could hold the
    evidence, and that is unclear to chase rather than a confident not_addressed.

    That gap clause is why the question is asked per batch. One batch saying "not in
    here" proves nothing about the others, and neither does a file the planner never
    reached or a batch whose call failed - so those are unclear with the reason
    named, rather than a gap a reader would act on. A failed batch is checked before
    the unreviewed-files reason because its answer is missing outright, while an
    unreviewed file is one whose answer was never asked for; naming the failure is
    the more specific of the two, and it is the one that means the run was
    incomplete rather than merely the coverage.

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
        elif batch_failed:
            # A batch that failed is a batch whose "not in here" was never heard, so
            # its silence decides nothing — whichever answer the surviving batches
            # gave (FR-007).
            status = RequirementStatus.UNCLEAR
            explanation = _FAILED_BATCH_REASON
        elif pr_scope:
            # Nothing in this PR implements it and nothing was left unreviewed, so
            # the PR does not address it. An unreviewed file could still hold the
            # code, so that row stays unclear (FR-007).
            if not_reviewed_files:
                status = RequirementStatus.UNCLEAR
                explanation = _UNREVIEWED_REASON
            else:
                status = RequirementStatus.NOT_ADDRESSED
                explanation = _NOT_ADDRESSED_REASON
                evidence = []
        elif complete and not_reviewed_files:
            # Every batch said no, but there was code nobody was shown.
            status = RequirementStatus.UNCLEAR
            explanation = _UNREVIEWED_REASON
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


def _is_pr_scope(state: ReviewState) -> bool:
    """Whether this run reviews a pull request (FR-007).

    Read from state rather than configured on the node: the scope is the scope
    node's to resolve (it is what decides which files exist at all), and a run
    must not have two sources of truth for which question it is answering.
    """
    return state.get("scope") is ReviewScope.PR


def _structured_node(
    runtime, state: ReviewState, requirements: list[Requirement]
) -> dict:
    """Evaluate the extracted requirements against every batch (FR-007)."""
    plan = state.get("batch_plan")
    total_batches = len(plan.batches) if plan is not None else 0
    payloads, failed = llm_requirement_answers(
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
        batch_failed=bool(failed),
        pr_scope=_is_pr_scope(state),
    )
    counts = {status: 0 for status in RequirementStatus}
    for finding in findings:
        counts[finding.status] += 1
    runtime.log.info(
        f"requirements: {len(findings)} evaluated: "
        f"{counts[RequirementStatus.SATISFIED]} satisfied, "
        f"{counts[RequirementStatus.PARTIAL]} partial, "
        f"{counts[RequirementStatus.GAP]} gap, "
        f"{counts[RequirementStatus.UNCLEAR]} unclear, "
        f"{counts[RequirementStatus.NOT_ADDRESSED]} not addressed"
    )
    # A failed batch rides the shared FR-027 channels and also holds back every
    # gap (FR-007): a batch that failed is a batch whose "not in here" we never heard.
    return {
        "requirement_findings": findings,
        "errors": [f.message for f in failed],
        "failed_batches": failed,
    }


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
    findings = _map_gap_in_pr_scope(findings, _is_pr_scope(state))
    runtime.log.info(f"requirements: {len(findings)} findings")
    return {"requirement_findings": findings}


def _map_gap_in_pr_scope(
    findings: list[RequirementFinding], pr_scope: bool
) -> list[RequirementFinding]:
    """Report a free-text gap as not_addressed in PR scope only (FR-007).

    The reviewer answered about the files it was shown, which in PR scope are the
    PR's files. A gap there means this PR carries no code for the requirement, not
    that the project lacks it, so the status is restated and the explanation is
    replaced — the LLM's gap wording says "no code implementing this requirement",
    which a PR reader would rightly object to. Project and module reviews keep the
    gap, because there the whole codebase was in scope.

    Findings are copied, not mutated: the caller may hold the same objects.
    """
    if not pr_scope:
        return findings
    mapped: list[RequirementFinding] = []
    for finding in findings:
        if finding.status is not RequirementStatus.GAP:
            mapped.append(finding)
            continue
        mapped.append(
            finding.model_copy(
                update={
                    "status": RequirementStatus.NOT_ADDRESSED,
                    "explanation": _NOT_ADDRESSED_REASON,
                }
            )
        )
    return mapped