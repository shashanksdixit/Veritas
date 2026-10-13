"""Integration tests — project-aware review (T057, FR-009/FR-010/FR-011)."""

from pathlib import Path

import pytest

from tests.conftest import FakeLLM, default_fake_llm, fake_secret

from veritas.models.entities import ReviewScope
from veritas.review.graph import run_review


def test_project_context_flows_into_prompts(sample_project, settings):
    llm = default_fake_llm()
    run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    all_user = "\n".join(user for _sys, user in llm.calls)

    assert "Python >=3.12" in all_user
    assert "Prefer early returns over nested ifs." in all_user
    assert "Flag any use of eval()" in all_user


def test_requirements_doc_reaches_requirements_prompt(sample_project, settings):
    llm = default_fake_llm()
    out = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    user = next(user for sys, user in llm.calls if "requirements-traceability" in sys.lower())
    assert "REQ-1" in user
    assert "SATISFIED" in user
    from pathlib import Path

    md = Path(str(out.report_path)).read_text(encoding="utf-8")
    assert "REQ-1" in md
    assert "- `src/app.py:2`" in md  # verified requirement evidence


def test_empty_project_uses_context_note(tmp_path, settings):
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.py").write_text("x = 1\n", encoding="utf-8")
    llm = FakeLLM({})
    out = run_review(settings, ReviewScope.PROJECT, str(tmp_path), llm=llm)
    assert out.exit_code == 0
    user_prompts = [user for _sys, user in llm.calls if user]
    assert any("Project context unavailable" in u for u in user_prompts)


# --- the report states the project context (T117, FR-009) ---


def _outputs(outcome, capsys):
    from pathlib import Path

    from veritas.config.constants import LAST_REPORT_JSON
    from veritas.models.entities import Report

    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    return report, md.splitlines(), capsys.readouterr().out.splitlines()


def test_local_project_reports_detected_context_with_a_relative_origin(
    tmp_path, settings, capsys
):
    from veritas.models.entities import ProjectContextStatus

    # The integration conftest runs every test from tmp_path, so the project
    # below it must be shown as "proj", never as tmp_path's absolute path.
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "lib.py").write_text("x = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nrequires-python = ">=3.12"\n', encoding="utf-8"
    )
    (project / "AGENTS.md").write_text("Prefer early returns.\n", encoding="utf-8")

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=FakeLLM({}))
    report, md_lines, stdout_lines = _outputs(outcome, capsys)

    assert report.run.project_context_status is ProjectContextStatus.DETECTED
    assert report.run.project_context_sources == ["pyproject.toml", "AGENTS.md"]
    assert report.run.project_context_reason is None
    assert report.run.project_context_origin == "proj"
    assert "- **Project context**: detected — pyproject.toml, AGENTS.md (from proj)" in md_lines
    assert "Context: detected (pyproject.toml, AGENTS.md)" in stdout_lines
    assert not any(str(tmp_path) in line for line in md_lines if "Project context" in line)


def test_empty_project_reports_context_unavailable_with_the_reason(tmp_path, settings, capsys):
    from veritas.models.entities import ProjectContextStatus

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.py").write_text("x = 1\n", encoding="utf-8")

    outcome = run_review(settings, ReviewScope.PROJECT, str(tmp_path), llm=FakeLLM({}))
    report, md_lines, stdout_lines = _outputs(outcome, capsys)

    reason = "no manifest, conventions file or rules file found"
    assert report.run.project_context_status is ProjectContextStatus.UNAVAILABLE
    assert report.run.project_context_sources == []
    assert report.run.project_context_reason == reason
    assert report.run.project_context_origin == "."
    assert f"- **Project context**: unavailable — {reason}" in md_lines
    assert f"Context: unavailable — {reason}" in stdout_lines


