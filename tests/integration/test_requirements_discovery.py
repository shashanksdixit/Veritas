"""Integration tests — requirement source discovery and extraction in the scope
node (B2a, FR-008).

Discovery is by pattern now, so the interesting cases are the ones an exact-name
list got wrong: a feature spec under `specs/`, a checklist that looks like
documentation, and an excluded spec that must never be fetched.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from veritas.config.settings import Settings
from veritas.models.entities import ReviewRun, ReviewScope
from veritas.review.graph import Runtime
from veritas.review.nodes import scope as scope_module
from veritas.review.nodes.requirements import make_requirements_node
from veritas.review.nodes.scope import make_scope_node
from veritas.security.opengrep import OpengrepResult
from veritas.utils.logging import Log

_SPEC_TEXT = "# Feature\n\n- **FR-001**: The system MUST ship.\n- **FR-002**: It MUST be fast.\n"
_CHECKLIST_TEXT = "- [x] FR-001 done\n"


@pytest.fixture
def spec_project(tmp_path: Path) -> Path:
    """A project with a feature spec, a checklist and the usual root docs."""
    (tmp_path / "specs/001-a").mkdir(parents=True)
    (tmp_path / "specs/001-a/spec.md").write_text(_SPEC_TEXT, encoding="utf-8")
    (tmp_path / "specs/001-a/checklists").mkdir()
    (tmp_path / "specs/001-a/checklists/requirements.md").write_text(
        _CHECKLIST_TEXT, encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n\nA readme with no FR lines.\n", encoding="utf-8")
    return tmp_path


class FakeHost:
    """Records every fetch so a test can prove what was and was not requested."""

    provider = "github"

    def __init__(self, contents: dict[str, str]) -> None:
        self._contents = contents
        self.fetched: list[str] = []

    def head_sha(self, owner, repo, number):
        return "abcd1234"

    def list_pr_files(self, owner, repo, number):
        return [{"filename": name} for name in self._contents]

    def get_file_contents(self, owner, repo, path, ref):
        self.fetched.append(path)
        return self._contents[path]


def _stub_sast(monkeypatch) -> None:
    monkeypatch.setattr(
        scope_module,
        "collect_sast",
        lambda files, **_kwargs: OpengrepResult(findings=[], rules="r"),
    )


def _runtime(exclude: list[str] | None = None, llm=None) -> Runtime:
    return Runtime(
        settings=Settings(exclude=exclude or [], api_key="test-key"),
        log=Log(verbose=False),
        llm=llm,
    )


def _state(target: str, scope: ReviewScope = ReviewScope.PROJECT) -> dict:
    run = ReviewRun(
        scope=scope,
        target=target,
        config_hash="h",
        model_name="m",
        prompt_version="1.7.0",
        started_at=datetime.now(),
    )
    return {"messages": [], "scope": scope, "target": target, "run": run, "files": {}}


class NullLLM:
    """A reviewer that returns nothing; discovery is what these tests assert."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return "[]"


# --- local discovery ---


def test_a_feature_spec_is_collected_and_a_checklist_is_not(spec_project, monkeypatch):
    _stub_sast(monkeypatch)
    result = make_scope_node(_runtime())(_state(str(spec_project)))

    assert "specs/001-a/spec.md" in result["files"]
    assert "specs/001-a/checklists/requirements.md" not in result["files"]


def test_requirement_sources_are_listed_in_priority_order(spec_project, monkeypatch):
    _stub_sast(monkeypatch)
    result = make_scope_node(_runtime())(_state(str(spec_project)))

    # The spec outranks the README even though the walk finds the README first.
    assert result["requirement_sources"] == ["specs/001-a/spec.md", "README.md"]


def test_the_requirements_are_extracted_with_their_file_and_line(spec_project, monkeypatch):
    _stub_sast(monkeypatch)
    result = make_scope_node(_runtime())(_state(str(spec_project)))

    requirements = result["requirements"]
    assert [r.id for r in requirements] == ["FR-001", "FR-002"]
    assert requirements[0].file == "specs/001-a/spec.md"
    assert requirements[0].line == 3
    assert requirements[0].text == "The system MUST ship."


def test_a_project_with_no_requirements_documentation_says_so(tmp_path, monkeypatch):
    _stub_sast(monkeypatch)
    empty = tmp_path / "plain"
    (empty / "src").mkdir(parents=True)
    (empty / "src/app.py").write_text("x = 1\n", encoding="utf-8")

    events: list[str] = []
    runtime = _runtime()
    runtime.log.info = lambda message, **_kw: events.append(message)  # type: ignore[method-assign]
    result = make_scope_node(runtime)(_state(str(empty)))

    assert result["requirement_sources"] == []
    assert result["requirements"] == []
    assert [e for e in events if e.startswith("requirements:")] == [
        "requirements: no requirements documentation in scope"
    ]


