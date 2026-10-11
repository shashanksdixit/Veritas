"""Hosting client wiring (T110): fetcher/poster split, token errors pinned.

``build_hosting_client`` still checks the token and builds the clients where it
did before T110; it now returns a ``HostingClients`` pair. The scope node reads
through the fetcher only and ``--post`` writes through the poster only.
"""

from __future__ import annotations

import pytest

from veritas.config.settings import Settings
from veritas.hosting import github, gitlab
from veritas.hosting.github import GitHubFetcher, GitHubPoster
from veritas.hosting.gitlab import GitLabFetcher, GitLabPoster
from veritas.models.entities import ReviewScope
from veritas.review import ReviewFatalError
from veritas.review.graph import run_review
from veritas.review.nodes import scope as scope_module
from veritas.review.nodes.scope import HostingClients, build_hosting_client
from veritas.utils.logging import Log

from tests.conftest import default_fake_llm

GITHUB_TOKEN_MISSING = (
    "PR review requires a GitHub token: set VERITAS_GITHUB_TOKEN "
    "(never commit tokens; see contracts/cli.md)."
)
GITLAB_TOKEN_MISSING = (
    "PR review requires a GitLab token: set VERITAS_GITLAB_TOKEN "
    "(never commit tokens; see contracts/cli.md)."
)


def _settings_without_tokens() -> Settings:
    return Settings(api_key="test-key", base_url="https://openrouter.ai/api/v1")


@pytest.mark.parametrize(
    ("provider", "message"),
    [("github", GITHUB_TOKEN_MISSING), ("gitlab", GITLAB_TOKEN_MISSING)],
)
def test_missing_token_keeps_todays_exact_message(provider, message):
    with pytest.raises(ReviewFatalError) as exc:
        build_hosting_client(_settings_without_tokens(), provider, Log())
    assert str(exc.value) == message


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("acme/widget#3", GITHUB_TOKEN_MISSING),
        ("group/project!3", GITLAB_TOKEN_MISSING),
    ],
)
def test_cli_prints_the_missing_token_line_and_exits_1(monkeypatch, tmp_path, target, message):
    from typer.testing import CliRunner

    from veritas.cli.app import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERITAS_API_KEY", "test-key")

    result = CliRunner().invoke(app, ["review", "--scope", "pr", "--target", target])

    assert result.exit_code == 1
    assert result.stderr.strip().splitlines()[-1] == f"[error] {message}"
    assert "Traceback" not in result.stderr


def test_github_builds_a_read_only_fetcher_and_a_poster(settings):
    clients = build_hosting_client(settings, "github", Log())
    assert isinstance(clients, HostingClients)
    assert isinstance(clients.fetcher, GitHubFetcher)
    assert isinstance(clients.poster, GitHubPoster)
    assert clients.fetcher.client.event_hooks["request"] == [github._reject_writes]
    assert clients.poster.client.event_hooks["request"] == []


def test_gitlab_builds_a_read_only_fetcher_and_a_poster_on_the_configured_instance():
    settings = Settings(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        gitlab_token="glpat-test",
        gitlab_url="https://gitlab.example.com",
    )
    clients = build_hosting_client(settings, "gitlab", Log())
    assert isinstance(clients.fetcher, GitLabFetcher)
    assert isinstance(clients.poster, GitLabPoster)
    assert clients.fetcher.base_url == "https://gitlab.example.com/api/v4"
    assert clients.poster.base_url == "https://gitlab.example.com/api/v4"
    assert clients.fetcher.client.event_hooks["request"] == [gitlab._reject_writes]
    assert clients.poster.client.event_hooks["request"] == []


class _ReadOnlyHost:
    """Fetcher fake: has no posting method, so a post sent here would fail."""

    def head_sha(self, owner, repo, number):
        return "abcd1234"

    def list_pr_files(self, owner, repo, number):
        return [{"filename": "src/app.py"}]

    def get_file_contents(self, owner, repo, path, ref):
        return "def f():\n    return 1\n"


class _PostOnlyHost:
    """Poster fake: has no reading method, so a fetch sent here would fail."""

    def __init__(self):
        self.posted: list[tuple] = []

    def post_comment(self, owner, repo, number, body):
        self.posted.append((owner, repo, number, body))


def test_scope_reads_through_the_fetcher_and_post_goes_through_the_poster(
    monkeypatch, settings, tmp_path
):
    poster = _PostOnlyHost()
    monkeypatch.setattr(
        scope_module,
        "build_hosting_client",
        lambda _s, _p, _l: HostingClients(_ReadOnlyHost(), poster),
    )

    outcome = run_review(
        settings,
        ReviewScope.PR,
        "acme/widget#3",
        output=str(tmp_path / "report.md"),
        post=True,
        llm=default_fake_llm(),
    )

    assert outcome.exit_code == 0
    assert len(poster.posted) == 1
    owner, repo, number, body = poster.posted[0]
    assert (owner, repo, number) == ("acme", "widget", 3)
    assert body.startswith("<!-- veritas-report-schema:")
