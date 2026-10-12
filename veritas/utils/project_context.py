"""Project-context parsing (T056, FR-009/FR-010/FR-011).

Detects language/framework versions from real manifest files, project
conventions (AGENTS.md-equivalent), and natural-language custom rules. When
nothing is detectable a ``note`` records that project context is unavailable.

Since T116 nothing here touches the filesystem: the scope node reads the files
into ``ProjectSources`` and the parsers below take text. The names to read and
the size limit live here so the reader and the parsers agree on them.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from veritas.config.constants import CONVENTIONS_FILENAMES, NATURAL_LANGUAGE_RULES_GLOB

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
# Every file but pyproject.toml larger than this is skipped unread.
MAX_SOURCE_BYTES = 200_000

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
            lines.append(self.conventions.strip()[:4000])
        if self.rules:
            lines.append("Project-specific natural-language review rules:")
            lines.append(self.rules.strip()[:4000])
        return "\n".join(lines)


def _parse_pyproject(text: str) -> list[str] | None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    parts: list[str] = []
    requires_python = data.get("project", {}).get("requires-python")
    if requires_python:
        parts.append(f"Python {requires_python}")
    deps = data.get("project", {}).get("dependencies", []) or []
    for dep in deps:
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


def detect_conventions(sources: ProjectSources) -> str | None:
    """Project conventions/AGENTS.md-equivalent (FR-010): the first non-empty one."""
    for name in CONVENTIONS_FILENAMES:
        text = sources.text(name)
        if text:
            return text.strip()
    return None


def detect_natural_language_rules(sources: ProjectSources) -> str | None:
    """Natural-language-only custom rules (FR-011)."""
    text = sources.text(NATURAL_LANGUAGE_RULES_GLOB)
    if text:
        return text.strip()
    return None


def build_project_context(sources: ProjectSources) -> ProjectContext:
    """Gather all project context from the files the scope node read."""
    ctx = ProjectContext()
    ctx.language_versions = detect_language_versions(sources)
    ctx.conventions = detect_conventions(sources)
    ctx.rules = detect_natural_language_rules(sources)
    if ctx.is_empty:
        ctx.note = "Project context unavailable (no detectable manifests, conventions, or custom rules); using generic guidance."
    return ctx