def test_free_text_sources_are_reported_as_such(spec_project, monkeypatch):
    _stub_sast(monkeypatch)
    # Every source present has documentation but no parseable FR line, which is
    # the case the requirements node still handles as free text.
    (spec_project / "specs/001-a/spec.md").write_text(
        "# Feature\n\nWritten as prose, not as FR lines.\n", encoding="utf-8"
    )
    no_frs = spec_project / "specs/002-b"
    no_frs.mkdir()
    (no_frs / "spec.md").write_text("# Draft\n\nNot a structured requirement yet.\n", encoding="utf-8")
    (spec_project / "README.md").write_text("# Demo\n\nA readme with no FR lines.\n", encoding="utf-8")

    events: list[str] = []
    runtime = _runtime()
    runtime.log.info = lambda message, **_kw: events.append(message)  # type: ignore[method-assign]
    result = make_scope_node(runtime)(_state(str(spec_project)))

    assert result["requirements"] == []
    assert result["requirement_sources"] == ["specs/001-a/spec.md", "specs/002-b/spec.md", "README.md"]
    lines = [e for e in events if e.startswith("requirements:")]
    # The free-text message names the highest-priority source, which is the one
    # the requirements node would read.
    assert lines == [
        "requirements: sources found but no structured requirements; "
        "free text from specs/001-a/spec.md"
    ]


def test_the_discovery_log_line_names_the_count_and_the_sources(spec_project, monkeypatch):
    _stub_sast(monkeypatch)
    events: list[str] = []
    runtime = _runtime()
    runtime.log.info = lambda message, **_kw: events.append(message)  # type: ignore[method-assign]
    make_scope_node(runtime)(_state(str(spec_project)))

    assert "requirements: 2 requirement(s) from 2 source(s): " "specs/001-a/spec.md, README.md" in events


def test_an_excluded_spec_is_not_collected(spec_project, monkeypatch):
    _stub_sast(monkeypatch)
    result = make_scope_node(_runtime(["specs/"]))(_state(str(spec_project)))

    assert result["requirement_sources"] == ["README.md"]
    assert result["requirements"] == []


# --- PR discovery: only sources included in the PR ---


def test_pr_scope_fetches_a_feature_spec_and_not_a_checklist(monkeypatch):
    _stub_sast(monkeypatch)
    host = FakeHost(
        {
            "src/app.py": "print('hello')\n",
            "specs/001-a/spec.md": _SPEC_TEXT,
            "specs/001-a/checklists/requirements.md": _CHECKLIST_TEXT,
            "README.md": "# Demo\n",
        }
    )
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda *a, **k: host)

    result = make_scope_node(_runtime())(_state("acme/widget#3", ReviewScope.PR))

    assert "specs/001-a/spec.md" in host.fetched
    assert "specs/001-a/checklists/requirements.md" not in host.fetched
    assert result["requirement_sources"] == ["specs/001-a/spec.md", "README.md"]
    assert [r.id for r in result["requirements"]] == ["FR-001", "FR-002"]


def test_pr_scope_does_not_fetch_a_requirements_file_the_pr_did_not_touch(monkeypatch):
    _stub_sast(monkeypatch)
    host = FakeHost({"src/app.py": "print('hello')\n"})
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda *a, **k: host)

    result = make_scope_node(_runtime())(_state("acme/widget#3", ReviewScope.PR))

    assert set(result["files"]) == {"src/app.py"}
    assert host.fetched == ["src/app.py"]
    assert result["requirement_sources"] == []


def test_pr_scope_does_not_fetch_an_excluded_spec(monkeypatch):
    _stub_sast(monkeypatch)
    host = FakeHost({"src/app.py": "x = 1\n", "specs/001-a/spec.md": _SPEC_TEXT})
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda *a, **k: host)

    result = make_scope_node(_runtime(["specs/"]))(_state("acme/widget#3", ReviewScope.PR))

    assert "specs/001-a/spec.md" not in host.fetched
    assert result["requirement_sources"] == []


# --- the requirements node is unchanged by any of this (B2a does not touch it) ---


def test_the_requirements_node_output_does_not_depend_on_the_new_state_keys():
    """Same files in, same findings out, whether or not B2a's keys are present.

    B2a adds `requirement_sources` and `requirements` to state; the requirements
    node must ignore them, because B2a is discovery only and the node's own
    source selection and judging are a later step.
    """
    from veritas.models.entities import RequirementStatus

    from veritas.review.requirements_source import Requirement

    files = {"src/app.py": "print('hello')\n", "specs/001-a/spec.md": _SPEC_TEXT}
    state = {"files": files, "project_context": None}
    without = make_requirements_node(_runtime(llm=NullLLM()))(dict(state))
    with_keys = make_requirements_node(_runtime(llm=NullLLM()))(
        {
            **state,
            "requirement_sources": ["specs/001-a/spec.md"],
            "requirements": [
                Requirement(
                    id="FR-001",
                    text="The system MUST ship.",
                    file="specs/001-a/spec.md",
                    line=3,
                )
            ],
        }
    )

    # Ids are generated per finding, so everything else must be identical.
    assert [f.model_dump(exclude={"id"}) for f in without["requirement_findings"]] == [
        f.model_dump(exclude={"id"}) for f in with_keys["requirement_findings"]
    ]
    # And with an llm that returns nothing and no documentation at all, the
    # existing single-unclear-finding fallback still fires.
    fallback = make_requirements_node(_runtime(llm=NullLLM()))(
        {"files": {"src/app.py": "x = 1\n"}, "project_context": None}
    )["requirement_findings"]
    assert len(fallback) == 1
    assert fallback[0].status is RequirementStatus.UNCLEAR