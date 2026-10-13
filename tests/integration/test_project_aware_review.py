"""Integration tests — project-aware review (T057, FR-009/FR-010/FR-011)."""

from tests.conftest import FakeLLM, default_fake_llm

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
