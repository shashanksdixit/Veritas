"""Integration tests — per-batch requirement evaluation in the node (B2b, FR-007).

The merge rule itself is table-driven in tests/unit/test_requirements_merge.py.
What is covered here is the part the unit tests cannot see: that every batch is
asked, that a failing batch is isolated instead of fatal, and that a true gap
reaches the report as a gap while the same project with a file nobody reviewed
reaches it as unclear.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import fake_secret
from veritas.config.settings import Settings
from veritas.models.entities import RequirementStatus, ReviewScope
from veritas.review.batching import plan_batches
from veritas.review.graph import Runtime
from veritas.review.nodes.requirements import make_requirements_node
from veritas.review.requirements_source import Requirement
from veritas.utils.logging import Log

_SPEC_TEXT = "# Feature\n\n- **FR-001**: The system MUST ship.\n- **FR-002**: The system MUST warn.\n"

REQUIREMENTS = [
    Requirement(id="FR-001", text="The system MUST ship.", file="specs/001-a/spec.md", line=3),
    Requirement(id="FR-002", text="The system MUST warn.", file="specs/001-a/spec.md", line=4),
]

_BATCH_CHARS = 3000

_AWS_KEY_ID = fake_secret("AKIA", "IOSFODNN7EXAMPLE")


def _lines(count: int, tag: str) -> str:
    return "".join(f"def {tag}_{n:03d}():\n    return {n}\n" for n in range(1, count + 1))


class AnswerLLM:
    """Answers every requirement from a per-batch script; records each call.

    ``answers`` is a list, one entry per batch call, of
    ``{requirement_id: answer}``. ``fail_on`` names a 1-based call index that raises,
    which is how a provider error in one batch is simulated.
    """

    def __init__(
        self,
        answers: list[dict[str, str]],
        *,
        evidence: dict[str, list[str]] | None = None,
        explanation: str = "because",
        fail_on: int | None = None,
        extra_objects: list[dict] | None = None,
    ) -> None:
        self._answers = answers
        self._evidence = evidence or {}
        self._explanation = explanation
        self._fail_on = fail_on
        self._extra = extra_objects or []
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise RuntimeError(f"provider exploded: api_key = {_AWS_KEY_ID}")
        payload = []
        for requirement in REQUIREMENTS:
            answer = self._answers[len(self.calls) - 1][requirement.id]
            item = {
                "id": requirement.id,
                "answer": answer,
                "explanation": self._explanation,
            }
            if answer in ("implemented", "partially_implemented"):
                item["evidence"] = self._evidence.get(requirement.id, ["src/app.py:1"])
            payload.append(item)
        payload.extend(self._extra)
        return json.dumps(payload)


def _runtime(llm, events: list[str] | None = None) -> Runtime:
    log = Log(verbose=False)
    if events is not None:
        log.info = lambda message, **_kw: events.append(f"info: {message}")  # type: ignore[method-assign]
        log.warn = lambda message, **_kw: events.append(f"warn: {message}")  # type: ignore[method-assign]
    return Runtime(settings=Settings(api_key="test-key"), log=log, llm=llm)


def _project(tmp_path: Path) -> dict[str, str]:
    """Two files, each too large to share a batch, so the plan really has two."""
    (tmp_path / "src").mkdir()
    app = _lines(60, "main")
    cli = _lines(60, "run")
    (tmp_path / "src/app.py").write_text(app, encoding="utf-8")
    (tmp_path / "src/cli.py").write_text(cli, encoding="utf-8")
    return {"src/app.py": app, "src/cli.py": cli}


def _state(
    files: dict[str, str],
    *,
    batch_chars: int = _BATCH_CHARS,
    max_batches: int = 8,
    requirements: bool = True,
):
    plan, _ = plan_batches(files, batch_chars=batch_chars, max_batches=max_batches)
    state = {
        "files": files,
        "batch_plan": plan,
        "project_context": None,
        "requirements": REQUIREMENTS if requirements else [],
    }
    return state, plan


# --- one call per batch, each carrying every requirement and its own code ---


def test_every_batch_is_asked_and_each_call_carries_every_requirement(tmp_path):
    files = _project(tmp_path)
    state, plan = _state(files)
    llm = AnswerLLM(
        [{"FR-001": "implemented", "FR-002": "not_in_this_batch"}] * len(plan.batches)
    )

    make_requirements_node(_runtime(llm))(state)

    assert len(llm.calls) == len(plan.batches)
    for _system, user in llm.calls:
        for requirement in REQUIREMENTS:
            assert f"{requirement.id}: {requirement.text}" in user
    # Each call carries its own batch's code, and the batches differ.
    assert "def main_001" in llm.calls[0][1]
    assert "def main_001" not in llm.calls[1][1]
    assert "def run_001" in llm.calls[1][1]


def test_the_structured_prompt_is_used(tmp_path):
    state, plan = _state(_project(tmp_path))
    llm = AnswerLLM([{"FR-001": "implemented", "FR-002": "not_in_this_batch"}])
    make_requirements_node(_runtime(llm))(state)
    system = llm.calls[0][0]
    assert "requirements-traceability" in system
    assert "not_in_this_batch" in system


def test_the_batch_line_is_logged_before_each_call(tmp_path):
    state, plan = _state(_project(tmp_path))
    events: list[str] = []
    llm = AnswerLLM([{"FR-001": "implemented", "FR-002": "not_in_this_batch"}] * len(plan.batches))
    make_requirements_node(_runtime(llm, events))(state)
    lines = [e for e in events if e.startswith("info: requirements: batch ")]
    assert lines == [
        f"info: requirements: batch {n}/{len(plan.batches)} (1 file(s))"
        for n in range(1, len(plan.batches) + 1)
    ]


def test_the_summary_line_counts_every_status(tmp_path):
    state, plan = _state(_project(tmp_path))
    events: list[str] = []
    llm = AnswerLLM([{"FR-001": "implemented", "FR-002": "not_in_this_batch"}] * len(plan.batches))
    make_requirements_node(_runtime(llm, events))(state)
    assert (
        "info: requirements: 2 evaluated: 1 satisfied, 0 partial, 1 gap, 0 unclear, "
        "0 not addressed"
    ) in events


# --- ids the requirements list does not contain ---


def test_an_unknown_requirement_id_is_ignored_and_logged(tmp_path):
    state, plan = _state(_project(tmp_path))
    events: list[str] = []
    llm = AnswerLLM(
        [{"FR-001": "implemented", "FR-002": "not_in_this_batch"}],
        extra_objects=[
            {"id": "FR-999", "answer": "implemented", "evidence": ["src/app.py:1"]},
            {"id": "FR-998", "answer": "implemented", "evidence": ["src/app.py:2"]},
        ],
    )

    result = make_requirements_node(_runtime(llm, events))(state)

    assert len(result["requirement_findings"]) == 2
    assert [f.requirement_ref for f in result["requirement_findings"]] == ["FR-001", "FR-002"]
    assert "info: requirements: ignored 2 unknown requirement id(s)" in events


# --- failure isolation ---


def test_a_failing_batch_is_isolated_and_the_others_still_merge(tmp_path):
    files = _project(tmp_path)
    state, plan = _state(files)
    events: list[str] = []
    # Batch 2 fails; batch 1 still implemented FR-001.
    llm = AnswerLLM(
        [
            {"FR-001": "implemented", "FR-002": "not_in_this_batch"},
            {"FR-001": "implemented", "FR-002": "not_in_this_batch"},
        ],
        fail_on=2,
    )

    result = make_requirements_node(_runtime(llm, events))(state)

    statuses = {f.requirement_ref: f.status for f in result["requirement_findings"]}
    assert statuses["FR-001"] is RequirementStatus.SATISFIED
    # FR-002 was negative in the one batch that answered, and the other batch never
    # did, so it cannot be a gap.
    assert statuses["FR-002"] is RequirementStatus.UNCLEAR
    assert len(result["errors"]) == 1
    assert "batch 2/2 failed" in result["errors"][0]
    # The provider's error text is redacted before it is recorded (Privacy & Data).
    assert _AWS_KEY_ID not in result["errors"][0]
    assert "[REDACTED]" in result["errors"][0]
    assert any(e.startswith("warn: requirements: batch 2/2 failed") for e in events)
    # The same failure is recorded for the report's Failed batches section
    # (FR-027): redacted too, and its message is exactly the error above.
    (failed,) = result["failed_batches"]
    assert (failed.review_type, failed.batch, failed.total) == ("requirements", 2, 2)
    assert failed.files == list(dict.fromkeys(c.path for c in plan.batches[1].chunks))
    assert _AWS_KEY_ID not in failed.reason
    assert "[REDACTED]" in failed.reason
    assert failed.message == result["errors"][0]


def test_a_failed_batch_prevents_a_gap_even_when_the_other_batches_say_not_here(tmp_path):
    """The batch that failed never answered, so its silence is a cannot_judge.

    The other batch said "not in here" twice over, which is the strongest negative
    answer available; it still cannot support a gap, and the explanation names the
    failed batch rather than leaving the reader to infer it (FR-007).
    """
    state, plan = _state(_project(tmp_path))
    llm = AnswerLLM(
        [
            {"FR-001": "not_in_this_batch", "FR-002": "not_in_this_batch"},
            {"FR-001": "not_in_this_batch", "FR-002": "not_in_this_batch"},
        ],
        fail_on=2,
    )
    result = make_requirements_node(_runtime(llm))(state)
    assert all(f.status is RequirementStatus.UNCLEAR for f in result["requirement_findings"])
    assert all(f.evidence == [] for f in result["requirement_findings"])
    assert all(
        "a requirements batch failed" in f.explanation
        for f in result["requirement_findings"]
    )
    assert len(result["errors"]) == 1


def test_a_failed_batch_is_named_however_thoroughly_the_other_batches_answered(tmp_path):
    """Three batches, the middle one fails, the other two both say not_in_this_batch.

    Whether the surviving batches answered cannot be the deciding fact — the failed
    one never did, so that is what the explanation has to say (FR-007).
    """
    (tmp_path / "src").mkdir()
    files = {}
    for name in ("app", "cli", "web"):
        files[f"src/{name}.py"] = _lines(60, name)
        (tmp_path / "src" / f"{name}.py").write_text(files[f"src/{name}.py"], encoding="utf-8")
    state, plan = _state(files)
    assert len(plan.batches) == 3

    negative = {"FR-001": "not_in_this_batch", "FR-002": "not_in_this_batch"}
    llm = AnswerLLM([negative, negative, negative], fail_on=2)
    result = make_requirements_node(_runtime(llm))(state)

    assert [f.status for f in result["requirement_findings"]] == [
        RequirementStatus.UNCLEAR,
        RequirementStatus.UNCLEAR,
    ]
    assert all(
        "a requirements batch failed" in f.explanation
        for f in result["requirement_findings"]
    )
    assert len(result["errors"]) == 1


def test_a_failed_batch_still_lets_an_implemented_answer_win(tmp_path):
    """The failure only decides a requirement nothing claimed to implement."""
    state, plan = _state(_project(tmp_path))
    llm = AnswerLLM(
        [
            {"FR-001": "implemented", "FR-002": "not_in_this_batch"},
            {"FR-001": "implemented", "FR-002": "not_in_this_batch"},
        ],
        fail_on=2,
    )
    result = make_requirements_node(_runtime(llm))(state)
    statuses = {f.requirement_ref: f.status for f in result["requirement_findings"]}
    assert statuses["FR-001"] is RequirementStatus.SATISFIED
    assert statuses["FR-002"] is RequirementStatus.UNCLEAR
    assert "a requirements batch failed" in next(
        f.explanation for f in result["requirement_findings"] if f.requirement_ref == "FR-002"
    )


# --- the free-text path ---


def test_the_free_text_path_is_used_when_nothing_was_extracted(tmp_path):
    state, plan = _state(_project(tmp_path), requirements=False)
    state["files"] = {**state["files"], "spec.md": _SPEC_TEXT}

    class FreeTextLLM:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def complete(self, system: str, user: str) -> str:
            self.calls.append((system, user))
            return json.dumps(
                [
                    {
                        "requirement_ref": "REQ-1",
                        "requirement_text": "The system MUST ship.",
                        "status": "satisfied",
                        "evidence": ["src/app.py:1"],
                        "explanation": "main returns the value",
                    }
                ]
            )

    llm = FreeTextLLM()
    result = make_requirements_node(_runtime(llm))(state)

    assert len(llm.calls) == 1
    assert _SPEC_TEXT in llm.calls[0][1]
    assert "not_in_this_batch" not in llm.calls[0][0]
    assert [f.status for f in result["requirement_findings"]] == [RequirementStatus.SATISFIED]
    assert result.get("errors", []) == []


# --- PR scope: not_addressed instead of gap (FR-007) ---


def _pr_state(files: dict[str, str], *, requirements=True):
    state, plan = _state(files, requirements=requirements)
    state["scope"] = ReviewScope.PR
    return state, plan


def test_the_node_reports_not_addressed_for_a_pr_that_implements_nothing(tmp_path):
    state, plan = _pr_state(_project(tmp_path))
    answers = [{"FR-001": "not_in_this_batch", "FR-002": "not_in_this_batch"}] * len(plan.batches)
    result = make_requirements_node(_runtime(AnswerLLM(answers)))(state)

    assert [f.status for f in result["requirement_findings"]] == [
        RequirementStatus.NOT_ADDRESSED,
        RequirementStatus.NOT_ADDRESSED,
    ]
    assert all(
        f.explanation == "No code for this requirement is part of this PR."
        for f in result["requirement_findings"]
    )


def test_the_node_names_a_failed_batch_in_preference_to_not_addressed(tmp_path):
    state, plan = _pr_state(_project(tmp_path))
    negative = {"FR-001": "not_in_this_batch", "FR-002": "not_in_this_batch"}
    result = make_requirements_node(_runtime(AnswerLLM([negative] * 3, fail_on=2)))(state)

    assert all(f.status is RequirementStatus.UNCLEAR for f in result["requirement_findings"])
    assert all(
        "a requirements batch failed" in f.explanation for f in result["requirement_findings"]
    )


def test_the_free_text_path_reports_a_gap_as_not_addressed_in_pr_scope_only(tmp_path):
    """The reviewer answered about the PR's files, so its "gap" is about the PR."""
    files = _project(tmp_path)
    project, _ = _state(files, requirements=False)
    project["files"] = {**files, "spec.md": _SPEC_TEXT}
    pr, _ = _pr_state(files, requirements=False)
    pr["files"] = {**files, "spec.md": _SPEC_TEXT}

    def _gap_llm():
        class FreeTextLLM:
            def complete(self, system: str, user: str) -> str:
                return json.dumps(
                    [
                        {
                            "requirement_ref": "REQ-1",
                            "requirement_text": "The system MUST ship.",
                            "status": "gap",
                            "evidence": [],
                            "explanation": "No code implementing this requirement was found.",
                        }
                    ]
                )

        return FreeTextLLM()

    in_project = make_requirements_node(_runtime(_gap_llm()))(project)
    in_pr = make_requirements_node(_runtime(_gap_llm()))(pr)

    assert in_project["requirement_findings"][0].status is RequirementStatus.GAP
    assert in_project["requirement_findings"][0].explanation == (
        "No code implementing this requirement was found."
    )

    reported = in_pr["requirement_findings"][0]
    assert reported.status is RequirementStatus.NOT_ADDRESSED
    # The reviewer's own wording would read as a claim about the whole project.
    assert reported.explanation == "No code for this requirement is part of this PR."