def test_pr_review_reports_context_unavailable_not_read_yet(monkeypatch, settings, capsys):
    from veritas.hosting.factory import HostingClients
    from veritas.models.entities import ProjectContextStatus

    class _Host:
        provider = "github"

        def head_sha(self, owner, repo, number):
            return "abcd1234"

        def list_pr_files(self, owner, repo, number):
            return [{"filename": "src/app.py"}]

        def get_file_contents(self, owner, repo, path, ref):
            return "x = 1\n"

    monkeypatch.setattr(
        "veritas.review.graph.build_hosting_client",
        lambda _s, _p, _l: HostingClients(_Host(), _Host()),
    )

    outcome = run_review(settings, ReviewScope.PR, "acme/widget#3", llm=FakeLLM({}))
    report, md_lines, stdout_lines = _outputs(outcome, capsys)

    assert report.run.project_context_status is ProjectContextStatus.UNAVAILABLE
    assert report.run.project_context_sources == []
    assert report.run.project_context_reason == "not read for PR reviews yet"
    assert report.run.project_context_origin is None
    assert "- **Project context**: unavailable — not read for PR reviews yet" in md_lines
    assert "Context: unavailable — not read for PR reviews yet" in stdout_lines


# --- project-context problems are stated, not silent (T118, FR-009) ---

_GO_MOD = "module example.com/demo\n\ngo 1.22\n"
_NOT_UTF8_PYPROJECT = b'[project]\nname = "caf\xe9"\n'
_BIG = "x" * 200_001


def _project(tmp_path, files: dict[str, str | bytes]) -> Path:
    """A project at tmp_path/proj with one Python file to review plus ``files``."""
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "lib.py").write_text("x = 1\n", encoding="utf-8")
    for name, content in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_bytes(content.encode("utf-8"))
    return project


def _make_unreadable(monkeypatch, path: Path) -> None:
    """Reading ``path`` raises PermissionError; every other file reads normally."""
    for method in ("read_text", "read_bytes"):
        original = getattr(Path, method)

        def failing(self, *args, _original=original, **kwargs):
            if self == path:
                raise PermissionError(13, "Permission denied", str(self))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, failing)


_PROBLEM_CASES = [
    # id, files, unreadable file, status, sources, reason
    (
        "too-large-partial",
        {"package.json": _BIG, "go.mod": _GO_MOD},
        None,
        "partial",
        ["go.mod"],
        "package.json over 200000 bytes, skipped",
    ),
    (
        "too-large-pyproject-unavailable",
        {"pyproject.toml": '[project]\nrequires-python = ">=3.12"\n' + "#" * 200_001},
        None,
        "unavailable",
        [],
        "pyproject.toml over 200000 bytes, skipped",
    ),
    (
        "not-utf8-partial",
        {"pyproject.toml": _NOT_UTF8_PYPROJECT, "AGENTS.md": "Prefer early returns.\n"},
        None,
        "partial",
        ["AGENTS.md"],
        "pyproject.toml is not valid UTF-8, skipped",
    ),
    (
        "not-utf8-unavailable",
        {"pyproject.toml": _NOT_UTF8_PYPROJECT},
        None,
        "unavailable",
        [],
        "pyproject.toml is not valid UTF-8, skipped",
    ),
    (
        "invalid-toml-partial",
        {"pyproject.toml": "[project\n", "go.mod": _GO_MOD},
        None,
        "partial",
        ["go.mod"],
        "pyproject.toml is not valid TOML",
    ),
    (
        "invalid-toml-unavailable",
        {"pyproject.toml": "[project\n"},
        None,
        "unavailable",
        [],
        "pyproject.toml is not valid TOML",
    ),
    (
        "unreadable-partial",
        {"AGENTS.md": "Prefer early returns.\n", "go.mod": _GO_MOD},
        "AGENTS.md",
        "partial",
        ["go.mod"],
        "AGENTS.md could not be read, skipped",
    ),
    (
        "unreadable-unavailable",
        {"go.mod": _GO_MOD},
        "go.mod",
        "unavailable",
        [],
        "go.mod could not be read, skipped",
    ),
    (
        "bad-layout-partial",
        {"pyproject.toml": 'project = "x"\n', "go.mod": _GO_MOD},
        None,
        "partial",
        ["go.mod"],
        "pyproject.toml has an unexpected [project] layout",
    ),
    (
        "bad-layout-unavailable",
        {"pyproject.toml": "[project]\nrequires-python = 3.12\n"},
        None,
        "unavailable",
        [],
        "pyproject.toml has an unexpected [project] layout",
    ),
    # A truncated file still contributes, so truncation is never unavailable.
    (
        "truncated-partial",
        {"AGENTS.md": "a" * 4001},
        None,
        "partial",
        ["AGENTS.md"],
        "AGENTS.md truncated to 4000 characters",
    ),
]


