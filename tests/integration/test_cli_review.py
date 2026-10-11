"""Integration tests — end-to-end review runs + CLI behaviour (T043/T048/T051)."""

from pathlib import Path

import pytest

from tests.conftest import FakeLLM, default_fake_llm

from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.models.entities import (
    Report,
    RequirementStatus,
    ReviewScope,
    Verdict,
)
from veritas.review.graph import run_review


def test_project_review_succeeds_and_writes_report(sample_project, settings):
    llm = default_fake_llm()
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    assert outcome.exit_code == 0
    report_path = Path(str(outcome.report_path))
    assert report_path.is_file()
    md = report_path.read_text(encoding="utf-8")
    assert md.startswith("<!-- veritas-report-schema: 1.9.0 -->")
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
    # The failing batch is recorded with the files it held (FR-029 batch
    # isolation), and run.error summarises it with the provider message
    # (FR-027 partial-review status).
    (failed,) = report.failed_batches
    assert (failed.review_type, failed.batch, failed.total, failed.files) == (
        "code_quality",
        1,
        1,
        ["src/app.py"],
    )
    assert "provider exploded" in failed.reason
    assert "1 LLM batch call(s) failed (code_quality: 1)" in (report.run.error or "")
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


# --- FR-007/FR-015: what the PR leaves out is not_addressed, not a gap ---


_SPEC = """# Review demo

- **FR-001**: The tool MUST print a hello greeting.
- **FR-002**: The tool MUST write every review to a file.
- **FR-003**: The tool MUST expose a health endpoint.
"""

_APP = """import os

def greet() -> None:
    print("hello")
"""


class _PrHost:
    """A PR that touches the app and its spec, and nothing else (FR-002)."""

    provider = "github"

    def __init__(self, contents: dict[str, str]) -> None:
        self._contents = contents

    def head_sha(self, owner, repo, number):
        return "abcd1234"

    def list_pr_files(self, owner, repo, number):
        return [{"filename": name} for name in self._contents]

    def get_file_contents(self, owner, repo, path, ref):
        return self._contents[path]


def _silent_code_findings(answers: str) -> FakeLLM:
    """An LLM that reports no code findings, so the verdict is decided by the
    requirement statuses alone."""
    return FakeLLM(
        {
            # Unique to the structured prompt, so it wins over the free-text key
            # below; both prompts call themselves requirements-traceability.
            "extracted from the project's spec": answers,
            "requirements-traceability": "[]",
            "code-quality": "[]",
            "security/owasp": "[]",
            "test-coverage judgment": "[]",
            "performance-reasoning": "[]",
        }
    )


def _stub_sast(monkeypatch):
    from veritas.review.nodes import scope as scope_module
    from veritas.security.opengrep import OpengrepResult

    # Ground-truth SAST findings are not what this test is about, and whether
    # Opengrep is installed would decide the verdict.
    monkeypatch.setattr(
        scope_module, "collect_sast", lambda *_a, **_k: OpengrepResult(findings=[], rules="r")
    )


def _project_with_spec(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text(_APP, encoding="utf-8")
    # A spec-kit feature spec, the shape FR-008 extracts FR-NNN lines from.
    (tmp_path / "specs" / "demo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "demo" / "spec.md").write_text(_SPEC, encoding="utf-8")
    return tmp_path


_ANSWERS = (
    '[{"id": "FR-001", "answer": "implemented", "evidence": ["src/app.py:3"],'
    ' "explanation": "greet() prints the greeting."},'
    ' {"id": "FR-002", "answer": "not_in_this_batch", "evidence": [],'
    ' "explanation": "no file writes here."},'
    ' {"id": "FR-003", "answer": "not_in_this_batch", "evidence": [],'
    ' "explanation": "no endpoint here."}]'
)


def test_a_pr_that_implements_nothing_reports_not_addressed_and_stays_clean(
    monkeypatch, settings, tmp_path
):
    """The PR ships only src/app.py and its spec: the two requirements it carries no
    code for are not_addressed, which holds the PR back no further than it was."""
    _stub_sast(monkeypatch)
    files = {"src/app.py": _APP, "specs/demo/spec.md": _SPEC}
    monkeypatch.setattr(
        "veritas.review.nodes.scope.build_hosting_client",
        lambda _s, _p, _l: _PrHost(files),
    )

    outcome = run_review(settings, ReviewScope.PR, "acme/widget#3", llm=_silent_code_findings(_ANSWERS))

    assert outcome.exit_code == 0
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))

    statuses = {f.requirement_ref: f.status for f in report.requirement_findings}
    assert statuses == {
        "FR-001": RequirementStatus.SATISFIED,
        "FR-002": RequirementStatus.NOT_ADDRESSED,
        "FR-003": RequirementStatus.NOT_ADDRESSED,
    }
    assert {
        f.explanation
        for f in report.requirement_findings
        if f.status is RequirementStatus.NOT_ADDRESSED
    } == {"No code for this requirement is part of this PR."}
    # FR-015: two requirements the PR carries no code for do not turn a clean PR
    # into one a human has to act on.
    assert report.code_findings == []
    assert report.summary.verdict is Verdict.CLEAN

    assert "2 requirement(s) are not addressed by this PR:" in md
    assert "- FR-002: The tool MUST write every review to a file." in md


def test_the_same_files_reviewed_in_project_scope_give_gaps(monkeypatch, settings, tmp_path):
    """Same spec, same answers, same files - the scope is the only difference, so
    the scope is what decides between the two statuses."""
    _stub_sast(monkeypatch)
    project = _project_with_spec(tmp_path)

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=_silent_code_findings(_ANSWERS))

    assert outcome.exit_code == 0
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))

    statuses = {f.requirement_ref: f.status for f in report.requirement_findings}
    assert statuses == {
        "FR-001": RequirementStatus.SATISFIED,
        "FR-002": RequirementStatus.GAP,
        "FR-003": RequirementStatus.GAP,
    }
    assert report.summary.verdict is Verdict.REQUIRES_MODIFICATION
    assert "requirement(s) are not addressed by this PR" not in md