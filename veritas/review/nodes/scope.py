"""Scope-resolution node (T033/T047/T075).

* ``pr`` — fetch changed files + contents via the hosting provider API (remote
  only, no local git state per FR-002); ``input_revision`` = PR/MR head sha.
* ``project``/``module`` — walk the directory tree for supported-language files.
* ``file`` — a single file.
For local scopes ``input_revision`` is ``sha256:<hex>``, a digest of the
reviewed files (``local_input_revision``), so a local report records which
input it reviewed (FR-024, constitution Principle I).

Exclusion (FR-029): ``[review] exclude`` patterns are applied before file
contents are fetched in every scope except ``file``, whose explicit target is
always reviewed. SAST scans exactly the post-exclusion file set.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable

from veritas.config.constants import MAX_SCOPE_FILES
from veritas.hosting.github import GitHubClient
from veritas.hosting.gitlab import GitLabClient
from veritas.hosting.resolver import UnresolvableTarget, parse_pr_target
from veritas.models.entities import ExcludedFile, ReviewRun, ReviewScope, SastStatus
from veritas.review import ReviewFatalError, ReviewNotFoundError
from veritas.review.batching import plan_batches
from veritas.review.requirements_source import (
    Requirement,
    extract_requirements,
    is_requirements_source,
    source_priority,
)
from veritas.review.state import ReviewState
from veritas.review.test_index import build_batch_test_indexes
from veritas.security.opengrep import collect_sast
from veritas.utils.languages import is_supported
from veritas.utils.paths import matching_exclusion

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pixi",
    ".idea",
    ".vscode",
    "build",
    "dist",
    "target",
    ".tox",
}
_SKIP_FILES = {
    ".gitignore",
    ".python-version",
}
_MAX_FILE_BYTES = 1_000_000


def _normalize_rel(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_excluded(path: str, patterns: list[str], excluded: dict[str, str]) -> bool:
    """Record ``path`` in ``excluded`` when a pattern matches it (FR-029).

    ``excluded`` maps relative path -> matching pattern and is keyed by path, so
    a path already recorded is never fetched or recorded twice (a supported
    requirements source is reached by both PR fetch loops).
    """
    if path in excluded:
        return True
    pattern = matching_exclusion(path, patterns)
    if pattern is None:
        return False
    excluded[path] = pattern
    return True


def _excluded_files(excluded: dict[str, str]) -> list[ExcludedFile]:
    return [ExcludedFile(path=path, pattern=pattern) for path, pattern in excluded.items()]


def _log_exclusions(runtime, excluded: dict[str, str]) -> None:
    """One info line naming each matching pattern and how many files it caught."""
    if not excluded:
        return
    counts: dict[str, int] = {}
    for pattern in excluded.values():
        counts[pattern] = counts.get(pattern, 0) + 1
    detail = ", ".join(f"{pattern} ({count})" for pattern, count in counts.items())
    runtime.log.info(f"scope: excluded {len(excluded)} file(s): {detail}")


def _requirement_source_paths(files: dict[str, str]) -> list[str]:
    """The requirement sources in ``files``, in FR-008 discovery order.

    Priority first, then path, so two specs discovered in the same rank come out
    in a stable order whatever order the scope was walked in.
    """
    return sorted(
        (path for path in files if is_requirements_source(path)),
        key=lambda path: (source_priority(path), path),
    )


def _collect_requirement_docs(
    root: str, patterns: list[str], excluded: dict[str, str]
) -> dict[str, str]:
    """Capture requirements documentation files (FR-008) into the scope.

    Markdown requirement sources are excluded from language review but MUST still
    flow to the requirements node via the shared ``files`` channel. Discovered by
    pattern in FR-008 priority order, so a spec-kit feature spec anywhere under
    the tree is found, not just the eight root/docs names. Relative keys, same
    normalization as walked source files. Exclusion patterns apply here too,
    before the file is read (FR-029).
    """
    docs: dict[str, str] = {}
    root_path = Path(root)
    if not root_path.is_dir():
        return docs
    candidates: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_file():
                candidates.append(_normalize_rel(str(full.relative_to(root_path))))
    for rel in sorted(
        (path for path in candidates if is_requirements_source(path)),
        key=lambda path: (source_priority(path), path),
    ):
        if _is_excluded(rel, patterns, excluded):
            continue
        try:
            if (root_path / rel).stat().st_size > _MAX_FILE_BYTES:
                continue
            docs[rel] = (root_path / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return docs


def _discover_requirements(log, files: dict[str, str]) -> tuple[list[str], list[Requirement]]:
    """Name the requirement sources and pull the requirements out of them (FR-008).

    One line says what was found, because "no requirements" and "no documentation"
    are different situations for a reader of the log and should not look alike.
    Extraction failures are per-source and non-fatal: one unreadable spec must not
    cost the whole review its requirements.
    """
    sources = _requirement_source_paths(files)
    requirements: list[Requirement] = []
    for path in sources:
        extracted, warnings = extract_requirements(path, files[path])
        for warning in warnings:
            log.warn(f"requirements: {warning}")
        requirements.extend(extracted)

    if not sources:
        log.info("requirements: no requirements documentation in scope")
    elif not requirements:
        log.info(f"requirements: sources found but no structured requirements; free text from {sources[0]}")
    else:
        log.info(
            f"requirements: {len(requirements)} requirement(s) from "
            f"{len(sources)} source(s): {', '.join(sources)}"
        )
    return sources, requirements


def build_hosting_client(settings, provider: str, log):
    """Construct the hosting client for the parsed PR provider."""
    if provider == "github":
        token = settings.github_token
        if not token:
            raise ReviewFatalError(
                "PR review requires a GitHub token: set VERITAS_GITHUB_TOKEN "
                "(never commit tokens; see contracts/cli.md)."
            )
        return GitHubClient(token, log=log)
    if provider == "gitlab":
        token = settings.gitlab_token
        if not token:
            raise ReviewFatalError(
                "PR review requires a GitLab token: set VERITAS_GITLAB_TOKEN "
                "(never commit tokens; see contracts/cli.md)."
            )
        return GitLabClient(token, log=log, base_url=settings.gitlab_url)
    raise ReviewFatalError(f"unsupported hosting provider: {provider}")


def local_input_revision(files: dict[str, str]) -> str:
    """The input revision of a local review (FR-024): ``sha256:`` + hex digest.

    ``files`` is the post-exclusion file set the review receives. For each file
    in sorted relative-path order the digest takes the path, a NUL byte, the
    content and a NUL byte, all UTF-8, so the result depends only on which files
    were reviewed and what they held, never on dict order. The NULs keep
    boundaries unambiguous: ("ab", "c") and ("a", "bc") hash differently.
    """
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[path].encode("utf-8"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _read_local_file(path: str) -> str:
    candidate = Path(path)
    if not candidate.is_file():
        raise ReviewNotFoundError(f"target file does not exist or is not readable: {path}")
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ReviewNotFoundError(f"cannot read target file {path}: {exc}") from exc


def _walk_local_scope(
    root: str, scope: ReviewScope, patterns: list[str]
) -> tuple[dict[str, str], list[str], list[ExcludedFile]]:
    root_path = Path(root)
    if not root_path.is_dir():
        raise ReviewNotFoundError(f"target directory does not exist or is not readable: {root}")
    files: dict[str, str] = {}
    skipped: list[str] = []
    excluded: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name in _SKIP_FILES:
                continue
            full = Path(dirpath) / name
            if not full.is_file():
                continue
            try:
                if full.stat().st_size > _MAX_FILE_BYTES:
                    skipped.append(_normalize_rel(str(full)))
                    continue
            except OSError:
                continue
            rel = _normalize_rel(str(full.relative_to(root_path)))
            if not is_supported(rel):
                skipped.append(rel)
                continue
            # FR-029: excluded before the file is read, so it never occupies a
            # slot against MAX_SCOPE_FILES.
            if _is_excluded(rel, patterns, excluded):
                continue
            if len(files) >= MAX_SCOPE_FILES:
                break
            try:
                files[rel] = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if len(files) >= MAX_SCOPE_FILES:
            break
    return files, skipped, _excluded_files(excluded)


def _fetch_pr(runtime) -> dict:
    parsed = parse_pr_target(runtime.target)  # may raise UnresolvableTarget
    host = build_hosting_client(runtime.settings, parsed.provider, runtime.log)
    runtime.hosting = host
    runtime.log.info(f"Fetching PR {parsed.ref} from {parsed.provider}")
    patterns = runtime.settings.exclude

    if parsed.provider == "github":
        sha = host.head_sha(parsed.owner, parsed.repo, parsed.number)
        items = host.list_pr_files(parsed.owner, parsed.repo, parsed.number)
    else:
        sha = host.head_sha(parsed.owner, parsed.repo, parsed.number)
        changes = host.list_mr_changes(parsed.owner, parsed.repo, parsed.number)
        items = []
        for change in changes:
            if not change.get("deleted_file"):
                items.append({"filename": change["new_path"]})

    files: dict[str, str] = {}
    skipped: list[str] = []
    excluded: dict[str, str] = {}
    for item in items:
        path = _normalize_rel(item["filename"])
        if not is_supported(path):
            skipped.append(path)
            continue
        # FR-029: excluded before the remote fetch, never requested.
        if _is_excluded(path, patterns, excluded):
            continue
        if parsed.provider == "github":
            content = host.get_file_contents(parsed.owner, parsed.repo, path, sha)
        else:
            content = host.get_file_at_ref(parsed.owner, parsed.repo, path, sha)
        files[path] = content

    # FR-008: any file the PR touches that is a requirement source, found by
    # pattern rather than by an exact-name list, so a feature spec added under
    # specs/ is fetched when the PR changes it. Only sources in the PR are
    # available; this loop never looks outside the diff.
    for item in items:
        path = _normalize_rel(item["filename"])
        if not is_requirements_source(path) or path in files:
            continue
        if _is_excluded(path, patterns, excluded):
            continue
        if parsed.provider == "github":
            content = host.get_file_contents(parsed.owner, parsed.repo, path, sha)
        else:
            content = host.get_file_at_ref(parsed.owner, parsed.repo, path, sha)
        files[path] = content
    return {
        "files": files,
        "skipped_languages": skipped,
        "excluded_files": _excluded_files(excluded),
        "input_revision": sha,
    }


def _run_local(scope: ReviewScope, target: str, patterns: list[str]) -> dict:
    if scope == ReviewScope.FILE:
        # An explicit --scope file target is always reviewed, even when it
        # matches an exclusion pattern (FR-029).
        content = _read_local_file(target)
        files: dict[str, str] = {_normalize_rel(target): content}
        skipped: list[str] = []
        if not is_supported(_normalize_rel(target)):
            skipped.append(_normalize_rel(target))
        excluded: list[ExcludedFile] = []
    else:
        files, skipped, excluded = _walk_local_scope(target, scope, patterns)
        excluded_by_path = {entry.path: entry.pattern for entry in excluded}
        files.update(_collect_requirement_docs(target, patterns, excluded_by_path))
        excluded = _excluded_files(excluded_by_path)
    return {
        "files": files,
        "skipped_languages": skipped,
        "excluded_files": excluded,
        "input_revision": local_input_revision(files),
    }


def make_scope_node(runtime) -> Callable[[ReviewState], dict]:
    def node(state: ReviewState) -> dict:
        scope: ReviewScope = state["scope"]
        target: str = state["target"]
        runtime.target = target
        runtime.log.info(f"Scanning target in scope: {target}", scope=scope.value)

        if scope == ReviewScope.PR:
            result = _fetch_pr(runtime)
        else:
            result = _run_local(scope, target, runtime.settings.exclude)

        _log_exclusions(
            runtime, {entry.path: entry.pattern for entry in result["excluded_files"]}
        )

        # FR-029: batch the post-exclusion, supported-language source files only.
        # Requirement documentation is not batched; it reaches the requirements
        # node whole.
        source_files = {path: text for path, text in result["files"].items() if is_supported(path)}
        plan, warnings = plan_batches(
            source_files,
            batch_chars=runtime.settings.batch_chars,
            max_batches=runtime.settings.max_batches,
        )
        for warning in warnings:
            runtime.log.warn(warning)
        runtime.log.info(
            f"batching: {len(plan.reviewed_files)} file(s) in {len(plan.batches)} batch(es) "
            f"of up to {runtime.settings.batch_chars} chars; {len(plan.split_files)} split, "
            f"{len(plan.not_reviewed_files)} not reviewed"
        )

        # FR-004: the same post-exclusion source files and the same plan, so each
        # batch carries an index of the tests ranked for the code it holds. Excluded
        # files are absent from the index by construction, which is the point.
        test_indexes, index_stats = build_batch_test_indexes(source_files, plan)
        runtime.log.info(
            f"test index: {index_stats['test_files']} test file(s), "
            f"{index_stats['test_names']} test name(s); "
            f"up to {index_stats['max_chars']} chars per batch; "
            f"{index_stats['truncated_batches']} of {index_stats['batches']} "
            "batch(es) truncated"
        )

        files: dict[str, str] = result["files"]
        requirement_sources, requirements = _discover_requirements(runtime.log, files)
        # SAST scans exactly the post-exclusion file set (FR-029).
        sast = collect_sast(files, scope_value=scope.value, rules=runtime.opengrep_rules)
        if sast.degraded:
            runtime.log.warn(sast.degraded)
            # The detail is the same single line as the reason when the reason
            # came from stderr; log it only when it adds something.
            if sast.scan_error and sast.scan_error != sast.degraded:
                runtime.log.warn(f"OpenGrep scan detail: {sast.scan_error}")

        run = state["run"].model_copy(
            update={
                "input_revision": result["input_revision"],
                # The rules source SAST ran with, recorded in the report
                # (schema 1.7.0); None only when the run never reached SAST.
                "sast_rules": sast.rules,
                # Whether it ran, its result count and the reason it did not
                # run or ran degraded (schema 1.9.0, FR-012).
                "sast_status": SastStatus.RAN if sast.ran else SastStatus.NOT_RUN,
                "sast_result_count": len(sast.findings) if sast.ran else None,
                "sast_reason": sast.degraded,
            }
        )
        return {
            "files": files,
            "skipped_languages": result["skipped_languages"],
            "excluded_files": result["excluded_files"],
            "batch_plan": plan,
            "test_indexes": test_indexes,
            "requirement_sources": requirement_sources,
            "requirements": requirements,
            "degraded_sast": sast.degraded,
            "sast_findings": sast.findings,
            "run": run,
        }

    return node