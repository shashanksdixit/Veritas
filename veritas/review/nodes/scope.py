"""Scope-resolution node (T033/T047).

* ``pr`` — fetch changed files + contents via the hosting provider API (remote
  only, no local git state per FR-002); ``input_revision`` = PR/MR head sha.
* ``project``/``module`` — walk the directory tree for supported-language files.
* ``file`` — a single file.
For local scopes ``input_revision`` is always None (revision tracking is
PR-only, data-model.md / constitution Principle IV).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from veritas.config.constants import MAX_SCOPE_FILES
from veritas.hosting.github import GitHubClient
from veritas.hosting.gitlab import GitLabClient
from veritas.hosting.resolver import UnresolvableTarget, parse_pr_target
from veritas.models.entities import ReviewRun, ReviewScope
from veritas.review import ReviewFatalError, ReviewNotFoundError
from veritas.review.state import ReviewState
from veritas.security.opengrep import collect_sast
from veritas.utils.languages import is_supported

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
    return path.replace("\\", "/").lstrip("./")


def _collect_requirement_docs(root: str) -> dict[str, str]:
    """Capture requirements documentation files (FR-008) into the scope.

    Markdown/manifest requirement sources are excluded from language review but
    MUST still flow to the requirements node via the shared ``files`` channel.
    Relative keys, same normalization as walked source files.
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


def _walk_local_scope(root: str, scope: ReviewScope) -> tuple[dict[str, str], list[str]]:
    root_path = Path(root)
    if not root_path.is_dir():
        raise ReviewNotFoundError(f"target directory does not exist or is not readable: {root}")
    files: dict[str, str] = {}
    skipped: list[str] = []
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
            if len(files) >= MAX_SCOPE_FILES:
                break
            try:
                files[rel] = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if len(files) >= MAX_SCOPE_FILES:
            break
    return files, skipped


def _fetch_pr(runtime) -> dict:
    parsed = parse_pr_target(runtime.target)  # may raise UnresolvableTarget
    host = build_hosting_client(runtime.settings, parsed.provider, runtime.log)
    runtime.hosting = host
    runtime.log.info(f"Fetching PR {parsed.ref} from {parsed.provider}")

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
    for item in items:
        path = _normalize_rel(item["filename"])
        if not is_supported(path):
            skipped.append(path)
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
        if parsed.provider == "github":
            content = host.get_file_contents(parsed.owner, parsed.repo, path, sha)
        else:
            content = host.get_file_at_ref(parsed.owner, parsed.repo, path, sha)
        files[path] = content
    return {"files": files, "skipped_languages": skipped, "input_revision": sha}


def _run_local(scope: ReviewScope, target: str) -> dict:
    if scope == ReviewScope.FILE:
        content = _read_local_file(target)
        files: dict[str, str] = {_normalize_rel(target): content}
        skipped: list[str] = []
        if not is_supported(_normalize_rel(target)):
            skipped.append(_normalize_rel(target))
    else:
        files, skipped = _walk_local_scope(target, scope)
        files.update(_collect_requirement_docs(target))
    return {"files": files, "skipped_languages": skipped, "input_revision": None}


def make_scope_node(runtime) -> Callable[[ReviewState], dict]:
    def node(state: ReviewState) -> dict:
        scope: ReviewScope = state["scope"]
        target: str = state["target"]
        runtime.target = target
        runtime.log.info(f"Scanning target in scope: {target}", scope=scope.value)

        if scope == ReviewScope.PR:
            result = _fetch_pr(runtime)
        else:
            result = _run_local(scope, target)

        files: dict[str, str] = result["files"]
        sast = collect_sast(files, scope_value=scope.value, rules=runtime.opengrep_rules)
        if sast.degraded:
            runtime.log.warn(sast.degraded)
            if sast.scan_error:
                runtime.log.warn(f"OpenGrep scan detail: {sast.scan_error}")

        run = state["run"].model_copy(update={"input_revision": result["input_revision"]})
        return {
            "files": files,
            "skipped_languages": result["skipped_languages"],
            "degraded_sast": sast.degraded,
            "sast_findings": sast.findings,
            "run": run,
        }

    return node