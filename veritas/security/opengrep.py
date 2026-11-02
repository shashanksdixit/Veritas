"""OpenGrep integration (T032, FR-003/FR-012).

OpenGrep exposes no Python API; it is invoked via subprocess (LGPL-2.1 engine,
CLI-only). Output is the semgrep-compatible ``--json`` cli_output schema.
Missing binary degrades security coverage with a specific reason rather than
failing the run (FR-012).

Rules licence (constitution Principle II): registry rulesets such as the
default ``p/owasp-top-ten`` are not covered by OpenGrep's LGPL-2.1 licence;
they carry the Semgrep Rules License, and using one is the user's licensing
choice. A local rules file or directory is the alternative.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from veritas.config.constants import DEFAULT_OPENGREP_JOBS, SUPPORTED_LANGUAGES
from veritas.utils.logging import get_log
from veritas.utils.redaction import redact_secrets

OPENGREP_NOT_FOUND = "OpenGrep not found on PATH"
OPENGREP_SCAN_FAILED = "OpenGrep scan reported an error"
OPENGREP_NO_JSON = "OpenGrep scan produced no parseable JSON"
SAST_NO_FILES = "no files to scan"

# The scan's time limit in seconds. Not configurable (T122 caps the job count
# instead); the not-run reason on a timeout states it.
OPENGREP_TIMEOUT_SECONDS = 120

# The clock the scan is timed with; tests replace it to avoid real timing.
_clock = time.monotonic

# A failed scan is reported as ONE line of at most this many characters: the
# degraded reason and the logged detail are the last non-empty line of the
# scanner's stderr, redacted - never a multi-line traceback (FR-012).
MAX_FAILURE_CHARS = 200

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

    ``elapsed_seconds`` is how long the OpenGrep process ran, set whenever it
    exited (not on a timeout or a missing binary); ``jobs`` is the ``-j``
    value it ran with.

    ``skipped`` is OpenGrep's own ``paths.skipped`` list (``{"path",
    "reason"}`` entries, each a file or a directory) from a finished scan;
    empty when it reported none (T123).
    """

    findings: list[dict] = field(default_factory=list)
    ran: bool = False
    degraded: str | None = None
    scan_error: str | None = None
    rules: str = "p/owasp-top-ten"
    elapsed_seconds: float | None = None
    jobs: int | None = None
    skipped: list[dict] = field(default_factory=list)


