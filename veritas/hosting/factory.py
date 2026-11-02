"""Hosting client construction (T110/T111, FR-002, FR-028).

``run_review`` calls ``build_hosting_client`` for a PR review before the graph
is built: the read-only fetcher goes to the scope node and, only when
``--post`` is set, the poster goes to the render node.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from veritas.hosting.github import GitHubFetcher, GitHubPoster
from veritas.hosting.gitlab import GitLabFetcher, GitLabPoster
from veritas.review import ReviewFatalError


class HostingClients(NamedTuple):
    """The read-only fetcher the scope node uses and the poster ``--post`` uses."""

    fetcher: Any
    poster: Any


def build_hosting_client(settings, provider: str, log) -> HostingClients:
    """Construct the hosting fetcher and poster for the parsed PR provider."""
    if provider == "github":
        token = settings.github_token
        if not token:
            raise ReviewFatalError(
                "PR review requires a GitHub token: set VERITAS_GITHUB_TOKEN "
                "(never commit tokens; see contracts/cli.md)."
            )
        return HostingClients(GitHubFetcher(token, log=log), GitHubPoster(token, log=log))
    if provider == "gitlab":
        token = settings.gitlab_token
        if not token:
            raise ReviewFatalError(
                "PR review requires a GitLab token: set VERITAS_GITLAB_TOKEN "
                "(never commit tokens; see contracts/cli.md)."
            )
        return HostingClients(
            GitLabFetcher(token, log=log, base_url=settings.gitlab_url),
            GitLabPoster(token, log=log, base_url=settings.gitlab_url),
        )
    raise ReviewFatalError(f"unsupported hosting provider: {provider}")
