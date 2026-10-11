from __future__ import annotations


class ReviewFatalError(RuntimeError):
    """Pre-graph fatal error (bad scope/target, config, hosting failure, --post
    misuse, missing credentials) — exits 1 with a clear stderr diagnostic and
    no report (contracts/cli.md)."""


class ReviewNotFoundError(ReviewFatalError):
    """Target does not exist or is not readable (US2 edge case)."""