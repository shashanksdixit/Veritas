"""Integration tests — an incomplete run's verdict is qualified end to end (T140, FR-015, FR-027).

Each run goes through run_review. The verdict stays as derived in
last-report.json; stdout, the Markdown report and the posted PR comment say it
covers only what was reviewed when the run is incomplete, and say nothing extra
when it is complete.
"""

import json
from pathlib import Path

import pytest

from tests.conftest import EmptyRepoRoot, FakeLLM, default_fake_llm

from veritas.config.constants import LAST_REPORT_JSON
from veritas.hosting.factory import HostingClients
from veritas.models.entities import Report, ReviewScope, Verdict
from veritas.review.graph import run_review

QUALIFIER = "(incomplete review: covers only what was reviewed)"
NOTE_FRAGMENT = "verdict here is not a clean bill of health"


class AllCallsFail(FakeLLM):
    """Every LLM call fails as a rejected key does."""

    def complete(self, system, user):
        raise RuntimeError("openai 401: invalid api key")


class CodeQualityFails(FakeLLM):
    """Every review type answers except code-quality, whose call fails."""

    def complete(self, system, user):
        if "code-quality" in system.lower():
            raise RuntimeError("provider exploded")
        return super().complete(system, user)


def _stdout_verdict(stdout: str) -> str:
    (line,) = [line for line in stdout.splitlines() if line.startswith("Verdict:")]
    return line


def _last_report() -> tuple[Report, dict]:
    text = Path(LAST_REPORT_JSON).read_text(encoding="utf-8")
    return Report.model_validate_json(text), json.loads(text)


def test_run_whose_llm_calls_all_fail_is_not_reported_as_a_bare_clean(sample_project, settings, capsys):
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=AllCallsFail())

    assert outcome.exit_code == 2
    report, raw = _last_report()
    assert report.run.report_status.value == "incomplete"
    assert report.code_findings == []
    assert report.requirement_findings == []
    assert report.summary.verdict is Verdict.CLEAN
    assert raw["summary"]["verdict"] == "Clean"

    assert _stdout_verdict(capsys.readouterr().out) == f"Verdict: Clean {QUALIFIER}"
    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert f"**Verdict**: `Clean` {QUALIFIER}\n" in markdown
    assert NOTE_FRAGMENT in markdown


def test_requires_modification_from_reviewed_parts_is_kept_and_qualified(sample_project, settings, capsys):
    llm = CodeQualityFails(default_fake_llm().responses)
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)

    assert outcome.exit_code == 2
    report, _ = _last_report()
    assert report.run.report_status.value == "incomplete"
    assert report.summary.verdict is Verdict.REQUIRES_MODIFICATION

    assert _stdout_verdict(capsys.readouterr().out) == f"Verdict: RequiresModification {QUALIFIER}"
    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert f"**Verdict**: `RequiresModification` {QUALIFIER}\n" in markdown


def test_complete_run_verdict_is_unqualified(sample_project, settings, capsys):
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm())

    assert outcome.exit_code == 0
    report, _ = _last_report()
    assert report.run.report_status.value == "complete"

    verdict = report.summary.verdict.value
    assert _stdout_verdict(capsys.readouterr().out) == f"Verdict: {verdict}"
    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert f"**Verdict**: `{verdict}`\n" in markdown
    assert "incomplete review" not in markdown


class RecordingHost(EmptyRepoRoot):
    """PR fetching works; every posted comment body is recorded."""

    provider = "github"

    def __init__(self):
        self.posted: list[str] = []

    def head_sha(self, owner, repo, number):
        return "abcd1234"

    def list_pr_files(self, owner, repo, number):
        return [{"filename": "src/app.py"}]

    def get_file_contents(self, owner, repo, path, ref):
        return "def f():\n    return 1\n"

    def post_comment(self, owner, repo, number, body):
        self.posted.append(body)


@pytest.fixture
def host(monkeypatch):
    from veritas.review import graph as graph_module

    fake = RecordingHost()
    monkeypatch.setattr(graph_module, "build_hosting_client", lambda _s, _p, _l: HostingClients(fake, fake))
    return fake


def test_posted_body_of_an_incomplete_pr_review_carries_the_qualified_verdict(host, settings):
    outcome = run_review(settings, ReviewScope.PR, "acme/widget#3", post=True, llm=AllCallsFail())

    assert outcome.exit_code == 2
    (body,) = host.posted
    assert f"**Verdict**: `Clean` {QUALIFIER}\n" in body
    assert NOTE_FRAGMENT in body
