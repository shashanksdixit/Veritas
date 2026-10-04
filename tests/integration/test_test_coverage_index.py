"""Integration tests — test index for the test-coverage review (T081, FR-004).

A batch only holds part of the scope, so the test-coverage reviewer used to see
the tests that happened to share its batch and report the rest as missing. These
tests pin the fix and its boundaries:

* the index is built once, from the same post-exclusion source files the batches
  are planned from, and travels in state;
* every batch of the test-coverage review carries it, so a batch holding only
  ``src/app.py`` still sees the tests for ``src/util.py``;
* no other review type is sent an index, and it says what it can say without one;
* a scope with no tests says so rather than implying no tests exist.
"""

from __future__ import annotations

from datetime import datetime
from functools import partial
from pathlib import Path

import pytest

from veritas.config.settings import Settings
from veritas.models.entities import ReviewRun, ReviewScope
from veritas.review.graph import Runtime
from veritas.review.nodes import scope as scope_module
from veritas.review.nodes.code_quality import make_code_quality_node
from veritas.review.nodes.performance import make_performance_node
from veritas.review.nodes.scope import make_scope_node
from veritas.review.nodes.security import make_security_node
from veritas.review.nodes.test_coverage import make_test_coverage_node
from veritas.review.test_index import INDEX_HEADER, NO_TEST_FILES_NOTICE, build_test_index
from veritas.security.opengrep import OpengrepResult
from veritas.utils.logging import Log

_CODE_MARKER = "Code to review:\n\n"

# Two 25-line files render to ~594-char blocks, so at batch_chars=1000 one block
# does not fit beside the other and each source file takes its own batch.
_BATCH_CHARS = 1000


def _lines(count: int, tag: str) -> str:
    return "".join(f"{tag}_{n:06d} = {n}\n" for n in range(1, count + 1))


