"""Project-context parsing (T056, FR-009/FR-010/FR-011).

Detects language/framework versions from real manifest files, project
conventions (AGENTS.md-equivalent), and natural-language custom rules. When
nothing is detectable a ``note`` records that project context is unavailable.
Conventions and rules text is redacted before it reaches the model (FR-013).

Since T116 nothing here touches the filesystem: the scope node reads the files
into ``ProjectSources`` and the parsers below take text. The names to read and
the size limit live here so the reader and the parsers agree on them.
``display_origin`` reads the current working directory but only does path
arithmetic on strings.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from veritas.config.constants import CONVENTIONS_FILENAMES, NATURAL_LANGUAGE_RULES_GLOB
from veritas.utils.redaction import redact_secrets

if TYPE_CHECKING:
    from veritas.review.state import ProjectSources

# Manifests probed by exact name directly in the project directory, in the
# order their versions are listed. ``*.csproj`` files follow them.
MANIFEST_NAMES = (
    "pyproject.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "package.json",
    "go.mod",
    "Cargo.toml",
)
CSPROJ_SUFFIX = ".csproj"
# Every file larger than this is skipped unread, and the reader says so (T118).
MAX_SOURCE_BYTES = 200_000
# At most this many characters of conventions, and of rules, reach the model.
MAX_PROMPT_CHARS = 4000

_MAVEN_COMPILER = re.compile(
    r"<maven\.compiler\.(?:source|target)>\s*(\d[\w.]*)\s*</maven\.compiler\.(?:source|target)>"
)
_MAVEN_JAVA = re.compile(r"<java\.version>\s*(\d[\w.]*)\s*</java\.version>")
_GRADLE_SOURCE_COMPAT = re.compile(r"sourceCompatibility\s*=\s*['\"]?([\w.]+)['\"]?")
_GRADLE_JAVA_VERSION = re.compile(r"languageVersion\s*=\s*JavaLanguageVersion\.of\(\s*(\d+)\s*\)")
_GO_VERSION = re.compile(r"^\s*go\s+([\d.]+)\s*$", re.MULTILINE)
_CARGO_EDITION = re.compile(r"^\s*edition\s*=\s*[\"']([\d]+)[\"']\s*$", re.MULTILINE)
_CSHARP_TFM = re.compile(r"<TargetFramework(?:s)?>([^<]+)</TargetFramework(?:s)?>")
_NODE_ENGINES = re.compile(r"\"engines\"\s*:\s*\{[^}]*\"node\"\s*:\s*\"([^\"]+)\"")
_DEP_SPEC = re.compile(r"^([A-Za-z0-9_.\-]+)\s*([<>=!~].*)$")


@dataclass
class ProjectContext:
    """Detected project context for prompt injection (FR-009/FR-010/FR-011)."""

    language_versions: list[str] = field(default_factory=list)
    conventions: str | None = None
    rules: str | None = None
    note: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.language_versions and not self.conventions and not self.rules

    def render(self) -> str:
        """Human-readable context block for prompt assembly."""
        lines: list[str] = []
        if self.note:
            lines.append(f"Note: {self.note}")
        if self.language_versions:
            lines.append("Detected manifest language/framework versions:")
            for entry in self.language_versions:
                lines.append(f"  - {entry}")
        if self.conventions:
            lines.append("Project conventions to honor:")
            lines.append(self.conventions.strip()[:MAX_PROMPT_CHARS])
        if self.rules:
            lines.append("Project-specific natural-language review rules:")
            lines.append(self.rules.strip()[:MAX_PROMPT_CHARS])
        return "\n".join(lines)


PYPROJECT_NOT_TOML = "pyproject.toml is not valid TOML"
PYPROJECT_BAD_LAYOUT = "pyproject.toml has an unexpected [project] layout"


def _pyproject_project(text: str) -> tuple[dict, str | None]:
    """The ``[project]`` table of a pyproject.toml and the problem with it, if any (T118).

    Valid TOML can still have the wrong shape: ``[project]`` that is not a
    table, ``requires-python`` that is not a string or ``dependencies`` that
    is not a list. Each is the layout problem, and the value of the wrong type
    is dropped so nothing downstream can trip on it; the rest of the table is
    kept. Non-string items inside a ``dependencies`` list are not a problem:
    the parser skips them. A missing ``[project]`` is an empty table.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}, PYPROJECT_NOT_TOML
    project = data.get("project", {})
    if not isinstance(project, dict):
        return {}, PYPROJECT_BAD_LAYOUT
    project = dict(project)
    problem = None
    if "requires-python" in project and not isinstance(project["requires-python"], str):
        del project["requires-python"]
        problem = PYPROJECT_BAD_LAYOUT
    if "dependencies" in project and not isinstance(project["dependencies"], list):
        del project["dependencies"]
        problem = PYPROJECT_BAD_LAYOUT
    return project, problem