def test_the_free_text_path_leaves_every_other_status_alone_in_pr_scope(tmp_path):
    files = _project(tmp_path)
    pr, _ = _pr_state(files, requirements=False)
    pr["files"] = {**files, "spec.md": _SPEC_TEXT}

    class FreeTextLLM:
        def complete(self, system: str, user: str) -> str:
            return json.dumps(
                [
                    {
                        "requirement_ref": "REQ-1",
                        "requirement_text": "The system MUST ship.",
                        "status": "partial",
                        "evidence": ["src/app.py:1"],
                        "explanation": "partly there",
                    },
                    {
                        "requirement_ref": "REQ-2",
                        "requirement_text": "The system MUST warn.",
                        "status": "unclear",
                        "evidence": [],
                        "explanation": "no docs",
                    },
                ]
            )

    result = make_requirements_node(_runtime(FreeTextLLM()))(pr)
    assert [f.status for f in result["requirement_findings"]] == [
        RequirementStatus.PARTIAL,
        RequirementStatus.UNCLEAR,
    ]


def test_the_node_logs_the_not_addressed_count_in_pr_scope(tmp_path):
    state, plan = _pr_state(_project(tmp_path))
    events: list[str] = []
    answers = [{"FR-001": "implemented", "FR-002": "not_in_this_batch"}] * len(plan.batches)
    make_requirements_node(_runtime(AnswerLLM(answers), events))(state)
    assert (
        "info: requirements: 2 evaluated: 1 satisfied, 0 partial, 0 gap, 0 unclear, "
        "1 not addressed"
    ) in events


