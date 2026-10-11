"""Integration tests — relevance-ordered test index for the test-coverage review
(T082, FR-004).

A batch only holds part of the scope, so the test-coverage reviewer used to see
the tests that happened to share its batch and report the rest as missing. These
tests pin the fix and its boundaries:

* the scope node builds one index per batch from the same post-exclusion source
  files the plan was built from, and each batch's index is ranked for its own
  code, so two batches get two different orderings;
* ``llm_findings`` hands each batch only its own entry, and every caller that
  passes nothing sends the message it sent before;
* no other review type is sent an index, and a scope with no tests says so
  instead of implying no tests exist;
* end to end, a suite too large for one index still gets the tests of the code in
  front of it - the regression that motivated ranking rather than sorting.
"""

from __future__ import annotations

import json
import inspect
import re
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Lock

import pytest

from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.models.entities import (
    Category,
    Report,
    ReviewRun,
    ReviewScope,
    Severity,
    Verdict,
)
from veritas.models.entities import compute_fingerprint
from veritas.review.graph import Runtime, run_review
from veritas.review.nodes import scope as scope_module
from veritas.review.nodes.code_quality import make_code_quality_node
from veritas.review.nodes.common import load_prompt, llm_findings
from veritas.review.nodes.performance import make_performance_node
from veritas.review.nodes.scope import make_scope_node
from veritas.review.nodes.security import make_security_node
from veritas.review.nodes.test_coverage import _cap_severity, make_test_coverage_node
from veritas.review.test_index import (
    INDEX_HEADER,
    NO_TEST_FILES_NOTICE,
    build_batch_test_indexes,
)
from veritas.security.opengrep import OpengrepResult
from veritas.utils.logging import Log

_CODE_MARKER = "Code to review:\n\n"

# Two 25-line files render to ~594-char blocks, so at batch_chars=1000 one block
# does not fit beside the other and src/app.py takes a batch of its own.
_BATCH_CHARS = 1000


def _lines(count: int, tag: str) -> str:
    return "".join(f"{tag}_{n:06d} = {n}\n" for n in range(1, count + 1))