def _parse_pyproject(text: str) -> list[str] | None:
    project, _problem = _pyproject_project(text)
    parts: list[str] = []
    requires_python = project.get("requires-python")
    if requires_python:
        parts.append(f"Python {requires_python}")
    for dep in project.get("dependencies", []):
        if not isinstance(dep, str):
            continue
        match = _DEP_SPEC.match(dep)
        if match is None:
            continue
        name = match.group(1).strip()
        version = match.group(2).strip()
        parts.append(f"{name} {version}")
    return parts or None


def _parse_pom(text: str) -> str | None:
    if not text:
        return None
    match = _MAVEN_JAVA.search(text) or _MAVEN_COMPILER.search(text)
    if match:
        return f"Java {match.group(1)} (Maven)"
    return None


def _parse_gradle(text: str) -> str | None:
    if not text:
        return None
    match = _GRADLE_JAVA_VERSION.search(text)
    if match:
        return f"Java {match.group(1)} (Gradle toolchain)"
    match = _GRADLE_SOURCE_COMPAT.search(text)
    if match:
        return f"Java {match.group(1)} (Gradle)"
    return None


def _parse_package_json(text: str) -> str | None:
    if not text:
        return None
    match = _NODE_ENGINES.search(text)
    if match:
        return f"Node {match.group(1)}"
    return None


def _parse_go_mod(text: str) -> str | None:
    if not text:
        return None
    match = _GO_VERSION.search(text)
    if match:
        return f"Go {match.group(1)}"
    return None


def _parse_cargo(text: str) -> str | None:
    if not text:
        return None
    match = _CARGO_EDITION.search(text)
    if match:
        return f"Rust edition {match.group(1)}"
    return None


def _parse_csproj(text: str) -> str | None:
    if not text:
        return None
    match = _CSHARP_TFM.search(text)
    if match:
        return f".NET {match.group(1)}"
    return None


_MANIFEST_PARSERS = {
    "pyproject.toml": _parse_pyproject,
    "pom.xml": _parse_pom,
    "build.gradle": _parse_gradle,
    "build.gradle.kts": _parse_gradle,
    "package.json": _parse_package_json,
    "go.mod": _parse_go_mod,
    "Cargo.toml": _parse_cargo,
}


def is_csproj(name: str) -> bool:
    """A ``*.csproj`` name, in any letter case (the reader's glob is case-blind on Windows)."""
    return name.lower().endswith(CSPROJ_SUFFIX)


def detect_language_versions(sources: ProjectSources) -> list[str]:
    """Versions from the manifests in ``sources``: named ones first, then ``*.csproj`` in read order."""
    versions: list[str] = []
    for name in MANIFEST_NAMES:
        text = sources.text(name)
        if text is None:
            continue
        _append_versions(_MANIFEST_PARSERS[name](text), versions)
    for name, text in sources.files:
        if is_csproj(name):
            _append_versions(_parse_csproj(text), versions)
    return versions


def _append_versions(parsed: object, versions: list[str]) -> None:
    """Append a parser result (``str`` or ``list[str]`` or ``None``)."""
    if isinstance(parsed, list):
        for item in parsed:
            if item not in versions:
                versions.append(item)
        return
    if isinstance(parsed, str) and parsed not in versions:
        versions.append(parsed)


def _conventions_source(sources: ProjectSources) -> tuple[str, str] | None:
    """The conventions file used: the first in ``CONVENTIONS_FILENAMES`` order
    whose text is not blank (a whitespace-only file is passed over, T118)."""
    for name in CONVENTIONS_FILENAMES:
        text = sources.text(name)
        if text and text.strip():
            return name, text
    return None


