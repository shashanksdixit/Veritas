"""Scope-resolution node (T033/T047/T075).

* ``pr`` — fetch changed files + contents via the hosting provider API (remote
  only, no local git state per FR-002); ``input_revision`` = PR/MR head sha.
  Project context comes from the repository root through the same API:
  manifests at the head commit, conventions and rules at the base commit (T120).
* ``project``/``module`` — walk the directory tree for supported-language files.
* ``file`` — a single file.
For local scopes ``input_revision`` is ``sha256:<hex>``, a digest of the
reviewed files (``local_input_revision``), so a local report records which
input it reviewed (FR-024, constitution Principle I).

Exclusion (FR-029): ``[review] exclude`` patterns are applied before file
contents are fetched in every scope except ``file``, whose explicit target is
always reviewed. SAST scans exactly the post-exclusion file set. A PR file
whose content the hosting API withholds is excluded with a reason (T124).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Sequence

from veritas.config.constants import (
    CONFIG_PATH,
    CONVENTIONS_FILENAMES,
    MAX_SCOPE_FILES,
    NATURAL_LANGUAGE_RULES_GLOB,
)
from veritas.models.entities import ExcludedFile, ReviewScope, SastStatus
from veritas.review import ReviewFatalError, ReviewNotFoundError
from veritas.review.batching import plan_batches
from veritas.review.contexts import ScopeContext
from veritas.review.requirements_source import (
    Requirement,
    extract_requirements,
    is_requirements_source,
    source_priority,
)
from veritas.review.state import ProjectSources, ReviewState
from veritas.review.test_index import build_batch_test_indexes
from veritas.security.opengrep import collect_sast
from veritas.utils.languages import is_supported
from veritas.utils.paths import matching_exclusion
from veritas.utils.project_context import (
    CSPROJ_SUFFIX,
    MANIFEST_NAMES,
    MAX_SOURCE_BYTES,
    is_csproj,
)

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


def _is_excluded(path: str, patterns: Sequence[str], excluded: dict[str, str]) -> bool:
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


def _excluded_files(
    excluded: dict[str, str], withheld: dict[str, str] | None = None
) -> list[ExcludedFile]:
    """Pattern exclusions first, then files whose content the hosting API
    withheld, each with its reason (T124)."""
    entries = [ExcludedFile(path=path, pattern=pattern) for path, pattern in excluded.items()]
    entries += [ExcludedFile(path=path, reason=reason) for path, reason in (withheld or {}).items()]
    return entries


def _log_exclusions(log, excluded: dict[str, str]) -> None:
    """One info line naming each matching pattern and how many files it caught.

    ``excluded`` maps path -> pattern for pattern exclusions only; withheld
    files have their own warning, logged when the fetch is refused (T124).
    """
    if not excluded:
        return
    counts: dict[str, int] = {}
    for pattern in excluded.values():
        counts[pattern] = counts.get(pattern, 0) + 1
    detail = ", ".join(f"{pattern} ({count})" for pattern, count in counts.items())
    log.info(f"scope: excluded {len(excluded)} file(s): {detail}")


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
    root: str, patterns: Sequence[str], excluded: dict[str, str]
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


# How many levels above the start directory the project-root search looks
# (T119). Reaching the cap without a marker counts as finding no root.
MAX_WALK_UP = 32


def _project_context_start(scope: ReviewScope, target: str) -> str:
    """The directory the project-context search starts from: the file's
    directory for ``--scope file``, else the target itself (T119 walks up
    from here to the project root)."""
    if scope == ReviewScope.FILE:
        return os.path.dirname(os.path.abspath(target)) or os.getcwd()
    return os.path.abspath(target)


def _find_project_root(start: str) -> str | None:
    """The project root above ``start`` (T119, FR-009), or None when there is none.

    The first directory from ``start`` upward, ``start`` included, holding
    ``.git`` (a directory, or a file for a git worktree or submodule, so a
    submodule is its own project) or a ``.veritas/config.toml`` file. A bare
    ``.veritas/`` directory is not a marker: Veritas creates one wherever it
    runs. None when the filesystem root, or ``MAX_WALK_UP`` levels above
    ``start``, is reached without a marker. Parents are found by string
    arithmetic on ``os.path.abspath`` (links are not resolved, so subst and
    mapped drives keep their names and a link loop cannot happen).
    """
    current = os.path.abspath(start)
    for _level in range(MAX_WALK_UP + 1):
        if os.path.lexists(os.path.join(current, ".git")) or os.path.isfile(
            os.path.join(current, CONFIG_PATH)
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def _read_source(
    path: Path, name: str, *, strict_utf8: bool = False
) -> tuple[str | None, str | None]:
    """Read one project-context file: ``(text, None)``, or ``(None, problem)`` (T118).

    Every file over ``MAX_SOURCE_BYTES``, pyproject.toml included, is skipped
    unread. pyproject.toml (``strict_utf8``) must be valid UTF-8, read as bytes
    and decoded as ``utf-8-sig`` so a leading byte-order mark (which
    ``tomllib`` rejects) is dropped; other files are text with invalid bytes
    replaced.
    The problem is one short line naming the file, for the report and the log.
    """
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return None, f"{name} over {MAX_SOURCE_BYTES} bytes, skipped"
        if not strict_utf8:
            return path.read_text(encoding="utf-8", errors="replace"), None
        raw = path.read_bytes()
    except OSError:
        return None, f"{name} could not be read, skipped"
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, f"{name} is not valid UTF-8, skipped"


def _read_project_sources(start: str) -> ProjectSources:
    """Read the project-context files from ``start`` up to the project root
    (T116/T118/T119, FR-009/FR-010/FR-011).

    The root is ``_find_project_root(start)``; without one, only ``start`` is
    read. Nothing above the root is read. Each file is named by its path
    relative to the root with ``/`` separators, and the root is the origin.

    - Manifests: the nearest directory holding any manifest (one in
      ``MANIFEST_NAMES`` or a ``*.csproj``) supplies all of them, named ones
      first and then every ``*.csproj`` sorted; levels are never mixed. A
      manifest that exists but cannot be used still decides the directory,
      and its problem is stated.
    - Conventions: the nearest file whose text is not blank, trying
      ``CONVENTIONS_FILENAMES`` in order within each directory before going up.
    - ``.veritas/rules.md``: the nearest whose text is not blank.

    A blank conventions or rules file is passed over and not kept. A missing
    file is left out silently. A file that is too large, unreadable or
    (pyproject.toml) not valid UTF-8 is left out with a problem in
    ``ProjectSources.problems``, in read order.
    """
    start = os.path.abspath(start)
    root = _find_project_root(start) or start
    levels = [start]
    while levels[-1] != root:
        levels.append(os.path.dirname(levels[-1]))
    root_path = Path(root)
    directories = [Path(level) for level in levels]
    files: list[tuple[str, str]] = []
    problems: list[str] = []

    def read(path: Path, *, strict_utf8: bool = False) -> tuple[str, str | None]:
        name = path.relative_to(root_path).as_posix()
        text, problem = _read_source(path, name, strict_utf8=strict_utf8)
        if problem is not None:
            problems.append(problem)
        return name, text

    for directory in directories:
        manifests = [directory / name for name in MANIFEST_NAMES if (directory / name).is_file()]
        manifests += [path for path in sorted(directory.glob(f"*{CSPROJ_SUFFIX}")) if path.is_file()]
        if not manifests:
            continue
        for path in manifests:
            name, text = read(path, strict_utf8=path.name == "pyproject.toml")
            if text is not None:
                files.append((name, text))
        break

    def nearest_non_blank(keys: Sequence[str]) -> None:
        for directory in directories:
            for key in keys:
                path = directory / key
                if not path.is_file():
                    continue
                name, text = read(path)
                if text is not None and text.strip():
                    files.append((name, text))
                    return

    nearest_non_blank(CONVENTIONS_FILENAMES)
    nearest_non_blank((NATURAL_LANGUAGE_RULES_GLOB,))
    return ProjectSources(origin=root, files=tuple(files), problems=tuple(problems))


def _walk_local_scope(
    root: str, scope: ReviewScope, patterns: Sequence[str]
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


def _fetch_pr(ctx: ScopeContext) -> dict:
    # run_review parsed the target and built the read-only fetcher before the
    # graph existed (T111), so a PR scope always arrives with both.
    parsed = ctx.pr
    host = ctx.fetcher
    ctx.log.info(f"Fetching PR {parsed.ref} from {parsed.provider}")
    patterns = ctx.exclude

    # One request gives both commits: the head for the files and manifests,
    # the base for conventions and rules (T120). head_sha made this same
    # request and kept only the head.
    refs = host.pull_refs(parsed.owner, parsed.repo, parsed.number)
    sha = refs.head_sha
    # Every path the PR touches, deleted and renamed-away ones included, so the
    # project-context reader can say when the PR changes a conventions or rules file.
    changed: set[str] = set()
    if parsed.provider == "github":
        items = host.list_pr_files(parsed.owner, parsed.repo, parsed.number)
        for item in items:
            changed.add(_normalize_rel(item["filename"]))
            if item.get("previous_filename"):
                changed.add(_normalize_rel(item["previous_filename"]))
    else:
        changes = host.list_mr_changes(parsed.owner, parsed.repo, parsed.number)
        items = []
        for change in changes:
            changed.add(_normalize_rel(change["new_path"]))
            if change.get("old_path"):
                changed.add(_normalize_rel(change["old_path"]))
            if not change.get("deleted_file"):
                items.append({"filename": change["new_path"]})

    files: dict[str, str] = {}
    skipped: list[str] = []
    excluded: dict[str, str] = {}
    # Path -> reason for files the hosting API answered without their content
    # (T124): reported as excluded, never reviewed or scanned as empty.
    withheld: dict[str, str] = {}

    def fetch(path: str) -> None:
        try:
            if parsed.provider == "github":
                content = host.get_file_contents(parsed.owner, parsed.repo, path, sha)
            else:
                content = host.get_file_at_ref(parsed.owner, parsed.repo, path, sha)
        except host.content_withheld as exc:
            withheld[path] = exc.reason
            ctx.log.warn(f"scope: {path} not reviewed: {exc.reason}")
            return
        files[path] = content

    for item in items:
        path = _normalize_rel(item["filename"])
        if not is_supported(path):
            skipped.append(path)
            continue
        # FR-029: excluded before the remote fetch, never requested.
        if _is_excluded(path, patterns, excluded):
            continue
        fetch(path)

    # FR-008: any file the PR touches that is a requirement source, found by
    # pattern rather than by an exact-name list, so a feature spec added under
    # specs/ is fetched when the PR changes it. Only sources in the PR are
    # available; this loop never looks outside the diff.
    for item in items:
        path = _normalize_rel(item["filename"])
        if not is_requirements_source(path) or path in files or path in withheld:
            continue
        if _is_excluded(path, patterns, excluded):
            continue
        fetch(path)
    return {
        "files": files,
        "skipped_languages": skipped,
        "excluded_files": _excluded_files(excluded, withheld),
        "input_revision": sha,
        "base_sha": refs.base_sha,
        "changed_paths": frozenset(changed),
    }


# At most this many ``*.csproj`` files are fetched from a PR's repository root.
MAX_PR_CSPROJ = 3


def _read_pr_project_sources(
    ctx: ScopeContext, head: str | None, base: str | None, changed_paths: frozenset[str]
) -> ProjectSources:
    """Read a PR's project-context files through the hosting API (T120,
    FR-009/FR-010/FR-011).

    Only the repository root is read, only through ``ctx.fetcher`` (GET
    requests), and each file keeps its bare root name.

    - Manifests at the head commit: the head listing says which of
      ``MANIFEST_NAMES`` exist as files; up to ``MAX_PR_CSPROJ`` ``*.csproj``
      files, sorted, follow them.
    - Conventions and ``.veritas/rules.md`` at the base commit, so a PR cannot
      change the rules it is reviewed by: the first of ``CONVENTIONS_FILENAMES``
      in the base listing whose text is not blank, and the rules file when it
      exists and is not blank. When the PR changes one of these files, the base
      version is still the one read, and a problem says so.

    A file whose listed size exceeds ``MAX_SOURCE_BYTES`` is not fetched; when
    the listing has no size (GitLab) the limit is applied to the UTF-8 length
    of the fetched text. pyproject.toml is decoded strictly, as the local
    reader decodes it. A provider error (anything but 404, which means absent)
    becomes one problem naming what was being read, and reading goes on; a
    failed root listing means nothing is read from that commit. No provider
    error leaves this function, so the review always continues. The provider
    error is the fetcher's ``api_error``: this module imports nothing from
    ``veritas.hosting`` (T111).

    Requests: one root listing per commit (GitLab: up to ``MAX_TREE_PAGES``
    pages each), at most 10 manifests, 3 conventions files and 1 rules file.
    """
    parsed = ctx.pr
    host = ctx.fetcher
    files: list[tuple[str, str]] = []
    problems: list[str] = []

    def root_files(ref: str, which: str, consequence: str) -> dict | None:
        """The files in the root listing at ``ref`` by name, or None when it failed."""
        try:
            entries = host.list_root(parsed.owner, parsed.repo, ref)
        except host.api_error as exc:
            problems.append(
                f"reading the repository root at the {which} commit failed: {exc}; {consequence}"
            )
            return None
        return {entry.name: entry for entry in entries if entry.type == "file"}

    def read(
        name: str, ref: str, which: str, size: int | None, *, strict_utf8: bool = False
    ) -> str | None:
        """The text of ``name`` at ``ref``, or None (absent, or a problem recorded)."""
        too_large = f"{name} over {MAX_SOURCE_BYTES} bytes, skipped"
        if size is not None and size > MAX_SOURCE_BYTES:
            problems.append(too_large)
            return None
        try:
            text = host.read_optional(parsed.owner, parsed.repo, name, ref, strict_utf8=strict_utf8)
        except UnicodeDecodeError:
            problems.append(f"{name} is not valid UTF-8, skipped")
            return None
        except host.api_error as exc:
            # A 404 never gets here: read_optional returns None for it.
            problems.append(f"reading {name} at the {which} commit failed: {exc}; skipped")
            return None
        if text is not None and size is None and len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
            problems.append(too_large)
            return None
        return text

    if not head:
        problems.append("head commit unknown; manifests not read")
    else:
        head_files = root_files(head, "head", "manifests not read")
        if head_files is not None:
            names = [name for name in MANIFEST_NAMES if name in head_files]
            names += sorted(name for name in head_files if is_csproj(name))[:MAX_PR_CSPROJ]
            for name in names:
                text = read(
                    name, head, "head", head_files[name].size, strict_utf8=name == "pyproject.toml"
                )
                if text is not None:
                    files.append((name, text))

    if not base:
        problems.append("base commit unknown; conventions and rules not read")
    else:
        base_files = root_files(base, "base", "conventions and rules not read")
        if base_files is not None:
            for name in CONVENTIONS_FILENAMES:
                if name not in base_files:
                    continue
                text = read(name, base, "base", base_files[name].size)
                if text is not None and text.strip():
                    files.append((name, text))
                    break
            problems_before = len(problems)
            rules = read(NATURAL_LANGUAGE_RULES_GLOB, base, "base", None)
            if rules is not None and rules.strip():
                files.append((NATURAL_LANGUAGE_RULES_GLOB, rules))
            # Present at the base: listed there, or (rules) found or failing to read.
            at_base = set(base_files)
            if rules is not None or len(problems) > problems_before:
                at_base.add(NATURAL_LANGUAGE_RULES_GLOB)
            # Said last, after every read problem from the base commit.
            for name in (*CONVENTIONS_FILENAMES, NATURAL_LANGUAGE_RULES_GLOB):
                if name not in changed_paths:
                    continue
                if name in at_base:
                    problems.append(f"{name} changed in this PR; the base version was used")
                else:
                    problems.append(f"{name} added in this PR; not used until it is in the base commit")

    origin = f"repository root at {head[:8]}" if head else "repository root (head commit unknown)"
    return ProjectSources(origin=origin, files=tuple(files), problems=tuple(problems))


def _run_local(scope: ReviewScope, target: str, patterns: Sequence[str]) -> dict:
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


def make_scope_node(ctx: ScopeContext) -> Callable[[ReviewState], dict]:
    def node(state: ReviewState) -> dict:
        scope: ReviewScope = state["scope"]
        target: str = state["target"]
        ctx.log.info(f"Scanning target in scope: {target}", scope=scope.value)

        if scope == ReviewScope.PR:
            # Fetching the PR is fatal by contract (contracts/hosting-api.md):
            # without it there is nothing to review. The provider error becomes
            # a ReviewFatalError, so the CLI prints it as one redacted [error]
            # line and exits 1 before any report is written (T121). Only
            # _fetch_pr's reads are fatal; the project-context reads below
            # catch their own errors (T120). A file whose content the API
            # withholds is not an error: _fetch_pr catches the fetcher's
            # content_withheld first and records the file as excluded (T124).
            try:
                result = _fetch_pr(ctx)
            except ctx.fetcher.api_error as exc:
                raise ReviewFatalError(" ".join(str(exc).split())) from exc
        else:
            result = _run_local(scope, target, ctx.exclude)

        _log_exclusions(
            ctx.log,
            {
                entry.path: entry.pattern
                for entry in result["excluded_files"]
                if entry.pattern is not None
            },
        )

        # FR-029: batch the post-exclusion, supported-language source files only.
        # Requirement documentation is not batched; it reaches the requirements
        # node whole.
        source_files = {path: text for path, text in result["files"].items() if is_supported(path)}
        plan, warnings = plan_batches(
            source_files,
            batch_chars=ctx.batch_chars,
            max_batches=ctx.max_batches,
        )
        for warning in warnings:
            ctx.log.warn(warning)
        ctx.log.info(
            f"batching: {len(plan.reviewed_files)} file(s) in {len(plan.batches)} batch(es) "
            f"of up to {ctx.batch_chars} chars; {len(plan.split_files)} split, "
            f"{len(plan.not_reviewed_files)} not reviewed"
        )

        # FR-004: the same post-exclusion source files and the same plan, so each
        # batch carries an index of the tests ranked for the code it holds. Excluded
        # files are absent from the index by construction, which is the point.
        test_indexes, index_stats = build_batch_test_indexes(source_files, plan)
        ctx.log.info(
            f"test index: {index_stats['test_files']} test file(s), "
            f"{index_stats['test_names']} test name(s); "
            f"up to {index_stats['max_chars']} chars per batch; "
            f"{index_stats['truncated_batches']} of {index_stats['batches']} "
            "batch(es) truncated"
        )

        files: dict[str, str] = result["files"]
        requirement_sources, requirements = _discover_requirements(ctx.log, files)
        # SAST scans exactly the post-exclusion file set (FR-029).
        sast = collect_sast(
            files, scope_value=scope.value, rules=ctx.opengrep_rules, jobs=ctx.opengrep_jobs
        )
        # T122: how long the scan took and with how many jobs, whenever the
        # OpenGrep process exited (a timeout already names its limit below).
        if sast.elapsed_seconds is not None:
            ctx.log.info(
                f"OpenGrep scan finished in {sast.elapsed_seconds:.1f} s (-j {sast.jobs})"
            )
        if sast.degraded:
            ctx.log.warn(sast.degraded)
            # The detail is the same single line as the reason when the reason
            # came from stderr; log it only when it adds something.
            if sast.scan_error and sast.scan_error != sast.degraded:
                ctx.log.warn(f"OpenGrep scan detail: {sast.scan_error}")

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
        # FR-009/FR-010/FR-011: read last, so the log up to here is unchanged.
        # A PR review reads them through the hosting API (T120).
        if scope == ReviewScope.PR:
            project_sources = _read_pr_project_sources(
                ctx, result["input_revision"], result["base_sha"], result["changed_paths"]
            )
        else:
            project_sources = _read_project_sources(_project_context_start(scope, target))
        return {
            "files": files,
            "skipped_languages": result["skipped_languages"],
            "excluded_files": result["excluded_files"],
            "batch_plan": plan,
            "test_indexes": test_indexes,
            "requirement_sources": requirement_sources,
            "requirements": requirements,
            "project_sources": project_sources,
            "degraded_sast": sast.degraded,
            "sast_findings": sast.findings,
            "run": run,
        }

    return node