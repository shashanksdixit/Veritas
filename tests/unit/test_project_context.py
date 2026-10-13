"""Unit tests — project-context detection (T056/T116/T118, FR-009/FR-010/FR-011).

Since T116 the parsers take text and ``build_project_context`` takes the
``ProjectSources`` the scope node read; the reading itself is tested through
``scope._read_project_sources``. T118 adds the problems the report states
(reading, TOML, truncation), blank conventions passed over, and redaction.
"""

from __future__ import annotations

import ast
import dataclasses
import io
from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import fake_secret
from veritas.models.entities import ProjectContextStatus, ReviewRun, ReviewScope
from veritas.review.contexts import ReviewContext
from veritas.review.nodes import context as context_module
from veritas.review.nodes.context import make_context_node, project_context_record
from veritas.review.nodes.scope import _project_context_root, _read_project_sources
from veritas.review.state import ProjectSources
from veritas.utils.logging import Log
from veritas.utils.project_context import (
    _parse_cargo,
    _parse_csproj,
    _parse_go_mod,
    _parse_gradle,
    _parse_package_json,
    _parse_pom,
    _parse_pyproject,
    build_project_context,
    contributing_sources,
    detect_conventions,
    detect_language_versions,
    detect_natural_language_rules,
    display_origin,
    project_context_problems,
)
from veritas.utils.redaction import redact_secrets


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


_BAD_LAYOUT = "pyproject.toml has an unexpected [project] layout"


@pytest.mark.parametrize(
    "text, parsed, problems",
    [
        # [project] is a string, not a table: was AttributeError.
        ('project = "x"\n', None, [_BAD_LAYOUT]),
        # requires-python is a number: was silently shown as "Python 3.12".
        ("[project]\nrequires-python = 3.12\n", None, [_BAD_LAYOUT]),
        # dependencies is a string: was iterated one character at a time.
        ('[project]\ndependencies = "pydantic>=2"\n', None, [_BAD_LAYOUT]),
        # A number inside a valid list is skipped cleanly, not a problem.
        ('[project]\ndependencies = ["pydantic>=2", 3]\n', ["pydantic >=2"], []),
    ],
    ids=["project-not-a-table", "requires-python-a-number", "dependencies-a-string", "number-in-list"],
)
def test_parse_pyproject_unexpected_layout_never_raises(text, parsed, problems):
    """T118: valid TOML of an unexpected shape is a stated problem or a clean skip."""
    assert _parse_pyproject(text) == parsed
    assert project_context_problems(_sources(("pyproject.toml", text))) == problems


def test_parse_pyproject_keeps_the_parts_of_the_right_type():
    """A wrong-typed requires-python is dropped; the valid dependencies still count."""
    text = '[project]\nrequires-python = 3.12\ndependencies = ["pydantic>=2"]\n'
    sources = ProjectSources(origin="/project", files=(("pyproject.toml", text),))
    assert _parse_pyproject(text) == ["pydantic >=2"]
    record = project_context_record(ReviewScope.PROJECT, sources)
    assert record["project_context_status"] is ProjectContextStatus.PARTIAL
    assert record["project_context_sources"] == ["pyproject.toml"]
    assert record["project_context_reason"] == _BAD_LAYOUT


def test_pyproject_without_a_project_table_is_not_a_problem():
    text = "[tool.ruff]\nline-length = 100\n"
    assert _parse_pyproject(text) is None
    assert project_context_problems(_sources(("pyproject.toml", text))) == []


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


def test_conventions_first_non_blank_in_filename_order():
    """T118: a whitespace-only file is passed over, so a blank AGENTS.md no longer hides STYLE.md."""
    sources = _sources(("STYLE.md", "Style.\n"), ("AGENTS.md", "  \n"), ("CONVENTIONS.md", ""))
    assert detect_conventions(sources) == "Style."
    assert detect_conventions(_sources(("AGENTS.md", ""), ("STYLE.md", " Style. \n"))) == "Style."
    assert detect_conventions(_sources(("AGENTS.md", " \t\n"))) is None
    assert detect_conventions(_sources()) is None


def test_rules_from_veritas_rules_md():
    assert detect_natural_language_rules(_sources((".veritas/rules.md", " Flag eval().\n"))) == "Flag eval()."
    assert detect_natural_language_rules(_sources((".veritas/rules.md", ""))) is None
    assert detect_natural_language_rules(_sources((".veritas/rules.md", " \n\t"))) is None


