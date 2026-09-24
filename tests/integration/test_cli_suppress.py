"""Integration tests — suppression flow (FR-017/FR-018, T048)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import default_fake_llm

from veritas.cli.app import app
from veritas.config.constants import LAST_REPORT_JSON, SUPPRESSIONS_PATH
from veritas.config.settings import Settings
from veritas.models.entities import Report, ReviewScope
from veritas.review.graph import run_review
from veritas.suppression.store import SuppressionStore


def _produce_report(sample_project, settings) -> Report:
    run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm())
    return Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))


def test_suppress_by_id(sample_project, settings):
    report = _produce_report(sample_project, settings)
    target = report.code_findings[0]

    runner = CliRunner()
    result = runner.invoke(app, ["suppress", "--finding-id", target.id, "--reason", "false positive"])
    assert result.exit_code == 0
    assert "Suppressed. Fingerprint:" in result.stdout

    entries = SuppressionStore().load()
    assert [e.file for e in entries] == [target.file]
    assert entries[0].reason == "false positive"


def test_suppress_then_rerun_excludes_finding(sample_project, settings):
    report = _produce_report(sample_project, settings)
    target = report.code_findings[0]
    CliRunner().invoke(app, ["suppress", "--finding-id", target.id, "--reason", "false positive"])

    report2 = _produce_report(sample_project, settings)
    assert all(f.id != target.id for f in report2.code_findings)
    assert not any(f.is_suppressed for f in report2.code_findings if True)


def test_suppressed_finding_absent_from_both_report_views(sample_project, settings, capsys):
    """T050: a suppressed finding is absent from BOTH the Markdown report file
    and the compact stdout summary (render-time matching, FR-017)."""
    report = _produce_report(sample_project, settings)
    target = next(f for f in report.code_findings if f.title == "Broken access control")
    CliRunner().invoke(app, ["suppress", "--finding-id", target.id])
    capsys.readouterr()  # drain the first run's stdout before the suppressed run

    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm())
    captured = capsys.readouterr()
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")

    assert target.title not in md
    assert target.cited_snippet not in md
    assert target.title not in captured.out
    assert target.cited_snippet not in captured.out


def test_suppress_by_file_and_line(sample_project, settings):
    report = _produce_report(sample_project, settings)
    target = report.code_findings[0]

    runner = CliRunner()
    result = runner.invoke(
        app, ["suppress", "--file", target.file, "--line", str(target.line_range.start_line)]
    )
    assert result.exit_code == 0


def test_suppress_no_match_exits_1(sample_project, settings):
    _produce_report(sample_project, settings)
    runner = CliRunner()
    result = runner.invoke(app, ["suppress", "--finding-id", "definitely-not-there"])
    assert result.exit_code == 1
    assert "No finding matched" in result.stderr


def test_suppress_file_without_line_is_usage_error(sample_project, settings):
    _produce_report(sample_project, settings)
    runner = CliRunner()
    result = runner.invoke(app, ["suppress", "--file", "src/app.py"])
    assert result.exit_code == 1
    assert "--line" in result.stderr  # file without line is a usage error


def test_suppress_without_prior_review_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["suppress", "--finding-id", "abc"])
    assert result.exit_code == 1
    assert "No previous review" in result.stderr


def test_unsuppress_removes_entry(sample_project, settings):
    report = _produce_report(sample_project, settings)
    target = report.code_findings[0]
    CliRunner().invoke(app, ["suppress", "--finding-id", target.id])
    fingerprint = SuppressionStore().load()[0].fingerprint

    result = CliRunner().invoke(app, ["unsuppress", "--fingerprint", fingerprint])
    assert result.exit_code == 0
    assert "Un-suppressed" in result.stdout
    assert SuppressionStore().load() == []


def test_unsuppress_unknown_fingerprint_exits_1(sample_project, monkeypatch):
    runner = CliRunner()
    result = runner.invoke(app, ["unsuppress", "--fingerprint", "0" * 64])
    assert result.exit_code == 1
    assert "No matching suppression entry" in result.stderr