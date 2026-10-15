from __future__ import annotations


class ReviewFatalError(RuntimeError):
    """Fatal error (bad scope/target, config, hosting failure, --post misuse,
    missing credentials) — exits 1 with a clear stderr diagnostic and no report
    (contracts/cli.md). Raised before the graph, or by the scope node, the
    first node, when the PR cannot be fetched (T121)."""


class ReviewNotFoundError(ReviewFatalError):
    """Target does not exist or is not readable (US2 edge case)."""