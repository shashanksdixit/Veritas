"""Structured logging for Veritas (constitution Principle VIII).

Diagnostics and errors go to stderr (Principle VI: stdout is for results only).
LLM calls are logged structurally with latency, model, and token usage.

Severity layout:
  * ``[info]``  — normal diagnostics
  * ``[warn]``  — degradations, non-fatal problems (e.g. OpenGrep missing, ZDR off)
  * ``[error]`` — fatal / partial-run errors
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, TextIO


class Log:
    """Minimal dependency-free structured logger writing to stderr.

    Structured (JSON) metadata can be attached to any record; when ``verbose``
    is set the structured payload is rendered inline for human inspection.
    """

    def __init__(self, stream: TextIO | None = None, verbose: bool = False) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.verbose = verbose

    def _emit(self, level: str, message: str, **structured: Any) -> None:
        record: dict[str, Any] = {
            "level": level,
            "message": message,
            "ts": int(time.time()),
        }
        record.update(structured)
        if self.verbose:
            line = f"[{level}] {message}"
            if structured:
                line += " " + json.dumps(structured, default=str, sort_keys=True)
        else:
            line = f"[{level}] {message}"
        print(line, file=self.stream, flush=True)

    def info(self, message: str, **structured: Any) -> None:
        self._emit("info", message, **structured)

    def warn(self, message: str, **structured: Any) -> None:
        self._emit("warn", message, **structured)

    def error(self, message: str, **structured: Any) -> None:
        self._emit("error", message, **structured)

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
        """Structured LLM call record (constitution Principle VIII)."""
        structured: dict[str, Any] = {"model": model, "latency_ms": round(latency_ms, 1)}
        if prompt_tokens is not None:
            structured["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            structured["completion_tokens"] = completion_tokens
        if total_tokens is not None:
            structured["total_tokens"] = total_tokens
        level = "error" if error else "info"
        msg = f"LLM call {model} ({latency_ms:.0f} ms)"
        if error:
            msg += f" failed: {error}"
        self._emit(level, msg, **structured)


def get_log(verbose: bool = False) -> Log:
    """Module-level helper returning a logger instance."""
    return Log(verbose=verbose)