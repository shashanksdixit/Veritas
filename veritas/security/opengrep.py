"""OpenGrep integration (T032, FR-003/FR-012).

OpenGrep exposes no Python API; it is invoked via subprocess (LGPL-2.1 engine,
CLI-only). Output is the semgrep-compatible ``--json`` cli_output schema.
Missing binary degrades security coverage with a specific reason rather than
failing the run (FR-012).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field

from veritas.config.constants import SUPPORTED_LANGUAGES

OPENGPRE_NOT_FOUND = "OpenGrep not found on PATH"

_SEVERITY_MAP = {
    "ERROR": "error",
    "WARNING": "warning",
    "INFO": "info",
}


def _normalize_severity(value: str | None) -> str:
    return _SEVERITY_MAP.get((value or "").upper(), "warning")


@dataclass
class OpengrepResult:
    """Raw OpenGrep scan output plus degradation info."""

    findings: list[dict] = field(default_factory=list)
    degraded: str | None = None
    scan_error: str | None = None
    rules: str = "p/owasp-top-ten"


def run_opengrep(
    target_dir: str,
    rules: str | None = None,
    *,
    opengrep_bin: str = "opengrep",
    timeout: int = 120,
) -> OpengrepResult:
    """Run ``opengrep scan --config <rules> --json -o <out> <target>``.

    Returns raw JSON results (research.md §1 schema). If the binary is absent
    returns ``degraded="OpenGrep not found on PATH"`` (FR-012) instead of
    raising. Results for files outside supported languages are filtered here.
    """
    from veritas.utils.languages import language_of

    resolved_rules = rules or OpengrepResult.rules
    if shutil.which(opengrep_bin) is None:
        return OpengrepResult(degraded=OPENGPRE_NOT_FOUND, rules=resolved_rules)

    out_json = f"{target_dir}/.opengrep-results-{id(object()):x}.json"
    cmd = [
        opengrep_bin,
        "scan",
        "--config",
        resolved_rules,
        "--json",
        "-o",
        out_json,
        target_dir,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = "OpenGrep scan timed out"
        else:
            reason = OPENGPRE_NOT_FOUND
        return OpengrepResult(degraded=reason, rules=resolved_rules)

    if proc.returncode not in (0, 1, 2):
        # opengrep exits 0 on clean scan; certain rule/scan errors yield non-zero
        return OpengrepResult(
            degraded="OpenGrep scan reported an error",
            scan_error=(proc.stderr or proc.stdout or "").strip()[:500],
            rules=resolved_rules,
        )

    try:
        with open(out_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return OpengrepResult(
            degraded="OpenGrep scan produced no parseable JSON",
            scan_error=(proc.stdout or proc.stderr or "").strip()[:500],
            rules=resolved_rules,
        )
    finally:
        try:
            import os

            os.remove(out_json)
        except OSError:
            pass

    findings: list[dict] = []
    for result in data.get("results", []):
        path = result.get("path", "")
        if language_of(path) not in SUPPORTED_LANGUAGES:
            continue
        findings.append(result)
    return OpengrepResult(findings=findings, rules=resolved_rules)


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


def collect_sast(
    files: dict[str, str],
    *,
    scope_value: str,
    rules: str | None = None,
    opengrep_bin: str = "opengrep",
) -> OpengrepResult:
    """Run OpenGrep over exactly the scoped files.

    Scoped contents are materialized into a temp directory (relative paths
    preserved) so path matching is exact regardless of scope type; the temp
    directory is created and cleaned up here (scope-preparation, not review
    analysis). Results whose path is not in the scoped file set are filtered.
    """
    if not files:
        return OpengrepResult(degraded=None, rules=rules or OpengrepResult.rules)
    if shutil.which(opengrep_bin) is None:
        return OpengrepResult(degraded=OPENGPRE_NOT_FOUND, rules=rules or OpengrepResult.rules)

    import os
    import tempfile

    with tempfile.TemporaryDirectory(prefix="veritas-sast-") as tmpdir:
        for rel, content in files.items():
            dest = os.path.join(tmpdir, rel)
            os.makedirs(os.path.dirname(dest) or tmpdir, exist_ok=True)
            with open(dest, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(content)
        result = run_opengrep(tmpdir, rules=rules, opengrep_bin=opengrep_bin)
        result.findings = [f for f in result.findings if f.get("path") in files]
        return result