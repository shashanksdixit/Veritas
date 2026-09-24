"""Project-context parsing (T056, FR-009/FR-010/FR-011).

Detects language/framework versions from real manifest files, project
conventions (AGENTS.md-equivalent), and natural-language custom rules. When
nothing is detectable a ``note`` records that project context is unavailable.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from veritas.config.constants import CONVENTIONS_FILENAMES, NATURAL_LANGUAGE_RULES_GLOB

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


def _parse_pyproject(path: Path) -> list[str] | None:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
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


def _parse_pom(path: Path) -> str | None:
    text = _read_text(path)
    if not text:
        return None
    match = _MAVEN_JAVA.search(text) or _MAVEN_COMPILER.search(text)
    if match:
        return f"Java {match.group(1)} (Maven)"
    return None


def _parse_gradle(path: Path) -> str | None:
    text = _read_text(path)
    if not text:
        return None
    match = _GRADLE_JAVA_VERSION.search(text)
    if match:
        return f"Java {match.group(1)} (Gradle toolchain)"
    match = _GRADLE_SOURCE_COMPAT.search(text)
    if match:
        return f"Java {match.group(1)} (Gradle)"
    return None


def _parse_package_json(path: Path) -> str | None:
    text = _read_text(path)
    if not text:
        return None
    match = _NODE_ENGINES.search(text)
    if match:
        return f"Node {match.group(1)}"
    return None


def _parse_go_mod(path: Path) -> str | None:
    text = _read_text(path)
    if not text:
        return None
    match = _GO_VERSION.search(text)
    if match:
        return f"Go {match.group(1)}"
    return None


def _parse_cargo(path: Path) -> str | None:
    text = _read_text(path)
    if not text:
        return None
    match = _CARGO_EDITION.search(text)
    if match:
        return f"Rust edition {match.group(1)}"
    return None


def _parse_csproj(path: Path) -> str | None:
    text = _read_text(path)
    if not text:
        return None
    match = _CSHARP_TFM.search(text)
    if match:
        return f".NET {match.group(1)}"
    return None


def _read_text(path: Path, limit: int = 200_000) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _manifest_parsers() -> dict[str, object]:
    return {
        "pyproject.toml": _parse_pyproject,
        "pom.xml": _parse_pom,
        "build.gradle": _parse_gradle,
        "build.gradle.kts": _parse_gradle,
        "package.json": _parse_package_json,
        "go.mod": _parse_go_mod,
        "Cargo.toml": _parse_cargo,
        "*.csproj": _parse_csproj,
    }


def detect_language_versions(root: str) -> list[str]:
    """Parse manifest files under ``root`` (top-level scan).

    Python 3.12 pathlib.glob lacks `{}` brace alternation, so named manifests
    are probed explicitly and ``*.csproj`` via a plain suffix glob.
    """
    versions: list[str] = []
    root_path = Path(root)
    if not root_path.is_dir():
        return versions
    parsers = _manifest_parsers()
    classic = [
        "pyproject.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "package.json",
        "go.mod",
        "Cargo.toml",
    ]
    for name in classic:
        path = root_path / name
        if not path.is_file():
            continue
        handler = parsers.get(name)
        if handler is None:
            continue
        _append_versions(parsed := handler(path), versions)
    for path in sorted(root_path.glob("*.csproj")):
        if not path.is_file():
            continue
        _append_versions(parsers.get("*.csproj")(path), versions)
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


def detect_conventions(root: str) -> str | None:
    """Load project conventions/AGENTS.md-equivalent (FR-010)."""
    root_path = Path(root)
    if not root_path.is_dir():
        return None
    for name in CONVENTIONS_FILENAMES:
        candidate = root_path / name
        if candidate.is_file():
            text = _read_text(candidate)
            if text:
                return text.strip()
    return None


def detect_natural_language_rules(root: str) -> str | None:
    """Load natural-language-only custom rules (FR-011)."""
    candidate = Path(root) / NATURAL_LANGUAGE_RULES_GLOB
    if candidate.is_file():
        text = _read_text(candidate)
        if text:
            return text.strip()
    return None


def build_project_context(root: str) -> ProjectContext:
    """Gather all project context for a local target directory."""
    ctx = ProjectContext()
    ctx.language_versions = detect_language_versions(root)
    ctx.conventions = detect_conventions(root)
    ctx.rules = detect_natural_language_rules(root)
    if ctx.is_empty:
        ctx.note = "Project context unavailable (no detectable manifests, conventions, or custom rules); using generic guidance."
    return ctx