@pytest.mark.parametrize(
    "files, unreadable, status, sources, reason",
    [case[1:] for case in _PROBLEM_CASES],
    ids=[case[0] for case in _PROBLEM_CASES],
)
def test_each_problem_is_stated_and_the_review_still_completes(
    tmp_path, settings, capsys, monkeypatch, files, unreadable, status, sources, reason
):
    from veritas.models.entities import ProjectContextStatus

    project = _project(tmp_path, files)
    if unreadable is not None:
        _make_unreadable(monkeypatch, project / unreadable)

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=FakeLLM({}))
    report, _md_lines, _stdout_lines = _outputs(outcome, capsys)

    assert outcome.exit_code == 0
    assert report.run.project_context_status is ProjectContextStatus(status)
    assert report.run.project_context_sources == sources
    assert report.run.project_context_reason == reason
    assert report.run.project_context_origin == "proj"


def test_two_problems_render_in_the_markdown_header_and_compact_output(
    tmp_path, settings, capsys
):
    from veritas.models.entities import ProjectContextStatus

    project = _project(
        tmp_path,
        {"pyproject.toml": _NOT_UTF8_PYPROJECT, "package.json": _BIG, "go.mod": _GO_MOD},
    )

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=FakeLLM({}))
    report, md_lines, stdout_lines = _outputs(outcome, capsys)

    reason = "pyproject.toml is not valid UTF-8, skipped; package.json over 200000 bytes, skipped"
    assert report.run.project_context_status is ProjectContextStatus.PARTIAL
    assert report.run.project_context_reason == reason
    assert f"- **Project context**: partial — go.mod (from proj); {reason}" in md_lines
    assert f"Context: partial (go.mod) — {reason}" in stdout_lines


def test_a_blank_agents_md_does_not_hide_style_md(tmp_path, settings, capsys):
    project = _project(tmp_path, {"AGENTS.md": "  \n\t\n", "STYLE.md": "Use four-space indentation.\n"})
    llm = FakeLLM({})

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=llm)
    report, _md_lines, _stdout_lines = _outputs(outcome, capsys)

    assert report.run.project_context_sources == ["STYLE.md"]
    assert any("Use four-space indentation." in user for _sys, user in llm.calls)


def test_conventions_and_rules_reach_the_model_redacted(tmp_path, settings):
    token = fake_secret("ghp_", "t" * 36)
    project = _project(
        tmp_path,
        {
            "AGENTS.md": f"Deploy with {token} only from CI.\n",
            ".veritas/rules.md": f"Flag any use of {token}.\n",
        },
    )
    llm = FakeLLM({})

    run_review(settings, ReviewScope.PROJECT, str(project), llm=llm)
    prompts = "\n".join(system + "\n" + user for system, user in llm.calls)

    assert llm.calls
    assert token not in prompts
    assert "Deploy with [REDACTED] only from CI." in prompts
    assert "Flag any use of [REDACTED]." in prompts


def test_a_bom_pyproject_is_detected_with_no_problem(tmp_path, settings, capsys):
    """T118: a pyproject.toml saved with a UTF-8 byte-order mark is read normally."""
    from veritas.models.entities import ProjectContextStatus

    project = _project(
        tmp_path, {"pyproject.toml": b'\xef\xbb\xbf[project]\nrequires-python = ">=3.12"\n'}
    )
    llm = FakeLLM({})

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=llm)
    report, _md_lines, _stdout_lines = _outputs(outcome, capsys)

    assert report.run.project_context_status is ProjectContextStatus.DETECTED
    assert report.run.project_context_sources == ["pyproject.toml"]
    assert report.run.project_context_reason is None
    assert any("Python >=3.12" in user for _sys, user in llm.calls)
