"""Unit tests — merging per-batch requirement answers into final statuses (B2b,
FR-007).

Table-driven on purpose: the rule is a decision table, and each row is one way the
code can be in the repository, so a reader can check the whole table against the
sentence in FR-007 without running anything.
"""

from __future__ import annotations

import pytest

from veritas.models.entities import RequirementStatus
from veritas.review.nodes.requirements import MAX_EVIDENCE, merge_answers
from veritas.review.requirements_source import Requirement

_ONE = [Requirement(id="FR-001", text="The system MUST ship.", file="specs/a/spec.md", line=1)]


def _answer(answer: str, *evidence: str, explanation: str = "") -> dict:
    payload = {"id": "FR-001", "answer": answer, "explanation": explanation}
    if evidence:
        payload["evidence"] = list(evidence)
    return payload


def _merge(payloads, *, total_batches=2, not_reviewed=(), batch_failed=False, pr_scope=False):
    findings = merge_answers(
        _ONE,
        payloads,
        total_batches=total_batches,
        not_reviewed_files=not_reviewed,
        batch_failed=batch_failed,
        pr_scope=pr_scope,
    )
    assert len(findings) == 1
    return findings[0]


# --- the decision table ---


def test_implemented_in_one_batch_is_satisfied():
    finding = _merge(
        [
            _answer("implemented", "src/app.py:2", explanation="shipped in main()"),
            _answer("not_in_this_batch"),
        ]
    )
    assert finding.status is RequirementStatus.SATISFIED
    assert finding.evidence == ["src/app.py:2"]
    assert finding.explanation == "shipped in main()"
    assert finding.requirement_ref == "FR-001"
    assert finding.requirement_text == "The system MUST ship."


def test_partially_implemented_with_a_negative_answer_elsewhere_is_partial():
    finding = _merge([_answer("partially_implemented", "src/app.py:9"), _answer("not_in_this_batch")])
    assert finding.status is RequirementStatus.PARTIAL
    assert finding.evidence == ["src/app.py:9"]


def test_not_in_this_batch_in_every_batch_is_a_gap():
    finding = _merge([_answer("not_in_this_batch"), _answer("not_in_this_batch")])
    assert finding.status is RequirementStatus.GAP
    assert finding.evidence == []
    assert finding.explanation == (
        "No code implementing this requirement was found in any reviewed batch."
    )


def test_not_in_this_batch_plus_cannot_judge_is_unclear():
    finding = _merge([_answer("not_in_this_batch"), _answer("cannot_judge")])
    assert finding.status is RequirementStatus.UNCLEAR
    assert "could not be judged" in finding.explanation


def test_all_negative_but_one_unreviewed_file_is_unclear_naming_the_coverage():
    finding = _merge(
        [_answer("not_in_this_batch"), _answer("not_in_this_batch")],
        not_reviewed=("src/big.py",),
    )
    assert finding.status is RequirementStatus.UNCLEAR
    assert "not every in-scope file was reviewed" in finding.explanation
    assert "gap could not be concluded" in finding.explanation


def test_all_negative_but_a_failed_batch_is_unclear_naming_the_failure():
    finding = _merge(
        [_answer("not_in_this_batch"), _answer("not_in_this_batch")],
        batch_failed=True,
    )
    assert finding.status is RequirementStatus.UNCLEAR
    assert "a requirements batch failed" in finding.explanation


def test_a_batch_that_never_mentioned_the_requirement_is_cannot_judge():
    # One batch answered, the other said nothing at all: an omission is an
    # inability to judge, not agreement that the requirement is absent.
    finding = _merge([_answer("not_in_this_batch")])
    assert finding.status is RequirementStatus.UNCLEAR
    assert "Not every reviewed batch answered" in finding.explanation


def test_an_omission_cannot_become_a_gap_however_negative_the_others_were():
    finding = _merge([_answer("not_in_this_batch")], total_batches=5)
    assert finding.status is RequirementStatus.UNCLEAR


def test_implemented_beats_a_partial_elsewhere():
    finding = _merge([_answer("implemented", "src/a.py:1"), _answer("partially_implemented", "src/b.py:4")])
    assert finding.status is RequirementStatus.SATISFIED


def test_an_unrecognised_answer_counts_as_an_omission():
    finding = _merge([_answer("not_in_this_batch"), {"id": "FR-001", "answer": "yes"}])
    assert finding.status is RequirementStatus.UNCLEAR


def test_no_batches_at_all_means_every_requirement_is_unclear():
    finding = _merge([], total_batches=0)
    assert finding.status is RequirementStatus.UNCLEAR


# --- PR scope: not_addressed, never gap (FR-007) ---


