"""Unit tests — project-context detection (T056, FR-009/FR-010/FR-011)."""

import pytest

from veritas.utils.project_context import (
    build_project_context,
    detect_conventions,
    detect_language_versions,
    detect_natural_language_rules,
)


def test_pyproject_python_version_and_deps(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nrequires-python = ">=3.12"\ndependencies = ["pydantic>=2.0", "typer"]\n',
        encoding="utf-8",
    )
    versions = detect_language_versions(str(tmp_path))
    assert any("Python >=3.12" in v for v in versions)
    assert any(v == "pydantic >=2.0" for v in versions)
    assert not any("typer" in v for v in versions)  # no version pin


def test_pom_java_version(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><java.version>17</java.version></properties></project>",
        encoding="utf-8",
    )
    assert detect_language_versions(str(tmp_path)) == ["Java 17 (Maven)"]


def test_package_json_node_engines(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"engines": {"node": ">=20"}}', encoding="utf-8"
    )
    assert detect_language_versions(str(tmp_path)) == ["Node >=20"]


def test_conventions_detected(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Prefer early returns.", encoding="utf-8")
    assert "Prefer early returns." in detect_conventions(str(tmp_path))


def test_nl_rules_detected(tmp_path):
    rules = tmp_path / ".veritas"
    rules.mkdir()
    (rules / "rules.md").write_text("Flag any eval() usage.", encoding="utf-8")
    assert "eval()" in detect_natural_language_rules(str(tmp_path))


def test_empty_project_notes_unavailable(tmp_path):
    ctx = build_project_context(str(tmp_path))
    assert ctx.is_empty
    assert ctx.note is not None


def test_full_context_renders(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n', encoding="utf-8"
    )
    (tmp_path / "AGENTS.md").write_text("Prefer early returns.", encoding="utf-8")
    ctx = build_project_context(str(tmp_path))
    assert not ctx.is_empty
    rendered = ctx.render()
    assert "Python >=3.12" in rendered
    assert "Prefer early returns." in rendered