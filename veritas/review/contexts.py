"""Per-node contexts for the review graph (T111, FR-026).

Each node factory receives exactly one of these, built once in ``run_review``
before the graph is compiled. They are frozen and slotted, so nothing a node
does can change what another node sees, and no node can gain a capability by
assigning a new attribute:

* ``ReviewContext`` — the analysis nodes (context, the five review nodes,
  verify): the LLM client and the log, nothing else.
* ``ScopeContext`` — the scope node only: what to read and how to batch it,
  plus the read-only hosting fetcher for a PR.
* ``RenderContext`` — the render node only: where to write, the suppression
  store and the hosting poster. No other context holds a write capability.
"""

from __future__ import annotations

from dataclasses import dataclass

from veritas.hosting.github import GitHubFetcher, GitHubPoster
from veritas.hosting.gitlab import GitLabFetcher, GitLabPoster
from veritas.hosting.resolver import ParsedPR
from veritas.llm.client import LLMClient
from veritas.suppression.store import SuppressionStore
from veritas.utils.logging import Log


@dataclass(frozen=True, slots=True)
class ReviewContext:
    llm: LLMClient
    log: Log


@dataclass(frozen=True, slots=True)
class ScopeContext:
    log: Log
    exclude: tuple[str, ...]
    batch_chars: int
    max_batches: int
    opengrep_rules: str
    opengrep_jobs: int
    fetcher: GitHubFetcher | GitLabFetcher | None
    pr: ParsedPR | None


@dataclass(frozen=True, slots=True)
class RenderContext:
    log: Log
    report_path: str
    batch_chars: int
    max_batches: int
    store: SuppressionStore
    poster: GitHubPoster | GitLabPoster | None
    pr: ParsedPR | None