def test_conventions_and_rules_are_redacted_before_the_model_sees_them():
    """T118, FR-013: built with fake_secret, so no secret-shaped literal is in this file."""
    token = fake_secret("ghp_", "r" * 36)
    sources = _sources(
        ("AGENTS.md", f"Deploy with {token}.\n"),
        (".veritas/rules.md", f"Never log {token}.\n"),
    )
    assert detect_conventions(sources) == "Deploy with [REDACTED]."
    assert detect_natural_language_rules(sources) == "Never log [REDACTED]."
    rendered = build_project_context(sources).render()
    assert token not in rendered
    assert rendered.count("[REDACTED]") == 2


def test_redaction_runs_before_the_4000_character_cut():
    """A token straddling the cut is masked whole: cutting first would leave a
    fragment too short to match the token pattern, and that fragment would leak."""
    token = fake_secret("ghp_", "s" * 36)
    text = "x" * 3989 + " " + token + "\n"
    rendered = build_project_context(_sources(("AGENTS.md", text))).render()
    assert token[:10] not in rendered
    assert rendered.endswith("x" * 3989 + " [REDACTED]")
    # The control: cutting first leaves "ghp_ssssss", which no pattern masks.
    assert redact_secrets(text[:4000]).endswith(token[:10])


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


def test_reader_keeps_searching_past_a_blank_conventions_file(tmp_path):
    """T118: whitespace-only text does not stop the search."""
    _write(tmp_path / "AGENTS.md", "  \n\t\n")
    _write(tmp_path / "CONVENTIONS.md", "")
    _write(tmp_path / "STYLE.md", "Style.\n")
    sources = _read_project_sources(str(tmp_path))
    assert sources.files == (("AGENTS.md", "  \n\t\n"), ("CONVENTIONS.md", ""), ("STYLE.md", "Style.\n"))
    assert sources.problems == ()


def test_reader_skips_a_file_over_200000_bytes_with_a_problem(tmp_path):
    """Exactly 200,000 bytes is read; one byte more is skipped and stated (T118)."""
    _write(tmp_path / "go.mod", "go 1.22\n" + "/" * (200_001 - 8))
    _write(tmp_path / "AGENTS.md", "a" * 200_000)
    sources = _read_project_sources(str(tmp_path))
    assert [name for name, _ in sources.files] == ["AGENTS.md"]
    assert sources.problems == ("go.mod over 200000 bytes, skipped",)


def test_reader_skips_a_pyproject_over_200000_bytes_with_a_problem(tmp_path):
    """T118: pyproject.toml is no longer exempt from the size limit."""
    text = '[project]\nrequires-python = ">=3.12"\n' + "#" * 210_000 + "\n"
    _write(tmp_path / "pyproject.toml", text)
    sources = _read_project_sources(str(tmp_path))
    assert sources.files == ()
    assert sources.problems == ("pyproject.toml over 200000 bytes, skipped",)


def test_reader_skips_a_pyproject_that_is_not_utf8_with_a_problem(tmp_path):
    """T118: was a UnicodeDecodeError that stopped the review; now a stated problem."""
    (tmp_path / "pyproject.toml").write_bytes(b'[project]\nname = "\xff"\n')
    _write(tmp_path / "go.mod", "go 1.22\n")
    sources = _read_project_sources(str(tmp_path))
    assert [name for name, _ in sources.files] == ["go.mod"]
    assert sources.problems == ("pyproject.toml is not valid UTF-8, skipped",)


@pytest.mark.parametrize(
    "name, method",
    [
        ("go.mod", "read_text"),
        ("AGENTS.md", "read_text"),
        (".veritas/rules.md", "read_text"),
        ("App.csproj", "read_text"),
        ("pyproject.toml", "read_bytes"),
    ],
)
def test_reader_skips_an_unreadable_file_with_a_problem(tmp_path, monkeypatch, name, method):
    """T118: an OSError while reading is stated, names the file, and the rest is still read."""
    _write(tmp_path / name, "content\n")
    _write(tmp_path / "Cargo.toml", 'edition = "2021"\n')
    original = getattr(Path, method)

    def failing(self, *args, **kwargs):
        if self == tmp_path / name:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)
    sources = _read_project_sources(str(tmp_path))
    assert [read for read, _ in sources.files] == ["Cargo.toml"]
    assert sources.problems == (f"{name} could not be read, skipped",)


