"""Unit tests — project-context detection (T056/T116, FR-009/FR-010/FR-011).

Since T116 the parsers take text and ``build_project_context`` takes the
``ProjectSources`` the scope node read; the reading itself is tested through
``scope._read_project_sources``.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from veritas.models.entities import ReviewScope
from veritas.review.nodes import context as context_module
from veritas.review.nodes.scope import _project_context_root, _read_project_sources
from veritas.review.state import ProjectSources
from veritas.utils.project_context import (
    _parse_cargo,
    _parse_csproj,
    _parse_go_mod,
    _parse_gradle,
    _parse_package_json,
    _parse_pom,
    _parse_pyproject,
    build_project_context,
    detect_conventions,
    detect_language_versions,
    detect_natural_language_rules,
)


def _sources(*files: tuple[str, str]) -> ProjectSources:
    return ProjectSources(origin="/project", files=files)


# --- parsers take text ---------------------------------------------------------


def test_parse_pyproject_python_version_and_pinned_deps():
    text = (
        '[project]\nname="x"\nrequires-python = ">=3.12"\n'
        'dependencies = ["pydantic>=2.0", "typer", "httpx>=0.27,<1"]\n'
    )
    assert _parse_pyproject(text) == ["Python >=3.12", "pydantic >=2.0", "httpx >=0.27,<1"]


def test_parse_pyproject_invalid_toml_or_nothing_detectable_is_none():
    assert _parse_pyproject("[project\n") is None
    assert _parse_pyproject("") is None
    assert _parse_pyproject('[project]\nname = "x"\n') is None


def test_parse_pyproject_accepts_crlf_like_tomllib_load():
    assert _parse_pyproject('[project]\r\nrequires-python = ">=3.11"\r\n') == ["Python >=3.11"]


def test_parse_pom_java_version_then_compiler_property():
    assert _parse_pom("<properties><java.version>17</java.version></properties>") == "Java 17 (Maven)"
    assert (
        _parse_pom("<maven.compiler.source>11</maven.compiler.source>") == "Java 11 (Maven)"
    )
    assert _parse_pom("<project/>") is None


def test_parse_gradle_toolchain_then_source_compatibility():
    assert (
        _parse_gradle("languageVersion = JavaLanguageVersion.of(21)")
        == "Java 21 (Gradle toolchain)"
    )
    assert _parse_gradle("sourceCompatibility = '17'") == "Java 17 (Gradle)"
    assert _parse_gradle("plugins {}") is None


def test_parse_package_json_node_engines():
    assert _parse_package_json('{"engines": {"node": ">=20"}}') == "Node >=20"
    assert _parse_package_json('{"name": "x"}') is None


def test_parse_go_mod_go_directive():
    assert _parse_go_mod("module example.com/x\n\ngo 1.22\n") == "Go 1.22"
    assert _parse_go_mod("module example.com/x\n") is None


def test_parse_cargo_edition():
    assert _parse_cargo('[package]\nedition = "2021"\n') == "Rust edition 2021"
    assert _parse_cargo("[package]\n") is None


def test_parse_csproj_target_framework():
    assert _parse_csproj("<TargetFramework>net8.0</TargetFramework>") == ".NET net8.0"
    assert _parse_csproj("<TargetFrameworks>net8.0;net6.0</TargetFrameworks>") == ".NET net8.0;net6.0"
    assert _parse_csproj("<Project/>") is None


@pytest.mark.parametrize(
    "parser",
    [_parse_pom, _parse_gradle, _parse_package_json, _parse_go_mod, _parse_cargo, _parse_csproj],
)
def test_text_parsers_return_none_for_empty_text(parser):
    assert parser("") is None


# --- building from ProjectSources ----------------------------------------------


def test_versions_follow_manifest_order_then_csproj_in_read_order_without_duplicates():
    sources = _sources(
        ("B.csproj", "<TargetFramework>net6.0</TargetFramework>"),
        ("go.mod", "go 1.22\n"),
        ("pyproject.toml", '[project]\nrequires-python = ">=3.12"\n'),
        ("a.CSPROJ", "<TargetFramework>net8.0</TargetFramework>"),
        ("c.csproj", "<TargetFramework>net6.0</TargetFramework>"),
    )
    assert detect_language_versions(sources) == [
        "Python >=3.12",
        "Go 1.22",
        ".NET net6.0",
        ".NET net8.0",
    ]


def test_conventions_first_non_empty_in_filename_order():
    sources = _sources(("STYLE.md", "Style.\n"), ("AGENTS.md", "  \n"), ("CONVENTIONS.md", ""))
    # AGENTS.md is whitespace, which is non-empty text: it wins and strips to "".
    assert detect_conventions(sources) == ""
    assert detect_conventions(_sources(("AGENTS.md", ""), ("STYLE.md", " Style. \n"))) == "Style."
    assert detect_conventions(_sources()) is None


def test_rules_from_veritas_rules_md():
    assert detect_natural_language_rules(_sources((".veritas/rules.md", " Flag eval().\n"))) == "Flag eval()."
    assert detect_natural_language_rules(_sources((".veritas/rules.md", ""))) is None


def test_empty_sources_note_unavailable():
    ctx = build_project_context(_sources())
    assert ctx.is_empty
    assert ctx.note == (
        "Project context unavailable (no detectable manifests, conventions, or "
        "custom rules); using generic guidance."
    )


def test_full_context_renders():
    ctx = build_project_context(
        _sources(
            ("pyproject.toml", '[project]\nrequires-python = ">=3.12"\n'),
            ("AGENTS.md", "Prefer early returns."),
        )
    )
    assert not ctx.is_empty
    assert ctx.render() == (
        "Detected manifest language/framework versions:\n"
        "  - Python >=3.12\n"
        "Project conventions to honor:\n"
        "Prefer early returns."
    )


# --- ProjectSources is immutable -------------------------------------------------


def test_project_sources_rejects_field_assignment():
    sources = _sources(("go.mod", "go 1.22\n"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        sources.files = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        sources.origin = "/elsewhere"
    assert not hasattr(sources, "__dict__")


def test_project_sources_files_cannot_be_modified():
    sources = _sources(("go.mod", "go 1.22\n"))
    with pytest.raises(TypeError):
        sources.files["go.mod"] = "go 1.0\n"
    with pytest.raises(TypeError):
        sources.files[0] = ("go.mod", "go 1.0\n")
    with pytest.raises(AttributeError):
        sources.files.append(("x", "y"))
    assert sources.text("go.mod") == "go 1.22\n"


def test_project_sources_copies_lists_into_tuples():
    files = [["go.mod", "go 1.22\n"]]
    problems = ["one"]
    sources = ProjectSources(origin="/p", files=files, problems=problems)
    files.append(["x", "y"])
    problems.append("two")
    assert sources.files == (("go.mod", "go 1.22\n"),)
    assert sources.problems == ("one",)


def test_project_sources_rejects_duplicate_names():
    with pytest.raises(ValueError, match="duplicate"):
        _sources(("go.mod", "a"), ("go.mod", "b"))


def test_project_sources_text_is_none_for_a_name_not_read():
    assert _sources().text("go.mod") is None


# --- the context node never touches the filesystem -------------------------------


def test_context_node_imports_no_filesystem_module_and_never_calls_open():
    tree = ast.parse(Path(context_module.__file__).read_text(encoding="utf-8"))
    forbidden = {"os", "pathlib", "io"}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in forbidden]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in forbidden:
                found.append(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "open":
                found.append("open()")
        elif isinstance(node, ast.Name) and node.id in forbidden | {"open"}:
            found.append(node.id)
    assert found == []


# --- the scope node's reader -------------------------------------------------------


def _write(path: Path, text: str) -> None:
    """Exact bytes: no newline translation, so sizes and texts are what the test says."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def test_reader_reads_the_files_in_order(tmp_path):
    _write(tmp_path / "go.mod", "go 1.22\n")
    _write(tmp_path / "pyproject.toml", "[project]\n")
    _write(tmp_path / "b.csproj", "<b/>")
    _write(tmp_path / "a.csproj", "<a/>")
    _write(tmp_path / "AGENTS.md", "Agents.\n")
    _write(tmp_path / ".veritas" / "rules.md", "Rules.\n")
    sources = _read_project_sources(str(tmp_path))
    assert sources.origin == str(tmp_path)
    assert [name for name, _ in sources.files] == [
        "pyproject.toml",
        "go.mod",
        "a.csproj",
        "b.csproj",
        "AGENTS.md",
        ".veritas/rules.md",
    ]
    assert sources.problems == ()


