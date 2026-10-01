"""Integration tests — scope exclusion (T075, FR-029).

Exclusion applies before file contents are fetched, in every scope except
``file`` (an explicit target is always reviewed), and SAST scans exactly the
post-exclusion file set.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from veritas.config.settings import Settings
from veritas.models.entities import ReviewRun, ReviewScope
from veritas.review.graph import Runtime
from veritas.review.nodes import scope as scope_module
from veritas.review.nodes.scope import make_scope_node
from veritas.review.state import ReviewState
from veritas.security.opengrep import OpengrepResult
from veritas.utils.logging import Log

# Named so the walk reaches it first on a case-insensitive, name-ordered
# directory listing — that is the order that exposes a cap regression.
_EXCLUDED_DIR = "aaa_excluded"


class FakeHost:
    """Records every fetch so tests can prove excluded files are never requested."""

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


def _stub_sast(monkeypatch) -> list[str]:
    """Replace collect_sast and return the list that records the file set."""
    seen: list[str] = []

    def _capture(files, **_kwargs):
        seen.extend(files)
        return OpengrepResult(findings=[], rules="r")

    monkeypatch.setattr(scope_module, "collect_sast", _capture)
    return seen


def _runtime(exclude: list[str]) -> Runtime:
    return Runtime(settings=Settings(exclude=exclude), log=Log(verbose=False), llm=None)


def _state(target: str, scope: ReviewScope = ReviewScope.PROJECT) -> ReviewState:
    run = ReviewRun(
        scope=scope,
        target=target,
        config_hash="h",
        model_name="m",
        prompt_version="1.2.0",
        started_at=datetime.now(),
    )
    return {
        "messages": [],
        "scope": scope,
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
        "project_context": None,
        "degraded_sast": None,
        "sast_findings": [],
        "errors": [],
        "report_path": "",
        "report_markdown": None,
    }


def _patterns(exclude: list[str]) -> dict[str, str]:
    return {entry.path: entry.pattern for entry in exclude}


@pytest.fixture
def exclusion_tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.min.js").write_text("const a = 1;\n", encoding="utf-8")
    # A nested .md is a language skip, not a root-level requirements source.
    (tmp_path / ".specify").mkdir()
    (tmp_path / ".specify" / "scripts").mkdir()
    (tmp_path / ".specify" / "scripts" / "plan.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / ".specify" / "nested.md").write_text("# notes\n", encoding="utf-8")
    # Root-level requirements sources, one under a directory prefix.
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "requirements.md").write_text("# REQ-1\n- SATISFIED: x\n", encoding="utf-8")
    (tmp_path / "notes.cob").write_text("IDENTIFICATION DIVISION.\n", encoding="utf-8")
    return tmp_path


def test_project_scope_excludes_before_reading(exclusion_tree, monkeypatch):
    _stub_sast(monkeypatch)
    runtime = _runtime([".specify/", "*.min.js", "docs/"])
    result = make_scope_node(runtime)(_state(str(exclusion_tree)))

    assert set(result["files"]) == {"src/app.py", "README.md"}
    assert _patterns(result["excluded_files"]) == {
        ".specify/scripts/plan.py": ".specify/",
        "web/app.min.js": "*.min.js",
        "docs/requirements.md": "docs/",
    }
    # Excluded directories are not language skips.
    assert ".specify/nested.md" in result["skipped_languages"]
    assert ".specify/nested.md" not in _patterns(result["excluded_files"])


def test_excluded_requirement_doc_not_read(exclusion_tree):
    result = scope_module._run_local("project", str(exclusion_tree), ["docs/"])
    assert "docs/requirements.md" not in result["files"]
    assert _patterns(result["excluded_files"]) == {"docs/requirements.md": "docs/"}
    assert "README.md" in result["files"]


def test_requirement_docs_still_collected_when_not_excluded(exclusion_tree):
    result = scope_module._run_local("project", str(exclusion_tree), ["vendor/"])
    assert "README.md" in result["files"]
    assert "docs/requirements.md" in result["files"]
    assert result["excluded_files"] == []


def test_excluded_files_do_not_consume_max_scope_files(tmp_path, monkeypatch):
    # Cap of 3 with only 2 in-scope sources: the walk completes, so the result is
    # independent of directory-walk order, yet an implementation that counted
    # excluded files against the cap would trip over 3 and truncate early.
    monkeypatch.setattr(scope_module, "MAX_SCOPE_FILES", 3)
    (tmp_path / _EXCLUDED_DIR).mkdir()
    for name in ("x.py", "y.py", "z.py"):
        (tmp_path / _EXCLUDED_DIR / name).write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("c = 3\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("d = 4\n", encoding="utf-8")

    result = scope_module._run_local("project", str(tmp_path), [f"{_EXCLUDED_DIR}/"])
    # Both in-scope sources are present even though 3 files are excluded: the
    # excluded files never occupied a slot against the cap.
    assert set(result["files"]) == {"app.py", "mod.py"}
    assert _patterns(result["excluded_files"]) == {
        f"{_EXCLUDED_DIR}/{name}": f"{_EXCLUDED_DIR}/" for name in ("x.py", "y.py", "z.py")
    }


def test_empty_exclude_list_excludes_nothing(exclusion_tree):
    result = scope_module._run_local("project", str(exclusion_tree), [])
    assert ".specify/scripts/plan.py" in result["files"]
    assert "docs/requirements.md" in result["files"]
    assert "README.md" in result["files"]
    assert "web/app.min.js" in result["files"]
    assert result["excluded_files"] == []


def test_file_scope_explicit_target_under_excluded_dir_is_reviewed(exclusion_tree):
    target = str(exclusion_tree / ".specify" / "scripts" / "plan.py")
    result = scope_module._run_local("file", target, [".specify/"])
    assert result["files"] == {target.replace("\\", "/"): "y = 2\n"}
    assert result["excluded_files"] == []


def test_pr_scope_excluded_files_are_never_fetched(monkeypatch):
    contents = {
        "src/app.py": "x = 1\n",
        ".specify/scripts/plan.py": "y = 2\n",
        "spec.md": "# REQ-1\n",
        "notes.cob": "IDENTIFICATION DIVISION.\n",
    }
    host = FakeHost(contents)
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: host)
    _stub_sast(monkeypatch)

    runtime = _runtime([".specify/", "spec.md"])
    target = "acme/widget#3"
    result = make_scope_node(runtime)(_state(target, ReviewScope.PR))

    assert host.fetched == ["src/app.py"]
    assert set(result["files"]) == {"src/app.py"}
    assert _patterns(result["excluded_files"]) == {
        ".specify/scripts/plan.py": ".specify/",
        "spec.md": "spec.md",
    }
    # An unsupported-language file is handled by the language filter as before.
    assert "notes.cob" in result["skipped_languages"]
    assert "notes.cob" not in _patterns(result["excluded_files"])


def test_pr_scope_supported_requirements_source_recorded_once(monkeypatch):
    """A supported file that is also a requirements source is fetched and
    recorded at most once across both PR fetch loops."""
    contents = {"src/app.py": "x = 1\n", "spec.md": "# REQ-1\n"}
    host = FakeHost(contents)
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: host)
    _stub_sast(monkeypatch)

    runtime = _runtime([".specify/"])
    target = "acme/widget#3"
    result = make_scope_node(runtime)(_state(target, ReviewScope.PR))
    assert host.fetched == ["src/app.py", "spec.md"]
    assert result["excluded_files"] == []


def test_pr_scope_requirements_source_excluded_and_not_fetched(monkeypatch):
    contents = {"src/app.py": "x = 1\n", "docs/requirements.md": "# REQ-1\n"}
    host = FakeHost(contents)
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: host)
    _stub_sast(monkeypatch)

    runtime = _runtime(["docs/"])
    target = "acme/widget#3"
    result = make_scope_node(runtime)(_state(target, ReviewScope.PR))
    assert host.fetched == ["src/app.py"]
    assert _patterns(result["excluded_files"]) == {"docs/requirements.md": "docs/"}


def test_pr_scope_unsupported_file_under_excluded_dir_not_recorded(monkeypatch):
    contents = {"src/app.py": "x = 1\n", ".specify/data.cob": "IDENTIFICATION DIVISION.\n"}
    host = FakeHost(contents)
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: host)
    _stub_sast(monkeypatch)

    runtime = _runtime([".specify/"])
    target = "acme/widget#3"
    result = make_scope_node(runtime)(_state(target, ReviewScope.PR))
    assert result["excluded_files"] == []
    assert ".specify/data.cob" in result["skipped_languages"]
    assert host.fetched == ["src/app.py"]


def test_sast_receives_post_exclusion_file_set(exclusion_tree, monkeypatch):
    seen = _stub_sast(monkeypatch)
    runtime = _runtime([".specify/", "*.min.js", "docs/"])
    make_scope_node(runtime)(_state(str(exclusion_tree)))
    assert sorted(seen) == ["README.md", "src/app.py"]


def test_exclusion_logged_once_with_pattern_counts(exclusion_tree, monkeypatch):
    _stub_sast(monkeypatch)
    events: list[str] = []
    runtime = _runtime([".specify/", "*.min.js"])
    monkeypatch.setattr(runtime.log, "info", lambda msg, **_kw: events.append(msg))
    make_scope_node(runtime)(_state(str(exclusion_tree)))
    assert [
        e for e in events if e.startswith("scope: excluded")
    ] == ["scope: excluded 2 file(s): .specify/ (1), *.min.js (1)"]


def test_no_exclusion_log_line_when_nothing_excluded(exclusion_tree, monkeypatch):
    _stub_sast(monkeypatch)
    events: list[str] = []
    runtime = _runtime([])
    monkeypatch.setattr(runtime.log, "info", lambda msg, **_kw: events.append(msg))
    make_scope_node(runtime)(_state(str(exclusion_tree)))
    assert not [e for e in events if e.startswith("scope: excluded")]
