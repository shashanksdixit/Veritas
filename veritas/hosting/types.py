"""Provider-neutral values returned by the hosting fetchers (T115)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class RootEntry:
    """One entry of a repository's root directory.

    ``size`` is the file size in bytes when the provider's listing reports it
    (GitHub), and None when it does not (GitLab's tree API; directories).
    """

    name: str
    type: Literal["file", "dir"]
    size: int | None


@dataclass(frozen=True, slots=True)
class PullRefs:
    """The head and base commit SHAs of a pull/merge request (None when the
    provider's response lacks one)."""

    head_sha: str | None
    base_sha: str | None