class RecordingLLM:
    """Records every user message a review node sends and returns no findings."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return "[]"

    def users(self) -> list[str]:
        return [user for _system, user in self.calls]

    def code_bodies(self) -> list[str]:
        return [user.split(_CODE_MARKER, 1)[1] for user in self.users()]


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
        prompt_version="1.4.0",
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
        "test_index": None,
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


# --- the scope node builds the index from the post-exclusion source files ---


def test_scope_state_carries_the_index_of_post_exclusion_test_files(project_tree, monkeypatch):
    # A test file under an excluded directory: never fetched, so never indexed,
    # which is the point of building the index from the same source files.
    (project_tree / "tests" / "legacy").mkdir()
    (project_tree / "tests" / "legacy" / "test_old.py").write_text(
        "def test_excluded_by_pattern():\n    pass\n", encoding="utf-8"
    )
    state = _scoped(project_tree, monkeypatch, exclude=["tests/legacy/"])

    assert state["test_index"] == (
        f"{INDEX_HEADER}\n"
        "- tests/test_app.py: test_app_starts, test_app_stops\n"
        "- tests/test_util.py: TestUtil.test_helper_math"
    )
    assert "test_excluded_by_pattern" not in state["test_index"]
    assert "tests/legacy" not in state["test_index"]
    assert "tests/legacy/test_old.py" not in state["files"]


def test_index_covers_the_files_that_are_batched(project_tree, monkeypatch):
    # Same input on both sides: an indexed test file is always a reviewed file,
    # so the index cannot describe a file the reviewer will never see.
    state = _scoped(project_tree, monkeypatch)
    batched = {chunk.path for batch in state["batch_plan"].batches for chunk in batch.chunks}
    indexed = {
        line[2:].split(":", 1)[0].split(" (", 1)[0]
        for line in state["test_index"].splitlines()[1:]
    }
    assert indexed == {"tests/test_app.py", "tests/test_util.py"}
    assert indexed <= batched


def test_scope_sets_the_index_to_none_when_the_scope_has_no_tests(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _stub_sast(monkeypatch)
    result = make_scope_node(_runtime(RecordingLLM()))(_state(str(tmp_path)))

    assert result["test_index"] is None


def test_test_index_log_line_reports_the_counts(project_tree, monkeypatch):
    _stub_sast(monkeypatch)
    events: list[str] = []
    runtime = _runtime(RecordingLLM())
    monkeypatch.setattr(runtime.log, "info", lambda message, **_kw: events.append(message))
    result = make_scope_node(runtime)(_state(str(project_tree)))

    index_lines = [event for event in events if event.startswith("test index:")]
    assert len(index_lines) == 1
    assert index_lines[0] == (
        f"test index: 2 test file(s), 3 test name(s), {len(result['test_index'])} chars"
    )


def test_test_index_log_line_reports_omitted_files(project_tree, monkeypatch):
    # A scope with more tests than the budget can hold, so the index is cut and
    # the log has to say how many files the model was not shown.
    for index in range(12):
        (project_tree / "tests" / f"test_extra_{index:02d}.py").write_text(
            "def test_extra():\n    pass\n", encoding="utf-8"
        )
    _stub_sast(monkeypatch)
    monkeypatch.setattr(scope_module, "build_test_index", partial(build_test_index, max_chars=220))
    events: list[str] = []
    runtime = _runtime(RecordingLLM())
    monkeypatch.setattr(runtime.log, "info", lambda message, **_kw: events.append(message))
    result = make_scope_node(runtime)(_state(str(project_tree)))

    index = result["test_index"]
    assert index.endswith("more test file(s) not listed (index limit 220 characters).")
    omitted = int(index.rsplit("...and ", 1)[1].split(" ", 1)[0])
    listed = len(index.splitlines()) - 2
    assert omitted == 14 - listed == 13
    assert [event for event in events if event.startswith("test index:")] == [
        f"test index: 14 test file(s), 15 test name(s), {len(index)} chars, "
        f"truncated ({omitted} file(s) omitted)"
    ]


# --- every test-coverage batch carries the index ---


def test_each_batch_sees_the_tests_for_the_code_in_the_other_batch(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch)
    batches = state["batch_plan"].batches
    assert len(batches) == 2, "the regression needs two batches"

    llm = RecordingLLM()
    make_test_coverage_node(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    bodies = llm.code_bodies()
    assert len(bodies) == 2
    # The first batch is src/app.py alone: neither test file is in it, which is
    # the situation that used to produce "no tests exist for this code".
    app_batch = bodies[0]
    assert app_batch.startswith("### FILE: src/app.py")
    assert "tests/test_app.py" not in app_batch
    for user in llm.users():
        assert INDEX_HEADER in user
        assert "- tests/test_app.py: test_app_starts, test_app_stops" in user
        assert "- tests/test_util.py: TestUtil.test_helper_math" in user
        # The index is context the reviewer reads, ahead of the code.
        assert user.index(INDEX_HEADER) < user.index(_CODE_MARKER)


def test_test_coverage_sends_the_index_when_there_is_only_one_batch(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch, batch_chars=50_000)
    assert len(state["batch_plan"].batches) == 1

    llm = RecordingLLM()
    make_test_coverage_node(_runtime(llm, batch_chars=50_000))(state)

    assert len(llm.calls) == 1
    assert INDEX_HEADER in llm.users()[0]


# --- and only the test-coverage review ---


@pytest.mark.parametrize(
    "factory",
    [make_code_quality_node, make_security_node, make_performance_node],
    ids=["code_quality", "security", "performance"],
)
def test_other_review_types_are_not_sent_the_index(project_tree, monkeypatch, factory):
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


# --- a scope with no tests ---


def test_no_tests_in_scope_tells_the_reviewer_so(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text(_lines(25, "app"), encoding="utf-8")
    state = _scoped(tmp_path, monkeypatch)
    assert state["test_index"] is None

    llm = RecordingLLM()
    make_test_coverage_node(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    assert llm.calls
    for user in llm.users():
        assert NO_TEST_FILES_NOTICE in user
        assert INDEX_HEADER not in user


def test_a_state_without_the_index_key_does_not_raise(tmp_path, monkeypatch):
    # Nodes are called with whatever state exists; a missing key means the same
    # as no tests in scope, not a KeyError.
    (tmp_path / "app.py").write_text(_lines(25, "app"), encoding="utf-8")
    state = _scoped(tmp_path, monkeypatch)
    state.pop("test_index")

    llm = RecordingLLM()
    result = make_test_coverage_node(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    assert result["code_findings"] == []
    assert NO_TEST_FILES_NOTICE in llm.users()[0]


def test_the_index_survives_a_node_that_returns_nothing_findings(project_tree, monkeypatch):
    state = _scoped(project_tree, monkeypatch)
    llm = RecordingLLM()
    result = make_test_coverage_node(_runtime(llm, batch_chars=_BATCH_CHARS))(state)

    assert result == {"code_findings": [], "errors": []}
    assert state["test_index"] is not None, "the node must not clear the shared state"