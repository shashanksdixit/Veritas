"""Integration tests — end-to-end review runs + CLI behaviour (T043/T048/T051)."""

from pathlib import Path

import pytest

from tests.conftest import FakeLLM, default_fake_llm

from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.models.entities import Report, ReviewScope
from veritas.review.graph import run_review


def test_project_review_succeeds_and_writes_report(sample_project, settings):
    llm = default_fake_llm()
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    assert outcome.exit_code == 0
    report_path = Path(str(outcome.report_path))
    assert report_path.is_file()
    md = report_path.read_text(encoding="utf-8")
    assert md.startswith("<!-- veritas-report-schema: 1.2.0 -->")
    assert "# Veritas Code Review" in md
    assert "## Code Findings" in md
    assert "Unused import os" in md


def test_sidecar_json_written_for_suppress(sample_project, settings):
    llm = default_fake_llm()
    run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    sidecar = Path(LAST_REPORT_JSON)
    assert sidecar.is_file()
    report = Report.model_validate_json(sidecar.read_text(encoding="utf-8"))
    assert report.run.report_status.value == "complete"
    assert report.code_findings


def test_file_scope_single_file(tmp_path, settings):
    target = tmp_path / "solo.py"
    target.write_text("x = 1\n", encoding="utf-8")
    llm = FakeLLM({})
    outcome = run_review(settings, ReviewScope.FILE, str(target), llm=llm)
    assert outcome.exit_code == 0


def test_missing_target_is_fatal_no_report(tmp_path, settings):
    from veritas.review import ReviewFatalError

    with pytest.raises(ReviewFatalError):
        run_review(settings, ReviewScope.PROJECT, str(tmp_path / "does-not-exist"), llm=FakeLLM({}))
    assert list(Path(".").glob("veritas-report-*.md")) == []


def test_post_with_non_pr_rejected_before_work(sample_project, settings):
    from veritas.review import ReviewFatalError

    with pytest.raises(ReviewFatalError, match="--scope pr"):
        run_review(
            settings,
            ReviewScope.PROJECT,
            str(sample_project),
            post=True,
            llm=FakeLLM({}),
        )


def test_bare_pr_number_is_fatal(settings):
    from veritas.review import ReviewFatalError

    with pytest.raises(ReviewFatalError, match="ambiguous"):
        run_review(settings, ReviewScope.PR, "123", llm=FakeLLM({}))


def test_pr_review_posts_when_requested(monkeypatch, settings):
    from veritas.review.nodes import scope as scope_module
    from veritas.review.nodes.scope import _normalize_rel

    class FakeHost:
        provider = "github"

        def __init__(self):
            self.posted = []

        def head_sha(self, owner, repo, number):
            return "abcd1234"

        def list_pr_files(self, owner, repo, number):
            return [{"filename": "src/app.py"}, {"filename": "README.md"}]

        def get_file_contents(self, owner, repo, path, ref):
            if path.startswith("src/"):
                return 'def f():\n    return 1\n'
            return "# REQ-1\n- SATISFIED: x\n"

        def post_comment(self, owner, repo, number, body):
            self.posted.append(body)

    host = FakeHost()
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: host)

    llm = default_fake_llm()
    outcome = run_review(
        settings,
        ReviewScope.PR,
        "acme/widget#3",
        post=True,
        llm=llm,
    )
    assert outcome.exit_code == 0
    assert len(host.posted) == 1
    assert host.posted[0].startswith("<!-- veritas-report-schema:")


def test_partial_failure_returns_exit_2(sample_project, settings):
    class ExplodingLLM(FakeLLM):
        def complete(self, system, user):
            if "code-quality" in system.lower():
                raise RuntimeError("provider exploded")
            return super().complete(system, user)

    llm = ExplodingLLM(default_fake_llm().responses)
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    assert outcome.exit_code == 2
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert report.run.report_status.value == "incomplete"
    assert "code_quality_node" in (report.run.error or "")
    assert "provider exploded" in (report.run.error or "")


def test_cli_invalid_scope_exits_1():
    from typer.testing import CliRunner

    from veritas.cli.app import app

    runner = CliRunner()
    result = runner.invoke(app, ["review", "--scope", "bogus", "--target", "."])
    assert result.exit_code == 1
    assert "Invalid scope" in result.stderr


def test_cli_missing_target_exits_1():
    from typer.testing import CliRunner

    from veritas.cli.app import app

    runner = CliRunner()
    result = runner.invoke(app, ["review", "--scope", "project", "--target", "nope-missing"])
    assert result.exit_code == 1
    assert "does not exist" in result.stderr


def test_cli_review_command_runs(sample_project):
    from typer.testing import CliRunner

    from veritas.cli.app import app

    settings = Settings(api_key="k")
    runner = CliRunner()

    def fake_run_review(_settings, scope_val, target, **kwargs):
        from tests.conftest import default_fake_llm

        return run_review(_settings, scope_val, target, llm=default_fake_llm(), **kwargs)

    import veritas.cli.app as cli_app

    cli_app.run_review = fake_run_review
    result = runner.invoke(app, ["review", "--scope", "project", "--target", str(sample_project)])
    assert result.exit_code == 0
    assert "Verdict:" in result.stdout
    cli_app.run_review = run_review