def test_reader_states_a_stat_failure_as_unreadable(tmp_path, monkeypatch):
    """The size check's stat() failing is the same problem as a failed read."""
    _write(tmp_path / "go.mod", "go 1.22\n")
    target = tmp_path / "go.mod"
    original_stat, original_is_file = Path.stat, Path.is_file

    def failing_stat(self, *args, **kwargs):
        if self == target:
            raise OSError(5, "I/O error", str(self))
        return original_stat(self, *args, **kwargs)

    # is_file() may itself use stat(); keep it answering True so the reader gets that far.
    monkeypatch.setattr(Path, "is_file", lambda self: self == target or original_is_file(self))
    monkeypatch.setattr(Path, "stat", failing_stat)
    sources = _read_project_sources(str(tmp_path))
    assert sources.files == ()
    assert sources.problems == ("go.mod could not be read, skipped",)


def test_reader_keeps_searching_past_a_conventions_file_it_could_not_use(tmp_path):
    _write(tmp_path / "AGENTS.md", "a" * 200_001)
    _write(tmp_path / "CONVENTIONS.md", "Conventions.\n")
    sources = _read_project_sources(str(tmp_path))
    assert sources.files == (("CONVENTIONS.md", "Conventions.\n"),)
    assert sources.problems == ("AGENTS.md over 200000 bytes, skipped",)


def test_reader_problems_follow_read_order(tmp_path):
    (tmp_path / "pyproject.toml").write_bytes(b"\xff")
    _write(tmp_path / "package.json", "x" * 200_001)
    _write(tmp_path / "AGENTS.md", "a" * 200_001)
    _write(tmp_path / ".veritas" / "rules.md", "r" * 200_001)
    assert _read_project_sources(str(tmp_path)).problems == (
        "pyproject.toml is not valid UTF-8, skipped",
        "package.json over 200000 bytes, skipped",
        "AGENTS.md over 200000 bytes, skipped",
        ".veritas/rules.md over 200000 bytes, skipped",
    )


def test_reader_drops_a_utf8_bom_from_pyproject(tmp_path):
    """T118: tomllib rejects U+FEFF, so a BOM pyproject.toml (common from Windows
    editors) read as plain UTF-8 was "not valid TOML". Read as utf-8-sig it is detected."""
    (tmp_path / "pyproject.toml").write_bytes(b'\xef\xbb\xbf[project]\nrequires-python = ">=3.12"\n')
    sources = _read_project_sources(str(tmp_path))
    assert sources.text("pyproject.toml") == '[project]\nrequires-python = ">=3.12"\n'
    assert sources.problems == ()
    assert project_context_problems(sources) == []
    assert detect_language_versions(sources) == ["Python >=3.12"]


def test_reader_still_rejects_invalid_utf8_after_a_bom(tmp_path):
    (tmp_path / "pyproject.toml").write_bytes(b'\xef\xbb\xbf[project]\nname = "\xff"\n')
    sources = _read_project_sources(str(tmp_path))
    assert sources.files == ()
    assert sources.problems == ("pyproject.toml is not valid UTF-8, skipped",)


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


# --- the report states the project context (T117, FR-009) ----------------------

_PYPROJECT = '[project]\nrequires-python = ">=3.12"\n'


def test_contributing_sources_names_every_file_that_reached_the_context():
    sources = _sources(
        ("pyproject.toml", _PYPROJECT),
        ("App.csproj", "<TargetFramework>net8.0</TargetFramework>"),
        ("AGENTS.md", "Prefer early returns.\n"),
        (".veritas/rules.md", "Flag eval().\n"),
    )
    assert contributing_sources(sources) == [
        "pyproject.toml",
        "App.csproj",
        "AGENTS.md",
        ".veritas/rules.md",
    ]


def test_a_manifest_that_parsed_to_nothing_is_not_a_source():
    sources = _sources(
        ("pyproject.toml", '[project]\nname = "demo"\n'),
        ("package.json", '{"name": "demo"}'),
        ("go.mod", "module example.com/demo\n\ngo 1.22\n"),
    )
    assert contributing_sources(sources) == ["go.mod"]


def test_blank_conventions_and_rules_are_not_sources():
    # The reader stops at the first conventions file with any text; whitespace
    # counts as text there but renders nothing, so it is not a source.
    sources = _sources(("AGENTS.md", "  \n"), (".veritas/rules.md", "\n"))
    assert contributing_sources(sources) == []
    assert build_project_context(sources).is_empty


