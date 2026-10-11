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
from pathlib import Path

from veritas.config.constants import SUPPORTED_LANGUAGES
from veritas.utils.logging import get_log
from veritas.utils.redaction import redact_secrets

OPENGREP_NOT_FOUND = "OpenGrep not found on PATH"
OPENGREP_SCAN_FAILED = "OpenGrep scan reported an error"
OPENGREP_NO_JSON = "OpenGrep scan produced no parseable JSON"
SAST_NO_FILES = "no files to scan"

# A failed scan is reported as ONE line of at most this many characters: the
# degraded reason and the logged detail are the last non-empty line of the
# scanner's stderr, redacted - never a multi-line traceback (FR-012).
MAX_FAILURE_CHARS = 200

_SEVERITY_MAP = {
    "ERROR": "error",
    "WARNING": "warning",
    "INFO": "info",
}


def _normalize_severity(value: str | None) -> str:
    return _SEVERITY_MAP.get((value or "").upper(), "warning")


def _failure_line(*candidates: str | None) -> str:
    """The last non-empty line of the first candidate that has one.

    A Python traceback in the scanner's stderr therefore reports as its final
    exception line. The line is passed through ``redact_secrets`` and cut to
    ``MAX_FAILURE_CHARS``, so the degraded reason and the logged detail are
    each one line no longer than that.
    """
    for candidate in candidates:
        lines = [line.strip() for line in (candidate or "").splitlines() if line.strip()]
        if lines:
            return redact_secrets(lines[-1])[:MAX_FAILURE_CHARS]
    return ""


@dataclass
class OpengrepResult:
    """Raw OpenGrep scan output plus degradation info.

    ``ran`` is True only when OpenGrep finished and its JSON was parsed; then
    ``findings`` is the complete result set and ``degraded``, if set, says why
    coverage is still partial (unmapped results). When ``ran`` is False,
    ``degraded`` is the one-line reason the scan did not run (FR-012).
    """

    findings: list[dict] = field(default_factory=list)
    ran: bool = False
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
        return OpengrepResult(degraded=OPENGREP_NOT_FOUND, rules=resolved_rules)

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
        # UTF-8 with replacement, not the locale codec: OpenGrep's scan summary
        # carries box-drawing characters, and on Windows a byte outside the
        # active code page would raise inside the reader thread.
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = "OpenGrep scan timed out"
        else:
            reason = OPENGREP_NOT_FOUND
        return OpengrepResult(degraded=reason, rules=resolved_rules)

    if proc.returncode not in (0, 1, 2):
        # opengrep exits 0 on clean scan; certain rule/scan errors yield non-zero
        detail = _failure_line(proc.stderr, proc.stdout)
        return OpengrepResult(
            degraded=detail or OPENGREP_SCAN_FAILED,
            scan_error=detail or None,
            rules=resolved_rules,
        )

    try:
        with open(out_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # stderr only: on this path stdout may be the JSON payload itself, and
        # the last line of JSON is punctuation, not a reason. With no stderr
        # line the category message above is the reason.
        detail = _failure_line(proc.stderr)
        return OpengrepResult(
            degraded=detail or OPENGREP_NO_JSON,
            scan_error=detail or None,
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
    return OpengrepResult(findings=findings, ran=True, rules=resolved_rules)


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


def _map_result_path(result_path: str, tmpdir: str, files: dict[str, str]) -> str | None:
    """Map an OpenGrep result path back to a scoped file key.

    First try an exact resolution: the result path resolved against the scan's
    temp directory, as a forward-slash key. On Windows OpenGrep reports the
    path it was given in the spelling the filesystem handed back - backslashes,
    sometimes an 8.3 short name (``SHASHA~1``) for the long user directory the
    temp directory was created under - so the resolved key can miss. Then fall
    back to suffix matching on the backslash-normalised path: the scoped key the
    path equals or ends with, longest key winning when keys share a suffix.

    Returns None when the result belongs to no reviewed file.
    """
    try:
        key = Path(result_path).resolve().relative_to(Path(tmpdir).resolve()).as_posix()
    except (OSError, ValueError):
        key = None
    if key is not None and key in files:
        return key
    normalized = result_path.replace("\\", "/")
    best: str | None = None
    for candidate in files:
        if normalized == candidate or normalized.endswith("/" + candidate):
            if best is None or len(candidate) > len(best):
                best = candidate
    return best


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
    analysis). Each result path is mapped back to its scoped file key; results
    that map to no reviewed file are counted and reported through the degraded
    reason rather than silently dropped.
    """
    if not files:
        return OpengrepResult(degraded=SAST_NO_FILES, rules=rules or OpengrepResult.rules)
    if shutil.which(opengrep_bin) is None:
        return OpengrepResult(degraded=OPENGREP_NOT_FOUND, rules=rules or OpengrepResult.rules)

    import os
    import tempfile

    with tempfile.TemporaryDirectory(prefix="veritas-sast-") as tmpdir:
        for rel, content in files.items():
            dest = os.path.join(tmpdir, rel)
            os.makedirs(os.path.dirname(dest) or tmpdir, exist_ok=True)
            with open(dest, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(content)
        result = run_opengrep(tmpdir, rules=rules, opengrep_bin=opengrep_bin)
        findings: list[dict] = []
        unmapped = 0
        for finding in result.findings:
            key = _map_result_path(finding.get("path", ""), tmpdir, files)
            if key is None:
                unmapped += 1
                continue
            findings.append({**finding, "path": key})
        result.findings = findings
        if unmapped:
            get_log().warn(
                f"sast: {unmapped} result(s) could not be mapped to reviewed files"
            )
            note = f"{unmapped} result(s) unmapped"
            result.degraded = f"{result.degraded}; {note}" if result.degraded else note
        return result