def test_reader_stops_at_the_first_non_empty_conventions_file(tmp_path):
    _write(tmp_path / "AGENTS.md", "")
    _write(tmp_path / "CONVENTIONS.md", "Conventions.\n")
    _write(tmp_path / "STYLE.md", "Style.\n")
    sources = _read_project_sources(str(tmp_path))
    assert sources.files == (("AGENTS.md", ""), ("CONVENTIONS.md", "Conventions.\n"))


def test_reader_skips_a_file_over_200000_bytes_silently(tmp_path):
    _write(tmp_path / "go.mod", "go 1.22\n" + "/" * (200_001 - 8))
    _write(tmp_path / "AGENTS.md", "a" * 200_000)
    sources = _read_project_sources(str(tmp_path))
    assert [name for name, _ in sources.files] == ["AGENTS.md"]
    assert sources.problems == ()


def test_reader_reads_a_pyproject_over_200000_bytes(tmp_path):
    text = '[project]\nrequires-python = ">=3.12"\n' + "#" * 210_000 + "\n"
    _write(tmp_path / "pyproject.toml", text)
    sources = _read_project_sources(str(tmp_path))
    assert sources.text("pyproject.toml") == text


def test_reader_keeps_todays_error_for_a_pyproject_that_is_not_utf8(tmp_path):
    """Kept from before T116, where ``tomllib.load`` raised it; not a design choice."""
    (tmp_path / "pyproject.toml").write_bytes(b'[project]\nname = "\xff"\n')
    with pytest.raises(UnicodeDecodeError):
        _read_project_sources(str(tmp_path))


def test_reader_replaces_invalid_utf8_in_other_files(tmp_path):
    (tmp_path / "go.mod").write_bytes(b"go 1.22 \xff\n")
    assert _read_project_sources(str(tmp_path)).text("go.mod") == "go 1.22 �\n"


def test_reader_does_not_walk_up_or_down(tmp_path):
    _write(tmp_path / "go.mod", "go 1.22\n")
    _write(tmp_path / "src" / "pkg" / "go.mod", "go 1.21\n")
    assert _read_project_sources(str(tmp_path / "src")).files == ()


def test_reader_of_a_missing_directory_reads_nothing(tmp_path):
    assert _read_project_sources(str(tmp_path / "missing")).files == ()


def test_context_root_is_the_files_directory_for_file_scope(tmp_path):
    target = tmp_path / "src" / "app.py"
    assert _project_context_root(ReviewScope.FILE, str(target)) == str(tmp_path / "src")
    assert _project_context_root(ReviewScope.MODULE, str(tmp_path / "src")) == str(tmp_path / "src")
    assert _project_context_root(ReviewScope.PROJECT, str(tmp_path)) == str(tmp_path)
