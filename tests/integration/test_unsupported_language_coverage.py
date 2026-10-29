"""Quickstart Scenario 12 end to end: unsupported-language files (T130, FR-022).

A project holding ``a.cob``, ``b.lua`` and ``c.py`` is reviewed through
``run_review`` with a fake LLM. ``c.py`` is reviewed; the other two are not
code-reviewed and are listed in the Coverage section and counted in the
compact coverage line.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import FakeLLM
from veritas.config.constants import LAST_REPORT_JSON
from veritas.models.entities import Report, ReviewScope
from veritas.review.graph import run_review


def test_scenario_12_unsupported_files_are_listed_and_counted(tmp_path, settings, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.cob").write_text("IDENTIFICATION DIVISION.\n", encoding="utf-8")
    (project / "b.lua").write_text("print('hi')\n", encoding="utf-8")
    (project / "c.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    llm = FakeLLM({})
    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=llm)
    stdout = capsys.readouterr().out.splitlines()

    assert outcome.exit_code == 0
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert "| Files skipped (unsupported language) | 2 |" in md
    # The list keeps directory-walk order, so the paths are checked one by one.
    block = md.split("<summary>Unsupported-language files (2)</summary>\n", 1)[1]
    block = block.split("</details>", 1)[0]
    assert sorted(line for line in block.splitlines() if line) == ["- `a.cob`", "- `b.lua`"]
    assert "| Files reviewed | 1 |" in md

    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert report.coverage.reviewed_files == ["c.py"]
    assert sorted(report.coverage.unsupported_files) == ["a.cob", "b.lua"]
    assert report.coverage.excluded_files == []
    # c.py reached the model; the unsupported files never did.
    prompts = [system + user for system, user in llm.calls]
    assert any("c.py" in prompt for prompt in prompts)
    assert not any("a.cob" in prompt or "b.lua" in prompt for prompt in prompts)

    assert [line for line in stdout if line.startswith("Coverage:")] == [
        "Coverage: 1 reviewed (0 split), 0 excluded, 2 unsupported, 0 not reviewed; 1/8 batches"
    ]


def test_a_second_review_does_not_list_the_first_runs_own_files(tmp_path, settings, capsys):
    """T130: the project is the working directory, so the first run writes
    veritas-report-<timestamp>.md and .veritas/last-report.json into it; the
    second run walks past both instead of listing them as unsupported."""
    (tmp_path / "a.cob").write_text("IDENTIFICATION DIVISION.\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    first = run_review(settings, ReviewScope.PROJECT, str(tmp_path), llm=FakeLLM({}))
    assert Path(str(first.report_path)).name.startswith("veritas-report-")
    assert (tmp_path / LAST_REPORT_JSON).is_file()

    second = run_review(settings, ReviewScope.PROJECT, str(tmp_path), llm=FakeLLM({}))
    capsys.readouterr()
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert second.exit_code == 0
    assert report.coverage.unsupported_files == ["a.cob"]
    assert report.coverage.reviewed_files == ["c.py"]
    assert report.coverage.excluded_files == []


def test_project_context_still_reads_veritas_rules_when_the_walk_skips_veritas(
    tmp_path, settings, capsys
):
    """T130: the walk skips .veritas/, but project context reads
    .veritas/rules.md directly, and none of .veritas/ is listed in Coverage."""
    project = tmp_path / "proj"
    (project / ".veritas").mkdir(parents=True)
    (project / ".veritas" / "rules.md").write_text("- Prefer pure functions.\n", encoding="utf-8")
    (project / ".veritas" / "config.toml").write_text("[review]\n", encoding="utf-8")
    (project / "c.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=FakeLLM({}))
    capsys.readouterr()
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))

    assert outcome.exit_code == 0
    assert ".veritas/rules.md" in report.run.project_context_sources
    assert report.coverage.unsupported_files == []
    assert report.coverage.reviewed_files == ["c.py"]


def test_veritas_config_still_marks_the_project_root(tmp_path, settings, capsys):
    """T130: .veritas/config.toml still marks the project root for the
    project-context search (T119): a module review below it reads the root's
    .veritas/rules.md."""
    project = tmp_path / "proj"
    (project / ".veritas").mkdir(parents=True)
    (project / ".veritas" / "rules.md").write_text("- Prefer pure functions.\n", encoding="utf-8")
    (project / ".veritas" / "config.toml").write_text("[review]\n", encoding="utf-8")
    (project / "pkg").mkdir()
    (project / "pkg" / "c.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    outcome = run_review(settings, ReviewScope.MODULE, str(project / "pkg"), llm=FakeLLM({}))
    capsys.readouterr()
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))

    assert outcome.exit_code == 0
    assert report.run.project_context_origin == "proj"
    assert ".veritas/rules.md" in report.run.project_context_sources
    assert report.coverage.reviewed_files == ["c.py"]