def test_pr_scope_reports_no_implementation_as_not_addressed_not_gap():
    finding = _merge(
        [_answer("not_in_this_batch"), _answer("not_in_this_batch")],
        pr_scope=True,
    )
    assert finding.status is RequirementStatus.NOT_ADDRESSED
    assert finding.evidence == []
    assert finding.explanation == "No code for this requirement is part of this PR."


def test_pr_scope_reports_cannot_judge_only_as_not_addressed():
    # The whole codebase is not in a PR, so "the code shown could not be judged"
    # says nothing about whether the PR implements the requirement — it says the PR
    # showed no code for it, which is not_addressed.
    finding = _merge([_answer("cannot_judge")], total_batches=1, pr_scope=True)
    assert finding.status is RequirementStatus.NOT_ADDRESSED


def test_pr_scope_reports_a_batch_that_never_answered_as_not_addressed():
    finding = _merge([_answer("not_in_this_batch")], total_batches=5, pr_scope=True)
    assert finding.status is RequirementStatus.NOT_ADDRESSED


def test_pr_scope_is_still_unclear_when_a_batch_failed():
    finding = _merge(
        [_answer("not_in_this_batch"), _answer("not_in_this_batch")],
        batch_failed=True,
        pr_scope=True,
    )
    assert finding.status is RequirementStatus.UNCLEAR
    assert "a requirements batch failed" in finding.explanation


def test_pr_scope_is_still_unclear_when_a_file_was_not_reviewed():
    # The unreviewed file may be where the requirement lives, so the PR review
    # genuinely does not know — that is not a confident not_addressed.
    finding = _merge(
        [_answer("not_in_this_batch"), _answer("not_in_this_batch")],
        not_reviewed=("src/big.py",),
        pr_scope=True,
    )
    assert finding.status is RequirementStatus.UNCLEAR
    assert "not every in-scope file was reviewed" in finding.explanation


def test_pr_scope_still_reports_a_positive_answer_as_before():
    satisfied = _merge(
        [_answer("implemented", "src/app.py:2"), _answer("not_in_this_batch")],
        pr_scope=True,
    )
    assert satisfied.status is RequirementStatus.SATISFIED
    assert satisfied.evidence == ["src/app.py:2"]

    partial = _merge(
        [_answer("partially_implemented", "src/app.py:9"), _answer("not_in_this_batch")],
        pr_scope=True,
    )
    assert partial.status is RequirementStatus.PARTIAL


def test_project_scope_is_unaffected_by_the_pr_rule():
    # Same answers, project scope: the whole codebase was in scope and nothing was
    # found, which is a gap a reader can act on.
    finding = _merge([_answer("not_in_this_batch"), _answer("not_in_this_batch")])
    assert finding.status is RequirementStatus.GAP
    assert finding.explanation == (
        "No code implementing this requirement was found in any reviewed batch."
    )


def test_no_batches_at_all_in_pr_scope_is_not_addressed():
    # A docs-only PR has no batch to review, and the PR question — "does this change
    # carry code for it?" — is answerable without one: it does not. Project scope
    # reads the same input as unclear (test_no_batches_at_all_means_every_requirement_is_unclear),
    # because there the answer would be about the whole codebase and nothing was read.
    finding = _merge([], total_batches=0, pr_scope=True)
    assert finding.status is RequirementStatus.NOT_ADDRESSED


def test_a_not_addressed_finding_cites_nothing_even_with_a_stray_reference():
    finding = _merge(
        [_answer("not_in_this_batch", "src/a.py:1")], total_batches=1, pr_scope=True
    )
    assert finding.status is RequirementStatus.NOT_ADDRESSED
    assert finding.evidence == []


def test_pr_scope_defaults_to_off_so_an_unpassed_scope_is_project_scope():
    findings = merge_answers(_ONE, [_answer("not_in_this_batch")], total_batches=1)
    assert findings[0].status is RequirementStatus.GAP


# --- evidence ---


def test_evidence_is_the_union_across_batches_in_batch_order():
    finding = _merge(
        [
            _answer("implemented", "src/a.py:1"),
            _answer("partially_implemented", "src/b.py:2", "src/b.py:3"),
        ]
    )
    assert finding.evidence == ["src/a.py:1", "src/b.py:2", "src/b.py:3"]


def test_evidence_is_capped_at_five():
    finding = _merge(
        [
            _answer("implemented", *[f"src/a.py:{n}" for n in range(1, 5)]),
            _answer("partially_implemented", *[f"src/b.py:{n}" for n in range(1, 4)]),
        ]
    )
    assert len(finding.evidence) == MAX_EVIDENCE
    assert finding.evidence == ["src/a.py:1", "src/a.py:2", "src/a.py:3", "src/a.py:4", "src/b.py:1"]


