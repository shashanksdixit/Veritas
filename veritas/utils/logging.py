"""Structured logging for Veritas (constitution Principle VIII).

Diagnostics and errors go to stderr (Principle VI: stdout is for results only).
Every LLM call is logged, in every run, as one line ending in a JSON object with
its model, latency and token usage (FR-019); other lines show their structured
fields only with ``--verbose``.

Severity layout:
  * ``[info]``  — normal diagnostics
  * ``[warn]``  — degradations, non-fatal problems (e.g. OpenGrep missing, ZDR off)
  * ``[error]`` — fatal / partial-run errors
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, TextIO

from veritas.utils.redaction import redact_secrets


class Log:
    """Minimal dependency-free structured logger writing to stderr.

    Structured (JSON) metadata can be attached to any record. An LLM-call record
    always renders its fields inline as a sorted JSON object; any other record
    renders them only when ``verbose`` is set.

    Every line is written atomically — one ``write`` call carrying the full line
    plus its newline, under a lock — so lines emitted concurrently by parallel
    review nodes never interleave.

    Every line, structured payload included, passes through ``redact_secrets()``
    before it is written (FR-013), so no caller has to remember to redact.
    """

    def __init__(self, stream: TextIO | None = None, verbose: bool = False) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.verbose = verbose
        self._lock = threading.Lock()

    def _emit(self, level: str, message: str, structured: dict[str, Any], *, inline: bool) -> None:
        """The single write path; ``inline`` appends ``structured`` as sorted JSON."""
        line = f"[{level}] {message}"
        if inline and structured:
            line += " " + json.dumps(structured, default=str, sort_keys=True)
        payload = redact_secrets(line) + "\n"
        with self._lock:
            self.stream.write(payload)
            self.stream.flush()

    def info(self, message: str, **structured: Any) -> None:
        self._emit("info", message, structured, inline=self.verbose)

    def warn(self, message: str, **structured: Any) -> None:
        self._emit("warn", message, structured, inline=self.verbose)

    def error(self, message: str, **structured: Any) -> None:
        self._emit("error", message, structured, inline=self.verbose)

    def llm_call(
        self,
        model: str,
        latency_ms: float,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        error: str | None = None,
    ) -> None:
        """Structured LLM call record (constitution Principle VIII, FR-019).

        Written in every run, with or without ``verbose``. All five fields are
        always present; a count the provider did not report is null. The error
        text, when given, is in the message only.
        """
        structured: dict[str, Any] = {
            "model": model,
            "latency_ms": round(latency_ms, 1),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        level = "error" if error else "info"
        msg = f"LLM call {model} ({latency_ms:.0f} ms)"
        if error:
            msg += f" failed: {error}"
        self._emit(level, msg, structured, inline=True)


def get_log(verbose: bool = False) -> Log:
    """Module-level helper returning a logger instance."""
    return Log(verbose=verbose)