def test_only_the_conventions_file_that_was_used_is_a_source():
    sources = _sources(("AGENTS.md", "Use tabs.\n"), ("STYLE.md", "Use spaces.\n"))
    assert detect_conventions(sources) == "Use tabs."
    assert contributing_sources(sources) == ["AGENTS.md"]


def test_origin_that_is_the_working_directory_is_a_dot(tmp_path):
    assert display_origin(str(tmp_path), cwd=str(tmp_path)) == "."


def test_origin_below_the_working_directory_is_relative(tmp_path):
    origin = tmp_path / "src" / "pkg"
    assert display_origin(str(origin), cwd=str(tmp_path)) == "src/pkg"


def test_origin_outside_the_working_directory_is_its_name_only(tmp_path):
    origin = tmp_path / "elsewhere" / "project"
    cwd = tmp_path / "work"
    shown = display_origin(str(origin), cwd=str(cwd))
    assert shown == "project"
    assert str(tmp_path) not in shown


def test_relative_origin_is_resolved_against_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    assert display_origin("proj") == "proj"
    assert display_origin(".") == "."


def test_a_sibling_with_a_shared_prefix_is_outside(tmp_path):
    # "/x/work-other" starts with the text "/x/work" but is not below it.
    shown = display_origin(str(tmp_path / "work-other"), cwd=str(tmp_path / "work"))
    assert shown == "work-other"


def test_record_local_with_something_detected(tmp_path):
    sources = ProjectSources(
        origin=str(tmp_path),
        files=(("pyproject.toml", _PYPROJECT), ("AGENTS.md", "Prefer early returns.\n")),
    )
    record = project_context_record(ReviewScope.PROJECT, sources)
    assert record["project_context_status"] is ProjectContextStatus.DETECTED
    assert record["project_context_sources"] == ["pyproject.toml", "AGENTS.md"]
    assert record["project_context_reason"] is None


def test_record_local_with_nothing_detected(tmp_path):
    sources = ProjectSources(origin=str(tmp_path), files=(("package.json", "{}"),))
    record = project_context_record(ReviewScope.PROJECT, sources)
    assert record["project_context_status"] is ProjectContextStatus.UNAVAILABLE
    assert record["project_context_sources"] == []
    assert record["project_context_reason"] == "no manifest, conventions file or rules file found"


def test_record_problems_with_something_detected_is_partial(tmp_path):
    sources = ProjectSources(
        origin=str(tmp_path),
        files=(("pyproject.toml", _PYPROJECT),),
        problems=("AGENTS.md over 200000 bytes, skipped", "package.json unreadable"),
    )
    record = project_context_record(ReviewScope.PROJECT, sources)
    assert record["project_context_status"] is ProjectContextStatus.PARTIAL
    assert record["project_context_sources"] == ["pyproject.toml"]
    assert record["project_context_reason"] == (
        "AGENTS.md over 200000 bytes, skipped; package.json unreadable"
    )


def test_record_problems_with_nothing_detected_is_unavailable_with_the_problems(tmp_path):
    sources = ProjectSources(
        origin=str(tmp_path), files=(), problems=("pyproject.toml is not valid UTF-8",)
    )
    record = project_context_record(ReviewScope.PROJECT, sources)
    assert record["project_context_status"] is ProjectContextStatus.UNAVAILABLE
    assert record["project_context_reason"] == "pyproject.toml is not valid UTF-8"


def test_record_pr_review_is_unavailable_not_read_yet():
    record = project_context_record(ReviewScope.PR, None)
    assert record == {
        "project_context_status": ProjectContextStatus.UNAVAILABLE,
        "project_context_sources": [],
        "project_context_reason": "not read for PR reviews yet",
        "project_context_origin": None,
    }


# --- problems the report states (T118) ---------------------------------------------


def test_no_problems_for_good_sources():
    sources = _sources(("pyproject.toml", _PYPROJECT), ("AGENTS.md", "Short.\n"))
    assert project_context_problems(sources) == []


def test_invalid_toml_is_a_problem():
    assert project_context_problems(_sources(("pyproject.toml", "[project\n"))) == [
        "pyproject.toml is not valid TOML"
    ]


def test_valid_toml_that_declares_nothing_is_not_a_problem():
    assert project_context_problems(_sources(("pyproject.toml", '[tool.x]\ny = 1\n'))) == []