def run_opengrep(
    target_dir: str,
    rules: str | None = None,
    *,
    opengrep_bin: str = "opengrep",
    timeout: int = OPENGREP_TIMEOUT_SECONDS,
    jobs: int = DEFAULT_OPENGREP_JOBS,
) -> OpengrepResult:
    """Run ``opengrep scan --config <rules> -j <jobs> --verbose --json -o <out> <target>``.

    Returns raw JSON results (research.md §1 schema). If the binary is absent
    returns ``degraded="OpenGrep not found on PATH"`` (FR-012) instead of
    raising. Results for files outside supported languages are filtered here.

    ``rules`` defaults to the registry ruleset ``p/owasp-top-ten``, which
    carries the Semgrep Rules License, not OpenGrep's LGPL-2.1; using it is the
    caller's licensing choice, and a local rules file or directory is the
    alternative.
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
        # Without -j OpenGrep runs one job per detected core, which made scan
        # times swing past the time limit (T122).
        "-j",
        str(jobs),
        # Only with --verbose does the JSON list the paths OpenGrep skipped
        # (paths.skipped), which the report states (T123).
        "--verbose",
        "--json",
        "-o",
        out_json,
        target_dir,
    ]
    started = _clock()
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
            reason = f"OpenGrep scan timed out after {timeout} s"
        else:
            reason = OPENGREP_NOT_FOUND
        return OpengrepResult(degraded=reason, rules=resolved_rules, jobs=jobs)
    timing = {"elapsed_seconds": _clock() - started, "jobs": jobs}

    if proc.returncode not in (0, 1, 2):
        # opengrep exits 0 on clean scan; certain rule/scan errors yield non-zero
        detail = _failure_line(proc.stderr, proc.stdout)
        return OpengrepResult(
            degraded=detail or OPENGREP_SCAN_FAILED,
            scan_error=detail or None,
            rules=resolved_rules,
            **timing,
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
            **timing,
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
    skipped = (data.get("paths") or {}).get("skipped") or []
    return OpengrepResult(
        findings=findings, ran=True, rules=resolved_rules, skipped=skipped, **timing
    )


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


def _map_skipped_dir(entry_path: str, tmpdir: str, files: dict[str, str]) -> str | None:
    """Map a skipped directory entry to its path relative to the temp directory.

    The same two steps as ``_map_result_path``: an exact resolution against the
    temp directory first, then a suffix match on the backslash-normalised path,
    the longest suffix that is a directory holding a scoped key winning.
    Returns ``""`` for the temp directory itself and None when no scoped file
    lies under the entry.
    """
    def holds_keys(prefix: str) -> bool:
        return any(prefix == "" or key.startswith(prefix + "/") for key in files)

    try:
        rel = (Path(tmpdir) / entry_path).resolve().relative_to(Path(tmpdir).resolve())
        prefix = "" if rel == Path(".") else rel.as_posix()
    except (OSError, ValueError):
        prefix = None
    if prefix is not None and holds_keys(prefix):
        return prefix
    parts = [part for part in entry_path.replace("\\", "/").split("/") if part]
    for start in range(len(parts)):
        candidate = "/".join(parts[start:])
        if holds_keys(candidate):
            return candidate
    return None


# The degraded-reason clause for each skip reason OpenGrep reports (T123); any
# other reason is stated as OpenGrep spells it.
SKIP_IGNORE_LIST = "semgrepignore_patterns_match"
SKIP_SIZE_LIMIT = "exceeded_size_limit"
MAX_SKIPPED_NAMES = 3


def _skipped_clauses(skipped: list[dict], tmpdir: str, files: dict[str, str]) -> list[str]:
    """One "<n> file(s) not scanned (...)" clause per skip reason.

    Clauses come in a fixed order whatever order OpenGrep lists its entries
    in: the ignore list, the size limit, then any other reason alphabetically.

    Each entry is mapped to the scoped files it covers: a file entry through
    ``_map_result_path``, otherwise every scoped key under it as a directory.
    Each scoped file is counted once; an entry covering no scoped file counts
    nothing and is not named.
    """
    counted: set[str] = set()
    by_reason: dict[str, tuple[set[str], set[str]]] = {}
    for entry in skipped:
        path = str(entry.get("path", ""))
        reason = str(entry.get("reason", "")) or "unknown reason"
        key = _map_result_path(path, tmpdir, files)
        if key is not None:
            covered, name = {key}, key
        else:
            prefix = _map_skipped_dir(path, tmpdir, files)
            if prefix is None:
                continue
            covered = {k for k in files if prefix == "" or k.startswith(prefix + "/")}
            name = f"{prefix}/" if prefix else "./"
        new = covered - counted
        if not new:
            continue
        counted |= new
        keys, names = by_reason.setdefault(reason, (set(), set()))
        keys |= new
        names.add(name)

    order = {SKIP_IGNORE_LIST: 0, SKIP_SIZE_LIMIT: 1}
    clauses = []
    for reason in sorted(by_reason, key=lambda r: (order.get(r, 2), r)):
        keys, names = by_reason[reason]
        if reason == SKIP_IGNORE_LIST:
            shown = sorted(names)
            listed = ", ".join(shown[:MAX_SKIPPED_NAMES])
            if len(shown) > MAX_SKIPPED_NAMES:
                listed += f", +{len(shown) - MAX_SKIPPED_NAMES} more"
            why = f"OpenGrep's default ignore list: {listed}"
        elif reason == SKIP_SIZE_LIMIT:
            why = "over OpenGrep's 1 MB size limit"
        else:
            why = reason
        clauses.append(f"{len(keys)} file(s) not scanned ({why})")
    return clauses


def collect_sast(
    files: dict[str, str],
    *,
    scope_value: str,
    rules: str | None = None,
    opengrep_bin: str = "opengrep",
    jobs: int = DEFAULT_OPENGREP_JOBS,
) -> OpengrepResult:
    """Run OpenGrep over exactly the scoped files, with ``jobs`` parallel jobs.

    Scoped contents are materialized into a temp directory (relative paths
    preserved) so path matching is exact regardless of scope type; the temp
    directory is created and cleaned up here (scope-preparation, not review
    analysis). Each result path is mapped back to its scoped file key; results
    that map to no reviewed file are counted and reported through the degraded
    reason rather than silently dropped. Scoped files OpenGrep reports as
    skipped are counted per reason and stated there too, after any unmapped
    note; the scan still counts as ran (T123).
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
        result = run_opengrep(tmpdir, rules=rules, opengrep_bin=opengrep_bin, jobs=jobs)
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
        clauses = _skipped_clauses(result.skipped, tmpdir, files)
        if clauses:
            result.degraded = "; ".join(([result.degraded] if result.degraded else []) + clauses)
        return result