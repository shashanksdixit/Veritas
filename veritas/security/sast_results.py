"""Mapping of raw OpenGrep results to findings (FR-012).

Pure: no subprocess, no filesystem, no logging. The scanner itself lives in
``veritas.security.opengrep``; the security node needs only this mapping.
"""

from __future__ import annotations

_SEVERITY_MAP = {
    "ERROR": "error",
    "WARNING": "warning",
    "INFO": "info",
}


def _normalize_severity(value: str | None) -> str:
    return _SEVERITY_MAP.get((value or "").upper(), "warning")


def result_to_finding(result: dict) -> dict:
    """Map a raw OpenGrep result to a CodeFinding-compatible dict.

    ``source`` is set to SAST (ground truth, FR-012). Paths are relative to the
    reviewed scope root.
    """
    extra = result.get("extra", {}) or {}
    start = result.get("start", {}) or {}
    end = result.get("end", {}) or {}
    metadata = extra.get("metadata", {}) or {}
    cwe = metadata.get("cwe") or []
    owasp = metadata.get("owasp") or []
    return {
        "file": result.get("path", ""),
        "start_line": start.get("line", 1),
        "start_col": start.get("col", 1),
        "end_line": end.get("line", start.get("line", 1)),
        "end_col": end.get("col", start.get("col", 1)),
        "severity": _normalize_severity(extra.get("severity")),
        "category": "security",
        "source": "sast",
        "owasp_id": owasp[0] if owasp else None,
        "cwe_id": cwe[0] if cwe else None,
        "title": (extra.get("message") or result.get("check_id", "Security finding")).strip(),
        "description": (extra.get("message") or "").strip()
        or f"OpenGrep rule `{result.get('check_id', '')}`.",
        "recommendation": "Review the flagged pattern and apply the fix suggested by the security rule.",
        "confidence": 1.0,
        "cited_snippet": extra.get("lines") or None,
    }