def test_regex_manifests_that_yield_nothing_are_not_problems():
    sources = _sources(
        ("pom.xml", "<project/>"),
        ("build.gradle", "plugins {}"),
        ("package.json", "{not json at all"),
        ("go.mod", "module x\n"),
        ("Cargo.toml", "[package\n"),
        ("App.csproj", "<Project/>"),
    )
    assert project_context_problems(sources) == []


def test_truncation_is_stated_only_above_4000_characters():
    at_limit = _sources(("STYLE.md", "s" * 4000), (".veritas/rules.md", "r" * 4000))
    assert project_context_problems(at_limit) == []
    over = _sources(("STYLE.md", "s" * 4001), (".veritas/rules.md", "r" * 4001))
    assert project_context_problems(over) == [
        "STYLE.md truncated to 4000 characters",
        ".veritas/rules.md truncated to 4000 characters",
    ]


def test_truncation_counts_the_stripped_text_the_model_would_get():
    """Surrounding whitespace is stripped before the cut, so it never causes a truncation."""
    sources = _sources(("AGENTS.md", "\n\n" + "a" * 4000 + "\n\n   "))
    assert project_context_problems(sources) == []
    assert build_project_context(sources).render().endswith("a" * 4000)


def test_truncation_does_not_change_what_the_model_receives():
    text = "".join(f"Rule {i:04d}: keep functions short.\n" for i in range(200))
    rendered = build_project_context(_sources(("AGENTS.md", text))).render()
    assert rendered == "Project conventions to honor:\n" + text.strip()[:4000]


def test_problems_order_reading_then_parsing_then_truncation():
    sources = ProjectSources(
        origin="/project",
        files=(("pyproject.toml", "[project\n"), ("AGENTS.md", "a" * 4001)),
        problems=("go.mod could not be read, skipped",),
    )
    assert project_context_problems(sources) == [
        "go.mod could not be read, skipped",
        "pyproject.toml is not valid TOML",
        "AGENTS.md truncated to 4000 characters",
    ]


def test_record_invalid_toml_and_nothing_else_is_unavailable(tmp_path):
    sources = ProjectSources(origin=str(tmp_path), files=(("pyproject.toml", "[project\n"),))
    record = project_context_record(ReviewScope.PROJECT, sources)
    assert record["project_context_status"] is ProjectContextStatus.UNAVAILABLE
    assert record["project_context_sources"] == []
    assert record["project_context_reason"] == "pyproject.toml is not valid TOML"


def test_record_truncation_is_partial_with_the_truncated_file_still_a_source(tmp_path):
    sources = ProjectSources(origin=str(tmp_path), files=(("AGENTS.md", "a" * 4001),))
    record = project_context_record(ReviewScope.PROJECT, sources)
    assert record["project_context_status"] is ProjectContextStatus.PARTIAL
    assert record["project_context_sources"] == ["AGENTS.md"]
    assert record["project_context_reason"] == "AGENTS.md truncated to 4000 characters"


def _run_context_node(sources: ProjectSources) -> tuple[dict, list[str]]:
    run = ReviewRun(
        scope=ReviewScope.PROJECT,
        target="/project",
        config_hash="h",
        model_name="m",
        prompt_version="1.7.0",
        started_at=datetime.now(),
    )
    state = {"scope": ReviewScope.PROJECT, "target": "/project", "run": run, "project_sources": sources}
    stream = io.StringIO()
    result = make_context_node(ReviewContext(llm=None, log=Log(stream=stream)))(state)
    return result, stream.getvalue().splitlines()


def test_context_node_logs_each_problem_as_one_warn_line():
    sources = ProjectSources(
        origin="/project",
        files=(("go.mod", "go 1.22\n"), ("pyproject.toml", "[project\n"), ("AGENTS.md", "a" * 4001)),
        problems=("package.json over 200000 bytes, skipped",),
    )
    _result, log = _run_context_node(sources)
    assert log == [
        "[info] Detected: Go 1.22",
        "[warn] Project context: package.json over 200000 bytes, skipped",
        "[warn] Project context: pyproject.toml is not valid TOML",
        "[warn] Project context: AGENTS.md truncated to 4000 characters",
    ]


def test_context_node_logs_no_warning_without_problems():
    _result, log = _run_context_node(_sources(("go.mod", "go 1.22\n")))
    assert log == ["[info] Detected: Go 1.22"]