def detect_conventions(sources: ProjectSources) -> str | None:
    """Project conventions/AGENTS.md-equivalent (FR-010): the first non-blank one, redacted."""
    found = _conventions_source(sources)
    return redact_secrets(found[1].strip()) if found is not None else None


def detect_natural_language_rules(sources: ProjectSources) -> str | None:
    """Natural-language-only custom rules (FR-011), redacted."""
    text = sources.text(NATURAL_LANGUAGE_RULES_GLOB)
    if text and text.strip():
        return redact_secrets(text.strip())
    return None


def project_context_problems(sources: ProjectSources) -> list[str]:
    """Every problem the report states about this project context (T118, FR-009).

    First the reading problems the scope node recorded (too large, unreadable,
    invalid UTF-8), in read order. Then what only parsing can tell: a
    pyproject.toml that is not valid TOML, or valid TOML whose ``[project]``
    has the wrong shape (``_pyproject_project``). Then a truncation for conventions
    and for rules longer than ``MAX_PROMPT_CHARS``, measured on the redacted
    text ``ProjectContext.render`` cuts. Redaction runs before the cut so a
    cut can never leave half a secret that no longer matches a pattern.

    A regex-parsed manifest that yields nothing is not a problem: it may simply
    not declare a version.
    """
    problems = list(sources.problems)
    pyproject = sources.text("pyproject.toml")
    if pyproject is not None:
        _project, problem = _pyproject_project(pyproject)
        if problem is not None:
            problems.append(problem)
    conventions = _conventions_source(sources)
    shown = (
        (conventions[0] if conventions else None, detect_conventions(sources)),
        (NATURAL_LANGUAGE_RULES_GLOB, detect_natural_language_rules(sources)),
    )
    for name, text in shown:
        if name is not None and text is not None and len(text.strip()) > MAX_PROMPT_CHARS:
            problems.append(f"{name} truncated to {MAX_PROMPT_CHARS} characters")
    return problems


def contributing_sources(sources: ProjectSources) -> list[str]:
    """The names of the files that put something into the rendered context, in read order.

    A file counts only when it changes what ``ProjectContext.render`` shows: a
    manifest when its parser extracts at least one version from it, the
    conventions file ``detect_conventions`` picked when its text is not blank,
    and the rules file when its text is not blank. A file that was read but
    parsed to nothing (a pyproject.toml with no requires-python and no pinned
    dependency, a package.json without engines.node) is not a source.
    """
    conventions = _conventions_source(sources)
    used: list[str] = []
    for name, text in sources.files:
        if name in _MANIFEST_PARSERS:
            contributed = bool(_MANIFEST_PARSERS[name](text))
        elif is_csproj(name):
            contributed = _parse_csproj(text) is not None
        elif conventions is not None and name == conventions[0]:
            contributed = bool(text.strip())
        elif name == NATURAL_LANGUAGE_RULES_GLOB:
            contributed = bool(text.strip())
        else:
            contributed = False
        if contributed:
            used.append(name)
    return used


def display_origin(origin: str, cwd: str | None = None) -> str:
    """Where project context was read from, as the report shows it: never an absolute path.

    The origin itself when it is the working directory (``"."``); the path
    relative to the working directory, with ``/`` separators, when it lies
    below it; otherwise only the origin's directory name. Pure path arithmetic
    on strings: nothing on disk is read.
    """
    here = os.path.normpath(os.path.abspath(cwd if cwd is not None else os.getcwd()))
    there = os.path.normpath(os.path.abspath(origin))
    try:
        inside = os.path.normcase(os.path.commonpath([here, there])) == os.path.normcase(here)
    except ValueError:  # different drives on Windows
        inside = False
    if inside:
        return os.path.relpath(there, here).replace(os.sep, "/")
    # A filesystem root has no name; say so rather than print the root itself.
    return os.path.basename(there) or "(filesystem root)"


def build_project_context(sources: ProjectSources) -> ProjectContext:
    """Gather all project context from the files the scope node read."""
    ctx = ProjectContext()
    ctx.language_versions = detect_language_versions(sources)
    ctx.conventions = detect_conventions(sources)
    ctx.rules = detect_natural_language_rules(sources)
    if ctx.is_empty:
        ctx.note = "Project context unavailable (no detectable manifests, conventions, or custom rules); using generic guidance."
    return ctx
