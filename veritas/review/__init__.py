from __future__ import annotations


class ReviewFatalError(RuntimeError):
    """Fatal error (bad scope/target, config, hosting failure, --post misuse,
    missing credentials) — exits 1 with a clear stderr diagnostic and no report
    (contracts/cli.md). Raised before the graph, or by the scope node, the
    first node, when the PR cannot be fetched (T121)."""


class ReviewNotFoundError(ReviewFatalError):
    """Target does not exist or is not readable (US2 edge case)."""


class ContentWithheldError(RuntimeError):
    """The hosting API answered a file request without the file's content
    (T124): GitHub's ``encoding: "none"`` for a file over 1 MB, or GitLab's
    empty content for a non-empty file. Not a request failure: the scope node
    records the file as excluded with ``reason`` instead of reviewing it as
    empty, and the run goes on.

    Defined here, outside ``veritas.hosting``, so the scope node can name it
    through ``fetcher.content_withheld`` as it names ``fetcher.api_error``.
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{reason} (path: {path})")
        self.path = path
        self.reason = reason
