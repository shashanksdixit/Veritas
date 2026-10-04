"""Scope-resolution node (T033/T047/T075).

* ``pr`` — fetch changed files + contents via the hosting provider API (remote
  only, no local git state per FR-002); ``input_revision`` = PR/MR head sha.
* ``project``/``module`` — walk the directory tree for supported-language files.
* ``file`` — a single file.
For local scopes ``input_revision`` is always None (revision tracking is
PR-only, data-model.md / constitution Principle IV).

Exclusion (FR-029): ``[review] exclude`` patterns are applied before file
contents are fetched in every scope except ``file``, whose explicit target is
always reviewed. SAST scans exactly the post-exclusion file set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from veritas.config.constants import MAX_SCOPE_FILES
from veritas.hosting.github import GitHubClient
from veritas.hosting.gitlab import GitLabClient
from veritas.hosting.resolver import UnresolvableTarget, parse_pr_target
from veritas.models.entities import ExcludedFile, ReviewRun, ReviewScope
from veritas.review import ReviewFatalError, ReviewNotFoundError
from veritas.review.batching import plan_batches
from veritas.review.state import ReviewState
from veritas.review.test_index import build_test_index
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


def _collect_requirement_docs(
    root: str, patterns: list[str], excluded: dict[str, str]
) -> dict[str, str]:
    """Capture requirements documentation files (FR-008) into the scope.

    Markdown/manifest requirement sources are excluded from language review but
    MUST still flow to the requirements node via the shared ``files`` channel.
    Relative keys, same normalization as walked source files. Exclusion
    patterns apply here too, before the file is read (FR-029).
    """
    from veritas.config.constants import REQUIREMENTS_SOURCES

    docs: dict[str, str] = {}
    root_path = Path(root)
    if not root_path.is_dir():
        return docs
    for name in REQUIREMENTS_SOURCES:
        candidate = root_path / name
        if not candidate.is_file():
            continue
        if _is_excluded(name, patterns, excluded):
            continue
        try:
            if candidate.stat().st_size > _MAX_FILE_BYTES:
                continue
            docs[name] = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return docs


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

    from veritas.config.constants import REQUIREMENTS_SOURCES

    for item in items:
        path = _normalize_rel(item["filename"])
        if path not in REQUIREMENTS_SOURCES or path in files:
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
        "input_revision": None,
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

        # FR-004: the same post-exclusion source files, so the test-coverage
        # review knows which tests exist even when the tests for a given batch
        # are in another one. Excluded files are absent from the index by
        # construction, which is the point.
        test_index, index_stats = build_test_index(source_files)
        omitted = ""
        if index_stats["truncated"]:
            omitted = f", truncated ({index_stats['omitted_files']} file(s) omitted)"
        runtime.log.info(
            f"test index: {index_stats['test_files']} test file(s), "
            f"{index_stats['test_names']} test name(s), "
            f"{index_stats['chars']} chars{omitted}"
        )

        files: dict[str, str] = result["files"]
        # SAST scans exactly the post-exclusion file set (FR-029).
        sast = collect_sast(files, scope_value=scope.value, rules=runtime.opengrep_rules)
        if sast.degraded:
            runtime.log.warn(sast.degraded)
            if sast.scan_error:
                runtime.log.warn(f"OpenGrep scan detail: {sast.scan_error}")

        run = state["run"].model_copy(update={"input_revision": result["input_revision"]})
        return {
            "files": files,
            "skipped_languages": result["skipped_languages"],
            "excluded_files": result["excluded_files"],
            "batch_plan": plan,
            "test_index": test_index,
            "degraded_sast": sast.degraded,
            "sast_findings": sast.findings,
            "run": run,
        }

    return node