# --- end to end: a real gap reaches the report as a gap ---


@pytest.mark.parametrize(
    ("unreviewed", "expected_status", "expected_verdict"),
    [
        (False, RequirementStatus.GAP, "RequiresModification"),
        (True, RequirementStatus.UNCLEAR, "Satisfies"),
    ],
)
def test_a_true_gap_only_becomes_a_gap_when_coverage_was_complete(
    tmp_path, unreviewed, expected_status, expected_verdict
):
    """The same project, one file reviewed and one dropped by the batch cap.

    A gap means "no code implementing this exists in what was reviewed", so it may
    only be claimed when the review actually saw everything. With one file left
    unreviewed the same answers must read as unclear, and the run must not demand
    a change nobody can act on.
    """
    files = _project(tmp_path)
    # max_batches=1 leaves src/cli.py unreviewed, which is the coverage gap that
    # turns a would-be gap into unclear.
    state, plan = _state(files, max_batches=1 if unreviewed else 8)
    if unreviewed:
        assert plan.not_reviewed_files == ("src/cli.py",)
    else:
        assert plan.not_reviewed_files == () and len(plan.batches) == 2

    # Every batch answers not_in_this_batch for both requirements.
    answers = [{"FR-001": "not_in_this_batch", "FR-002": "not_in_this_batch"}] * len(plan.batches)
    llm = AnswerLLM(answers)
    result = make_requirements_node(_runtime(llm))(state)

    assert all(f.status is expected_status for f in result["requirement_findings"])
    assert all(f.evidence == [] for f in result["requirement_findings"])
    if unreviewed:
        assert all(
            "not every in-scope file was reviewed" in f.explanation
            for f in result["requirement_findings"]
        )

    verdict = _verdict_from_findings(result["requirement_findings"], result.get("errors", []))
    assert verdict == expected_verdict


def _verdict_from_findings(findings, errors: list[str]) -> str:
    """The report status the findings imply (FR-027), computed as the render node does."""
    if errors:
        return "Incomplete"
    blocking = [f for f in findings if f.status in (RequirementStatus.GAP, RequirementStatus.PARTIAL)]
    return "RequiresModification" if blocking else "Satisfies"