def test_only_implemented_and_partial_answers_contribute_evidence():
    finding = _merge(
        [
            _answer("implemented", "src/a.py:1"),
            _answer("not_in_this_batch", "src/b.py:99"),
            _answer("cannot_judge", "src/c.py:5"),
        ]
    )
    assert finding.evidence == ["src/a.py:1"]


def test_a_repeated_reference_is_listed_once():
    finding = _merge([_answer("implemented", "src/a.py:1"), _answer("implemented", "src/a.py:1")])
    assert finding.evidence == ["src/a.py:1"]


def test_a_gap_cites_nothing_even_if_an_answer_supplied_evidence():
    # The rule only collects evidence from implemented/partially_implemented, so a
    # not_in_this_batch answer carrying a stray reference cannot leak into the gap.
    finding = _merge(
        [_answer("not_in_this_batch", "src/a.py:1"), _answer("not_in_this_batch", "src/a.py:1")]
    )
    assert finding.status is RequirementStatus.GAP
    assert finding.evidence == []


# --- ids the requirements list does not contain ---


def test_an_unknown_requirement_id_is_ignored():
    payloads = [_answer("implemented", "src/a.py:1"), _answer("implemented", "src/a.py:2", explanation="x")]
    payloads[1]["id"] = "FR-999"
    finding = _merge(payloads)
    assert len(finding.evidence) == 1
    assert finding.evidence == ["src/a.py:1"]


def test_an_unknown_id_creates_no_finding():
    payloads = [{"id": "FR-999", "answer": "implemented", "evidence": ["src/a.py:1"]}]
    findings = merge_answers(_ONE, payloads, total_batches=1)
    assert [f.requirement_ref for f in findings] == ["FR-001"]
    assert findings[0].status is RequirementStatus.UNCLEAR


# --- every requirement gets a finding, in list order ---


def test_one_finding_per_requirement_in_the_extracted_order():
    requirements = [
        Requirement(id=f"FR-{n:03d}", text=f"text {n}", file="specs/a/spec.md", line=n)
        for n in (1, 2, 3)
    ]
    payloads = [
        {"id": "FR-003", "answer": "implemented", "evidence": ["src/c.py:1"]},
        {"id": "FR-001", "answer": "not_in_this_batch"},
        {"id": "FR-002", "answer": "partially_implemented", "evidence": ["src/b.py:2"]},
        {"id": "FR-002", "answer": "not_in_this_batch"},
    ]
    findings = merge_answers(requirements, payloads, total_batches=2)
    assert [f.requirement_ref for f in findings] == ["FR-001", "FR-002", "FR-003"]
    assert [f.status for f in findings] == [
        RequirementStatus.UNCLEAR,  # FR-001 only one batch answered
        RequirementStatus.PARTIAL,
        RequirementStatus.SATISFIED,
    ]


def test_the_merge_is_deterministic_for_the_same_input():
    payloads = [
        _answer("not_in_this_batch"),
        _answer("partially_implemented", "src/a.py:3"),
        _answer("cannot_judge"),
    ]
    first = _merge(payloads)
    second = _merge(list(payloads))
    # Ids are generated per finding, so everything else must match.
    assert first.model_dump(exclude={"id"}) == second.model_dump(exclude={"id"})


def test_an_explanation_is_redacted_like_every_other_node():
    # The redaction happens in build_requirement_finding, so the node's path into
    # state is covered by the same guarantee as the free-text path.
    finding = _merge(
        [
            _answer("implemented", "src/a.py:1", explanation='key api_key = "AKIAIOSFODNN7EXAMPLE"'),
            _answer("not_in_this_batch"),
        ]
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in finding.explanation
    assert "[REDACTED]" in finding.explanation


@pytest.mark.parametrize(
    ("payloads", "expected"),
    [
        ([_answer("implemented", "src/a.py:1")], RequirementStatus.SATISFIED),
        ([_answer("partially_implemented", "src/a.py:1")], RequirementStatus.PARTIAL),
        ([_answer("not_in_this_batch")], RequirementStatus.GAP),
        ([_answer("cannot_judge")], RequirementStatus.UNCLEAR),
    ],
)
def test_the_status_table(payloads, expected):
    """One row per answer shape the prompt allows, from a single batch."""
    assert _merge(payloads, total_batches=1).status is expected


def test_a_batch_answering_the_same_requirement_twice_cannot_manufacture_a_gap():
    """Defensive: the merge counts answers, so a duplicated id is not a second batch.

    Counting answers rather than distinct batches means an odd response makes a gap
    impossible, which is the safe direction - an unclear finding a reader can chase,
    not a gap the tool invented.
    """
    finding = _merge([_answer("not_in_this_batch"), _answer("not_in_this_batch")], total_batches=1)
    assert finding.status is RequirementStatus.UNCLEAR