class RecordingLLM:
    """Records every user message a review node sends and returns no findings.

    ``run_review`` drives the five review nodes from one superstep, so the calls
    arrive on several threads and their order is not reproducible; the system
    prompt is what identifies the caller. ``users`` restores batch order by
    reading the batch header out of the message, which is what a node-level test
    wants to assert on.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._lock = Lock()
        self.model_name = "recording"

    def complete(self, system: str, user: str) -> str:
        with self._lock:
            self.calls.append((system, user))
        return "[]"

    def users(self, review: str | None = None) -> list[str]:
        if review is None:
            return [user for _system, user in self.calls]
        prompt = load_prompt(review)
        return [user for system, user in self.calls if system == prompt]

    def code_bodies(self, review: str | None = None) -> list[str]:
        return [user.split(_CODE_MARKER, 1)[1] for user in self.users(review)]

    def index_of(self, user: str) -> str:
        """Whatever the node put before the code: index, notice, or nothing."""
        return user.split(_CODE_MARKER, 1)[0]

    def index_text(self, user: str) -> str:
        """The index alone, without the shared context a node puts around it."""
        prefix = self.index_of(user)
        start = prefix.find(INDEX_HEADER)
        return prefix[start:] if start >= 0 else prefix

    def batch_order(self, review: str | None = None) -> list[str]:
        """The users of ``review``, in the order the planner numbered the batches."""
        users = self.users(review)

        def batch_number(user: str) -> str:
            match = re.search(r"^### FILE: (.+)$", user.split(_CODE_MARKER, 1)[1], re.M)
            return match.group(1)

        return sorted(users, key=batch_number)


def _stub_sast(monkeypatch) -> None:
    monkeypatch.setattr(
        scope_module,
        "collect_sast",
        lambda files, **_kwargs: OpengrepResult(findings=[], rules="r"),
    )


def _runtime(llm, **settings_kwargs) -> Runtime:
    return Runtime(
        settings=Settings(api_key="test-key", **settings_kwargs),
        log=Log(verbose=False),
        llm=llm,
    )


def _state(target: str) -> dict:
    run = ReviewRun(
        scope=ReviewScope.PROJECT,
        target=target,
        config_hash="h",
        model_name="m",
        prompt_version="1.5.0",
        started_at=datetime.now(),
    )
    return {
        "messages": [],
        "scope": ReviewScope.PROJECT,
        "target": target,
        "code_findings": [],
        "requirement_findings": [],
        "verified_code_findings": [],
        "verified_requirement_findings": [],
        "verification_failures": [],
        "run": run,
        "phase": "scope",
        "files": {},
        "skipped_languages": [],
        "excluded_files": [],
        "batch_plan": None,
        "test_indexes": None,
        "project_context": None,
        "degraded_sast": None,
        "sast_findings": [],
        "errors": [],
        "report_path": "",
        "report_markdown": None,
    }


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    """Two source files and two test files, small enough to batch predictably."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text(_lines(25, "app"), encoding="utf-8")
    (tmp_path / "src" / "util.py").write_text(_lines(25, "util"), encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_app_starts():\n    pass\n\n\ndef test_app_stops():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_util.py").write_text(
        "class TestUtil:\n    def test_helper_math(self):\n        pass\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def crowded_tree(project_tree: Path) -> Path:
    """``project_tree`` plus twelve filler test files that sort before its tests."""
    for index in range(12):
        (project_tree / "tests" / f"test_filler_{index:02d}.py").write_text(
            "def test_filler():\n    pass\n", encoding="utf-8"
        )
    return project_tree


def _scoped(
    project_tree: Path,
    monkeypatch,
    *,
    exclude: list[str] | None = None,
    batch_chars: int = _BATCH_CHARS,
) -> dict:
    """Run the real scope node and return the state a review node would receive."""
    _stub_sast(monkeypatch)
    runtime = _runtime(RecordingLLM(), exclude=exclude or [], batch_chars=batch_chars)
    result = make_scope_node(runtime)(_state(str(project_tree)))
    return {**_state(str(project_tree)), **result}


def _indexed_paths(index: str) -> list[str]:
    return [
        line[2:].split(":", 1)[0].split(" (", 1)[0]
        for line in index.splitlines()
        if line.startswith("- ")
    ]


# --- the scope node builds one index per batch ---


def test_scope_state_carries_one_index_per_batch(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch)
    plan = state["batch_plan"]
    assert len(plan.batches) == 2

    indexes = state["test_indexes"]
    assert sorted(indexes) == [batch.index for batch in plan.batches]
    for index in indexes.values():
        assert index.startswith(f"{INDEX_HEADER}\n")
        assert "Code to review:" not in index


def test_each_batch_index_is_ranked_for_its_own_code(project_tree, monkeypatch):
    # The same two test files in two batches holding different source files: the
    # batch holding src/app.py leads with app's test, the other leads with util's.
    state = _scoped(project_tree, monkeypatch)
    indexes = state["test_indexes"]

    assert _indexed_paths(indexes[1]) == ["tests/test_app.py", "tests/test_util.py"]
    assert _indexed_paths(indexes[2]) == ["tests/test_util.py", "tests/test_app.py"]


def test_scope_indexes_exclude_excluded_test_files(project_tree, monkeypatch):
    # A test file under an excluded directory: never fetched, so never indexed.
    (project_tree / "tests" / "legacy").mkdir()
    (project_tree / "tests" / "legacy" / "test_old.py").write_text(
        "def test_excluded_by_pattern():\n    pass\n", encoding="utf-8"
    )
    state = _scoped(project_tree, monkeypatch, exclude=["tests/legacy/"])

    for index in state["test_indexes"].values():
        assert set(_indexed_paths(index)) == {"tests/test_app.py", "tests/test_util.py"}
    assert "test_excluded_by_pattern" not in str(state["test_indexes"])
    assert "tests/legacy/test_old.py" not in state["files"]


def test_scope_sets_indexes_to_none_when_the_scope_has_no_tests(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _stub_sast(monkeypatch)
    result = make_scope_node(_runtime(RecordingLLM()))(_state(str(tmp_path)))

    assert result["test_indexes"] is None


def test_test_index_log_line(project_tree, monkeypatch):
    _stub_sast(monkeypatch)
    events: list[str] = []
    runtime = _runtime(RecordingLLM(), batch_chars=_BATCH_CHARS)
    monkeypatch.setattr(runtime.log, "info", lambda message, **_kw: events.append(message))
    make_scope_node(runtime)(_state(str(project_tree)))

    assert [event for event in events if event.startswith("test index:")] == [
        "test index: 2 test file(s), 3 test name(s); up to 16000 chars per batch; "
        "0 of 2 batch(es) truncated"
    ]


def test_test_index_log_line_counts_truncated_batches(crowded_tree, monkeypatch):
    _stub_sast(monkeypatch)
    monkeypatch.setattr(
        scope_module,
        "build_batch_test_indexes",
        partial(build_batch_test_indexes, max_chars=250),
    )
    events: list[str] = []
    runtime = _runtime(RecordingLLM(), batch_chars=_BATCH_CHARS)
    monkeypatch.setattr(runtime.log, "info", lambda message, **_kw: events.append(message))
    result = make_scope_node(runtime)(_state(str(crowded_tree)))

    assert len(result["test_indexes"]) == len(result["batch_plan"].batches)
    assert [event for event in events if event.startswith("test index:")] == [
        "test index: 14 test file(s), 15 test name(s); up to 250 chars per batch; "
        "3 of 3 batch(es) truncated"
    ]


# --- llm_findings: per-batch context, and nothing else changed ---


def test_batch_extra_reaches_only_its_own_batch(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch)
    plan = state["batch_plan"]

    llm = RecordingLLM()
    llm_findings(
        llm,
        plan,
        "code_quality",
        None,
        category=Category.TEST_COVERAGE,
        log=None,
        batch_extra={1: "INDEX FOR ONE", 2: "INDEX FOR TWO"},
    )

    users = llm.batch_order()
    assert len(users) == 2
    assert "### FILE: src/app.py" in users[0]
    assert "INDEX FOR ONE" in users[0] and "INDEX FOR TWO" not in users[0]
    assert "### FILE: src/util.py" in users[1]
    assert "INDEX FOR TWO" in users[1] and "INDEX FOR ONE" not in users[1]


def test_batch_extra_goes_after_extra_and_before_the_code(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch)
    plan = state["batch_plan"]

    llm = RecordingLLM()
    llm_findings(
        llm,
        plan,
        "code_quality",
        "PROJECT CONTEXT",
        category=Category.TEST_COVERAGE,
        log=None,
        extra="SHARED EXTRA",
        batch_extra={batch.index: f"INDEX {batch.index}" for batch in plan.batches},
    )

    for number, user in enumerate(llm.batch_order(), start=1):
        assert user.index("PROJECT CONTEXT") < user.index("SHARED EXTRA")
        assert user.index("SHARED EXTRA") < user.index(f"INDEX {number}")
        assert user.index(f"INDEX {number}") < user.index(_CODE_MARKER)


def test_a_caller_without_batch_extra_sends_the_unchanged_message(project_tree, monkeypatch):
    # Every other review type passes nothing, so its messages must be what they
    # were before the parameter existed: context, then extra, then the code.
    state = _scoped(project_tree, monkeypatch)
    plan = state["batch_plan"]

    llm = RecordingLLM()
    llm_findings(
        llm,
        plan,
        "code_quality",
        "PROJECT CONTEXT",
        category=Category.TEST_COVERAGE,
        log=None,
        extra="SHARED EXTRA",
    )

    for user, batch in zip(llm.batch_order(), plan.batches, strict=True):
        assert user == f"PROJECT CONTEXT\n\nSHARED EXTRA\n\n{_CODE_MARKER}{batch.text}"


def test_a_batch_with_no_entry_is_left_alone(project_tree, monkeypatch):
    # batch_extra present but silent about this batch must not add a stray blank
    # block or swallow the code.
    state = _scoped(project_tree, monkeypatch)
    plan = state["batch_plan"]

    llm = RecordingLLM()
    llm_findings(
        llm,
        plan,
        "code_quality",
        None,
        category=Category.TEST_COVERAGE,
        log=None,
        batch_extra={1: "INDEX FOR ONE"},
    )

    assert llm.batch_order()[1] == f"{_CODE_MARKER}{plan.batches[1].text}"


def test_an_empty_batch_extra_dict_changes_nothing(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch)
    plan = state["batch_plan"]

    llm = RecordingLLM()
    llm_findings(
        llm, plan, "code_quality", None, category=Category.TEST_COVERAGE, log=None, batch_extra={}
    )

    for user, batch in zip(llm.batch_order(), plan.batches, strict=True):
        assert user == f"{_CODE_MARKER}{batch.text}"


# --- the test-coverage node, and only that node ---


def test_test_coverage_sends_each_batch_its_own_index(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch)
    llm = RecordingLLM()
    make_test_coverage_node(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    users = llm.batch_order()
    assert len(users) == 2
    assert _indexed_paths(llm.index_text(users[0])) == [
        "tests/test_app.py",
        "tests/test_util.py",
    ]
    assert _indexed_paths(llm.index_text(users[1])) == [
        "tests/test_util.py",
        "tests/test_app.py",
    ]
    for user in users:
        assert INDEX_HEADER in user
        assert user.index(INDEX_HEADER) < user.index(_CODE_MARKER)


def test_test_coverage_sends_the_index_when_there_is_only_one_batch(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch, batch_chars=50_000)
    assert len(state["batch_plan"].batches) == 1

    llm = RecordingLLM()
    make_test_coverage_node(_runtime(llm, batch_chars=50_000))(state)

    assert len(llm.users()) == 1
    assert INDEX_HEADER in llm.users()[0]


@pytest.mark.parametrize(
    "factory",
    [make_code_quality_node, make_security_node, make_performance_node],
    ids=["code_quality", "security", "performance"],
)
def test_other_review_types_are_not_sent_any_index(project_tree, monkeypatch, factory):
    state = _scoped(project_tree, monkeypatch)

    llm = RecordingLLM()
    factory(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    assert llm.calls, "the node should have reviewed the two batches"
    for user in llm.users():
        # The test files are in scope and so appear as code to review, but no
        # review type but test coverage is handed an index of them.
        assert INDEX_HEADER not in user
        assert "test files in the reviewed scope" not in user
    bodies = "".join(llm.code_bodies())
    assert "test_app_starts" in bodies, "the test files are reviewed as ordinary code"


def test_no_tests_in_scope_tells_the_reviewer_so(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text(_lines(25, "app"), encoding="utf-8")
    state = _scoped(tmp_path, monkeypatch)
    assert state["test_indexes"] is None

    llm = RecordingLLM()
    make_test_coverage_node(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    assert llm.calls
    for user in llm.users():
        assert NO_TEST_FILES_NOTICE in user
        assert INDEX_HEADER not in user


def test_a_state_without_the_indexes_key_does_not_raise(tmp_path, monkeypatch):
    # Nodes are called with whatever state exists; a missing key means the same as
    # no tests in scope, not a KeyError.
    (tmp_path / "app.py").write_text(_lines(25, "app"), encoding="utf-8")
    state = _scoped(tmp_path, monkeypatch)
    state.pop("test_indexes")

    llm = RecordingLLM()
    result = make_test_coverage_node(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    assert result["code_findings"] == []
    assert NO_TEST_FILES_NOTICE in llm.users()[0]


def test_the_indexes_survive_a_node_that_finds_nothing(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch)
    llm = RecordingLLM()
    result = make_test_coverage_node(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    assert result == {"code_findings": [], "errors": [], "failed_batches": []}
    assert set(state["test_indexes"]) == {1, 2}, "the node must not clear shared state"


# --- the severity cap: warning is the ceiling for test coverage (T085, FR-004) ---


_CITED = "src/app.py"


def _finding_payload(severity: str, line: int = 2, finding_id: str = "cf-1") -> str:
    """One code finding, grounded in a line of the project's src/app.py."""
    return json.dumps(
        [
            {
                "id": finding_id,
                "file": _CITED,
                "start_line": line,
                "start_col": 1,
                "end_line": line,
                "end_col": 1,
                "severity": severity,
                "title": f"Gap at {_CITED}:{line}",
                "description": "Something the reviewer wants to raise.",
                "recommendation": "Add a test.",
                "confidence": 0.8,
                "cited_snippet": f"app_{line:06d} = {line}",
            }
        ]
    )


class SeveritiesLLM:
    """Answers the test-coverage review with one finding per severity asked for.

    The severities are handed out one per batch, in order; a batch past the end of
    the list repeats the last one. The other four review types get "[]", so a test
    can say "the only findings in this run are these test-coverage ones" and mean it.
    """

    model_name = "severities"

    def __init__(self, severities: tuple[str, ...]) -> None:
        self.severities = severities
        self.calls: list[tuple[str, str]] = []
        self._lock = Lock()
        self._test_coverage_prompt = load_prompt("test_coverage")
        self._served = 0

    def complete(self, system: str, user: str) -> str:
        with self._lock:
            self.calls.append((system, user))
            if system != self._test_coverage_prompt:
                return "[]"
            self._served += 1
            batch = self._served
        severity = self.severities[min(batch, len(self.severities)) - 1]
        return _finding_payload(severity, finding_id=f"cf-{batch}")


def _capped_run(project_tree, monkeypatch, severities, *, log=None) -> tuple[list, list[str]]:
    """Run the test-coverage node over the project with these severities.

    ``log`` collects the node's info lines, so a test can assert on what it said.
    """
    state = _scoped(project_tree, monkeypatch)
    runtime = _runtime(SeveritiesLLM(severities), batch_chars=_BATCH_CHARS)
    lines: list[str] = log if log is not None else []
    runtime.log.info = lambda message, **_kw: lines.append(message)  # type: ignore[method-assign]
    findings = make_test_coverage_node(runtime)(state)["code_findings"]
    return findings, lines


def test_a_test_coverage_error_is_lowered_to_warning(project_tree, monkeypatch):
    findings, _lines = _capped_run(project_tree, monkeypatch, ("error", "error"))

    assert [f.severity for f in findings] == [Severity.WARNING, Severity.WARNING]
    # The reviewer's own severity is recorded, not discarded: without it the report
    # cannot distinguish a capped finding from a reviewer's own warning.
    assert [f.severity_adjusted_from for f in findings] == [Severity.ERROR, Severity.ERROR]


def test_a_capped_finding_is_a_new_object_and_leaves_the_original_alone(project_tree, monkeypatch):
    """A finding is shared state; the cap must not rewrite the one the node got."""
    state = _scoped(project_tree, monkeypatch)
    llm = SeveritiesLLM(("error",))
    original, _errors = llm_findings(
        llm,
        state["batch_plan"],
        "test_coverage",
        None,
        category=Category.TEST_COVERAGE,
        log=None,
    )

    capped, lowered = _cap_severity(original, None)

    assert lowered == 2
    assert capped is not original
    assert [f.severity for f in original] == [Severity.ERROR, Severity.ERROR]
    assert all(f.severity_adjusted_from is None for f in original)
    assert [f.severity for f in capped] == [Severity.WARNING, Severity.WARNING]
    assert [f.id for f in capped] == [f.id for f in original], "identity must survive the cap"


@pytest.mark.parametrize("severity", ["warning", "info"])
def test_other_test_coverage_severities_are_left_alone(project_tree, monkeypatch, severity):
    findings, _lines = _capped_run(project_tree, monkeypatch, (severity,))

    assert findings, "the node should still have produced a finding per batch"
    assert {f.severity for f in findings} == {Severity(severity)}
    assert all(f.severity_adjusted_from is None for f in findings)


@pytest.mark.parametrize(
    ("name", "factory"),
    [
        ("code_quality", make_code_quality_node),
        ("security", make_security_node),
        ("performance", make_performance_node),
    ],
)
def test_an_error_from_another_review_type_is_not_capped(project_tree, monkeypatch, name, factory):
    # The cap is a statement about test coverage, not about severity in general:
    # a real defect in production code must still be able to be an error.

    class OneError:
        model_name = "one-error"

        def complete(self, system: str, user: str) -> str:
            return _finding_payload("error")

    findings = factory(_runtime(OneError(), batch_chars=_BATCH_CHARS))(
        _scoped(project_tree, monkeypatch)
    )["code_findings"]

    assert findings, f"{name} should have produced a finding per batch"
    assert {f.severity for f in findings} == {Severity.ERROR}
    assert all(f.severity_adjusted_from is None for f in findings), name


def test_the_cap_logs_how_many_findings_it_lowered(project_tree, monkeypatch):
    lines: list[str] = []

    _capped_run(project_tree, monkeypatch, ("error", "error"), log=lines)

    assert "test-coverage: lowered 2 finding(s) from error to warning (FR-004)" in lines, lines


def test_one_lowered_finding_is_logged_as_one(project_tree, monkeypatch):
    lines: list[str] = []

    _capped_run(project_tree, monkeypatch, ("error", "warning"), log=lines)

    assert "test-coverage: lowered 1 finding(s) from error to warning (FR-004)" in lines, lines


def test_nothing_logged_when_nothing_was_lowered(project_tree, monkeypatch):
    lines: list[str] = []

    _capped_run(project_tree, monkeypatch, ("warning", "info"), log=lines)

    assert not [line for line in lines if "lowered" in line], lines
    assert "test-coverage: 2 findings" in lines, "the usual count line is still logged"


def test_the_cap_does_not_change_the_suppression_fingerprint(project_tree, monkeypatch):
    """A suppressed test-coverage finding must suppress the same way either way.

    The fingerprint is keyed by file, category and snippet, so there is no input a
    severity cap could change: a finding a user silenced stays silenced, and the
    cap cannot silence one they did not.
    """
    findings, _lines = _capped_run(project_tree, monkeypatch, ("error",))
    capped = findings[0]
    uncapped = capped.model_copy(
        update={"severity": Severity.ERROR, "severity_adjusted_from": None}
    )
    assert capped.severity is Severity.WARNING

    assert "severity" not in inspect.signature(compute_fingerprint).parameters
    assert compute_fingerprint(
        capped.file, capped.category.value, capped.cited_snippet
    ) == compute_fingerprint(uncapped.file, uncapped.category.value, uncapped.cited_snippet)


def test_a_run_whose_only_errors_are_test_coverage_is_not_requires_modification(tmp_path, monkeypatch):
    """End to end (FR-004, FR-015): the cap must reach the run's verdict.

    The reviewer grades the missing test an error, exactly as it would have before
    the cap; nothing else in the run produces a finding. Without the cap this run
    would be RequiresModification for a test that was never shown to be broken.
    """
    _stub_sast(monkeypatch)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text(_lines(25, "app"), encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text("def test_app():\n    pass\n", encoding="utf-8")

    class OnlyTestCoverageErrors:
        model_name = "only-test-coverage-errors"

        def complete(self, system: str, user: str) -> str:
            if system != load_prompt("test_coverage"):
                return "[]"
            if "requirements-traceability" in system.lower():
                return "[]"
            return _finding_payload("error")

    outcome = run_review(
        Settings(api_key="test-key", batch_chars=_BATCH_CHARS, max_batches=8),
        ReviewScope.PROJECT,
        str(tmp_path),
        llm=OnlyTestCoverageErrors(),
    )
    assert outcome.exit_code == 0

    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert [f.category for f in report.code_findings] == [Category.TEST_COVERAGE]
    assert [f.severity for f in report.code_findings] == [Severity.WARNING]
    assert Severity.ERROR not in report.summary.severity_counts
    assert report.summary.verdict is not Verdict.REQUIRES_MODIFICATION
    assert report.summary.verdict is Verdict.REQUIRES_REVIEW
    markdown = report.markdown_content or ""
    assert "**Verdict**: `RequiresReview`" in markdown
    assert "test-coverage findings are capped at warning" in markdown


# --- end to end: the regression a single alphabetical index caused ---


def _regression_tree(tmp_path: Path) -> Path:
    """A suite whose alphabetical order buries the test of src/batching.py.

    ``tests/test_filler_*`` sorts before ``tests/unit/test_batching.py``, so an
    index that listed files in path order would spend its whole budget on fillers
    and never mention batching's tests.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "src" / "batching.py").write_text(_lines(25, "batch"), encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text(_lines(25, "other"), encoding="utf-8")
    (tmp_path / "tests" / "unit" / "test_batching.py").write_text(
        "def test_packs_whole_files_first():\n    pass\n\n\n"
        "def test_is_deterministic_in_insertion_order():\n    pass\n",
        encoding="utf-8",
    )
    for index in range(12):
        (tmp_path / "tests" / f"test_filler_{index:02d}.py").write_text(
            "def test_filler():\n    pass\n", encoding="utf-8"
        )
    return tmp_path


def test_batching_tests_survive_a_truncated_index_end_to_end(tmp_path, monkeypatch):
    """The batch holding src/batching.py must still see batching's own tests.

    The budget holds two of the suite's thirteen test files. In path order they
    would be tests/test_filler_00.py and tests/test_filler_01.py, and batching's
    own tests would be among the eleven the reviewer is never told about; by
    relevance the batch's own test is listed first, with its names.
    """
    _stub_sast(monkeypatch)
    monkeypatch.setattr(
        scope_module,
        "build_batch_test_indexes",
        partial(build_batch_test_indexes, max_chars=300),
    )
    tree = _regression_tree(tmp_path)
    llm = RecordingLLM()
    outcome = run_review(
        Settings(api_key="test-key", batch_chars=_BATCH_CHARS, max_batches=8),
        ReviewScope.PROJECT,
        str(tree),
        llm=llm,
    )
    assert outcome.exit_code == 0

    # The fillers really do sort before the batching test, so this is not luck.
    fillers = sorted(path.relative_to(tree).as_posix() for path in tree.rglob("test_filler_*.py"))
    assert fillers[0] < "tests/unit/test_batching.py"

    calls = [
        user
        for user in llm.users("test_coverage")
        if "### FILE: src/batching.py" in user.split(_CODE_MARKER, 1)[1]
    ]
    assert len(calls) == 1, "src/batching.py should have been reviewed in one batch"
    index = llm.index_text(calls[0])

    assert _indexed_paths(index) == ["tests/unit/test_batching.py", "tests/test_filler_00.py"]
    assert (
        "- tests/unit/test_batching.py: test_packs_whole_files_first, "
        "test_is_deterministic_in_insertion_order" in index
    )
    assert index.rstrip().endswith(
        "more test file(s) not listed (index limit 300 characters)."
    )
    # The batch holding the fillers leads with the file it actually contains, so
    # relevance follows the batch rather than one global ordering.
    other = [
        llm.index_text(user)
        for user in llm.users("test_coverage")
        if "### FILE: tests/test_filler_00.py" in user.split(_CODE_MARKER, 1)[1]
    ]
    assert len(other) == 1
    assert _indexed_paths(other[0])[0] == "tests/test_filler_00.py"