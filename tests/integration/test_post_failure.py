"""Integration tests — a failed --post never changes the exit code (T100, FR-028).

The hosting client is a fake whose post_comment raises the real
GitHubAPIError, exactly as GitHubClient._request does on an HTTP 403.
"""

from pathlib import Path

import pytest

from tests.conftest import FakeLLM, default_fake_llm, fake_secret

from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.hosting.github import GitHubAPIError
from veritas.models.entities import Report, ReviewScope
from veritas.review.graph import run_review

# Assembled at runtime so no token-shaped literal sits in the test source.
TOKEN = fake_secret("ghp_", "PostFailureToken0123456789abcdefABCD")
# What GitHub sends back on a 403; GitHubAPIError keeps it as the error detail.
RAW_BODY = (
    '{"message":"Resource not accessible by integration",'
    '"account_id":"acct-7731-internal","documentation_url":"https://docs.github.com/rest"}'
)


class ForbiddenHost:
    """PR fetching works; posting the comment fails with a 403."""

    provider = "github"

    def __init__(self):
        self.post_attempts = 0

    def head_sha(self, owner, repo, number):
        return "abcd1234"

    def list_pr_files(self, owner, repo, number):
        return [{"filename": "src/app.py"}]

    def get_file_contents(self, owner, repo, path, ref):
        return 'def f():\n    return 1\n'

    def post_comment(self, owner, repo, number, body):
        self.post_attempts += 1
        raise GitHubAPIError(403, RAW_BODY, path=f"{owner}/{repo}#{number}")


@pytest.fixture
def host(monkeypatch):
    from veritas.review.nodes import scope as scope_module

    fake = ForbiddenHost()
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: fake)
    return fake


@pytest.fixture
def token_settings() -> Settings:
    return Settings(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        github_token=TOKEN,
        gitlab_token="glpat-test",
    )


def _post_warnings(stderr: str) -> list[str]:
    return [
        line
        for line in stderr.splitlines()
        if line.startswith("[warn]") and "Failed to post report" in line
    ]


def test_complete_review_with_failed_post_exits_0(host, token_settings, capsys):
    outcome = run_review(
        token_settings, ReviewScope.PR, "acme/widget#3", post=True, llm=default_fake_llm()
    )

    assert host.post_attempts == 1
    assert outcome.exit_code == 0
    report_path = Path(str(outcome.report_path))
    assert report_path.is_file()
    assert report_path.read_text(encoding="utf-8").startswith("<!-- veritas-report-schema:")
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert report.run.report_status.value == "complete"

    warnings = _post_warnings(capsys.readouterr().err)
    assert warnings == [
        "[warn] Failed to post report as github comment: 403 Forbidden; "
        f"report saved to {outcome.report_path}"
    ]


def test_incomplete_review_with_failed_post_still_exits_2(host, token_settings, capsys):
    class OneBatchFails(FakeLLM):
        def complete(self, system, user):
            if "code-quality" in system.lower():
                raise RuntimeError("provider exploded")
            return super().complete(system, user)

    llm = OneBatchFails(default_fake_llm().responses)
    outcome = run_review(token_settings, ReviewScope.PR, "acme/widget#3", post=True, llm=llm)

    assert host.post_attempts == 1
    assert outcome.exit_code == 2
    assert Path(str(outcome.report_path)).is_file()
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert report.run.report_status.value == "incomplete"
    assert len(report.failed_batches) == 1
    assert len(_post_warnings(capsys.readouterr().err)) == 1


def test_post_failure_warning_has_no_token_or_raw_body(host, token_settings, capsys):
    run_review(token_settings, ReviewScope.PR, "acme/widget#3", post=True, llm=default_fake_llm())

    stderr = capsys.readouterr().err
    (warning,) = _post_warnings(stderr)
    assert "403 Forbidden" in warning
    for leaked in (TOKEN, RAW_BODY, "Resource not accessible", "acct-7731-internal", "https://"):
        assert leaked not in warning
    # Nowhere else on stderr either.
    assert TOKEN not in stderr
    assert "acct-7731-internal" not in stderr
