"""PR ref resolver (T031) — parses ``--target`` for ``--scope pr``.

Accepted patterns (contracts/hosting-api.md):
  * ``owner/repo#123``                  → GitHub PR #123
  * ``https://github.com/owner/repo/pull/123`` → GitHub PR #123
  * ``group/project!123``               → GitLab MR #123
  * ``https://gitlab.com/owner/repo/-/merge_requests/123`` → GitLab MR #123
A bare number is always rejected (ambiguous; never resolved via local git state
or a config default).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_GITHUB_SHORT = re.compile(r"^([^/\s]+)/([^/\s#]+)#(\d+)$")
_GITLAB_SHORT = re.compile(r"^([^/\s]+)/([^/\s!]+)!(\d+)$")
_GITHUB_URL = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")
_GITLAB_URL = re.compile(r"^https?://[^/]+/([^/]+)/(.+)?/-/merge_requests/(\d+)$")
_BARE_NUMBER = re.compile(r"^\d+$")


class UnresolvableTarget(ValueError):
    """Raised when a PR target cannot be resolved (bare number, malformed)."""


@dataclass(frozen=True)
class ParsedPR:
    provider: str  # "github" | "gitlab"
    owner: str
    repo: str
    number: int

    @property
    def ref(self) -> str:
        sep = "#" if self.provider == "github" else "!"
        return f"{self.owner}/{self.repo}{sep}{self.number}"


def parse_pr_target(target: str) -> ParsedPR:
    target = target.strip()
    match = _GITHUB_URL.match(target)
    if match:
        return ParsedPR("github", match.group(1), match.group(2), int(match.group(3)))
    match = _GITLAB_URL.match(target)
    if match:
        owner = match.group(1)
        repo_candidate = match.group(2)
        repo = repo_candidate.rstrip("/-/") if repo_candidate else owner
        return ParsedPR("gitlab", owner, repo, int(match.group(3)))
    match = _GITHUB_SHORT.match(target)
    if match:
        return ParsedPR("github", match.group(1), match.group(2), int(match.group(3)))
    match = _GITLAB_SHORT.match(target)
    if match:
        return ParsedPR("gitlab", match.group(1), match.group(2), int(match.group(3)))
    if _BARE_NUMBER.match(target):
        raise UnresolvableTarget(
            f"Bare number '{target}' is ambiguous and cannot be resolved to a project. "
            "Use the full PR reference: 'owner/repo#N' (GitHub) or 'group/project!N' (GitLab)."
        )
    raise UnresolvableTarget(
        f"Unrecognized PR target '{target}'. Expected 'owner/repo#N' (GitHub), "
        "'group/project!N' (GitLab), or a full PR/MR URL."
    )