"""Integration tests — OpenGrep integration (T032, FR-003/FR-012)."""

import json
import shutil
from pathlib import Path

import pytest

from tests.conftest import default_fake_llm, fake_secret

from veritas.config.constants import LAST_REPORT_JSON
from veritas.models.entities import Report, ReviewScope, SastStatus
from veritas.review.graph import run_review
from veritas.security.opengrep import (
    OPENGREP_NO_JSON,
    OPENGREP_SCAN_FAILED,
    OPENGREP_NOT_FOUND,
    SAST_NO_FILES,
    OpengrepResult,
    collect_sast,
    result_to_finding,
    run_opengrep,
)


def test_missing_binary_degrades_gracefully(monkeypatch):
    monkeypatch.setattr("veritas.security.opengrep.shutil.which", lambda _bin: None)
    result = run_opengrep("some/dir")
    assert result.degraded == OPENGREP_NOT_FOUND
    assert result.findings == []


@pytest.fixture
def fake_opengrep(monkeypatch):
    """Stub the subprocess so a run produces a known JSON output file."""
    state = {"saw_cmd": None, "saw_kwargs": None}

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        state["saw_cmd"] = cmd
        state["saw_kwargs"] = kwargs
        out_json = cmd[-2]
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "results": [
                        {
                            "path": "src/app.py",
                            "check_id": "python.lang.security.insecure-shell",
                            "start": {"line": 3, "col": 1},
                            "end": {"line": 3, "col": 40},
                            "extra": {
                                "severity": "ERROR",
                                "message": "subprocess shell=True detected",
                                "metadata": {"cwe": ["CWE-78"]},
                                "lines": "subprocess.call(cmd, shell=True)",
                            },
                        },
                        {
                            "path": "legacy/x.cob",
                            "check_id": "some.rule",
                            "start": {"line": 1, "col": 1},
                            "end": {"line": 1, "col": 5},
                            "extra": {"severity": "WARNING", "message": "cobol rule"},
                        },
                    ]
                },
                fh,
            )
        return FakeProc()

    monkeypatch.setattr("veritas.security.opengrep.shutil.which", lambda _bin: "/fake/opengrep")
    monkeypatch.setattr("veritas.security.opengrep.subprocess.run", fake_run)
    return state


def test_run_parses_and_filters(monkeypatch, tmp_path, fake_opengrep):
    result = run_opengrep(str(tmp_path), rules="p/owasp-top-ten")
    assert result.degraded is None
    assert result.rules == "p/owasp-top-ten"
    assert len(result.findings) == 1
    assert result.findings[0]["path"] == "src/app.py"


def test_run_invokes_expected_args(monkeypatch, tmp_path, fake_opengrep):
    run_opengrep(str(tmp_path))
    cmd = fake_opengrep["saw_cmd"]
    assert cmd[0] == "opengrep"
    assert cmd[1:4] == ["scan", "--config", "p/owasp-top-ten"]
    assert cmd[-1] == str(tmp_path)  # trailing positional target dir
    assert cmd[-2].startswith(f"{tmp_path}/.opengrep-results-")


def test_run_decodes_output_as_utf8_with_replacement(tmp_path, fake_opengrep):
    """UTF-8 with replacement, not the locale codec (Windows box-drawing bytes)."""
    run_opengrep(str(tmp_path))
    kwargs = fake_opengrep["saw_kwargs"]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert "text" not in kwargs


def test_timeout_degrades(monkeypatch):
    import subprocess

    monkeypatch.setattr("veritas.security.opengrep.shutil.which", lambda _bin: "/fake/opengrep")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr("veritas.security.opengrep.subprocess.run", boom)
    result = run_opengrep("x")
    assert "timed out" in (result.degraded or "")


def test_result_to_finding_mapping():
    raw = {
        "path": "src/app.py",
        "check_id": "py.security",
        "start": {"line": 4, "col": 1},
        "end": {"line": 4, "col": 80},
        "extra": {
            "severity": "ERROR",
            "message": "Use of eval() on untrusted input",
            "metadata": {"owasp": ["A03:2021"]},
            "lines": "eval(user_input)",
        },
    }
    finding = result_to_finding(raw)
    assert finding["source"] == "sast"
    assert finding["severity"] == "error"
    assert finding["category"] == "security"
    assert finding["cwe_id"] is None
    assert finding["owasp_id"] == "A03:2021"
    assert finding["file"] == "src/app.py"
    assert finding["confidence"] == 1.0


# --- failure output: one line of at most 200 chars, never a traceback (FR-012) ---

_TRACEBACK_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "opengrep/_main.py", line 88, in main\n'
    "    raise ConfigNotFound(pack)\n"
    "RuntimeError: rules pack not found: p/nope\n"
    "\n"
)


@pytest.fixture
def stubbed_opengrep(monkeypatch):
    """Replace shutil.which + subprocess.run with a configurable fake scan."""
    state = {"returncode": 3, "stdout": "", "stderr": ""}

    class FakeProc:
        def __init__(self):
            self.returncode = state["returncode"]
            self.stdout = state["stdout"]
            self.stderr = state["stderr"]

    monkeypatch.setattr(
        "veritas.security.opengrep.shutil.which", lambda _bin: "/fake/opengrep"
    )
    monkeypatch.setattr(
        "veritas.security.opengrep.subprocess.run",
        lambda *args, **kwargs: FakeProc(),
    )
    return state


def _assert_one_line(reason: str | None) -> str:
    assert reason, "a failed scan must report a reason"
    assert "\n" not in reason
    assert len(reason) <= 200
    assert "Traceback" not in reason
    return reason


def test_multiline_stderr_yields_one_line_reason_without_traceback(
    stubbed_opengrep, tmp_path
):
    stubbed_opengrep["stderr"] = _TRACEBACK_STDERR
    result = run_opengrep(str(tmp_path))
    reason = _assert_one_line(result.degraded)
    assert reason == "RuntimeError: rules pack not found: p/nope"
    # the logged detail is that same single line
    assert result.scan_error == reason


def test_failure_detail_is_redacted_and_capped(stubbed_opengrep, tmp_path):
    token = fake_secret("ghp_", "a" * 36)
    stubbed_opengrep["stderr"] = "Traceback (most recent call last):\n  ...\n"
    stubbed_opengrep["stderr"] += "x" * 400 + "\n"
    result = run_opengrep(str(tmp_path))
    reason = _assert_one_line(result.degraded)
    assert reason == "x" * 200  # cut to the cap, still one line

    # the redaction applies to the exception line a traceback ends with too
    stubbed_opengrep["stderr"] = (
        "Traceback (most recent call last):\n"
        f"RuntimeError: request failed for {token}\n"
    )
    result = run_opengrep(str(tmp_path))
    reason = _assert_one_line(result.degraded)
    assert token not in reason
    assert "[REDACTED]" in reason


def test_failure_without_stderr_keeps_the_category_message(stubbed_opengrep, tmp_path):
    stubbed_opengrep["stderr"] = ""
    stubbed_opengrep["stdout"] = ""
    result = run_opengrep(str(tmp_path))
    assert result.degraded == OPENGREP_SCAN_FAILED
    assert result.scan_error is None


def test_no_parseable_json_yields_one_line_reason(stubbed_opengrep, tmp_path):
    stubbed_opengrep["returncode"] = 0  # the output file is never written
    stubbed_opengrep["stderr"] = _TRACEBACK_STDERR
    result = run_opengrep(str(tmp_path))
    reason = _assert_one_line(result.degraded)
    assert reason == "RuntimeError: rules pack not found: p/nope"
    assert result.scan_error == reason


def test_no_parseable_json_without_stderr_keeps_the_category_message(
    stubbed_opengrep, tmp_path
):
    stubbed_opengrep["returncode"] = 0
    stubbed_opengrep["stderr"] = ""
    result = run_opengrep(str(tmp_path))
    assert result.degraded == OPENGREP_NO_JSON
    assert result.scan_error is None


def test_missing_binary_reason_is_one_line(monkeypatch, tmp_path):
    monkeypatch.setattr("veritas.security.opengrep.shutil.which", lambda _bin: None)
    result = run_opengrep(str(tmp_path))
    assert _assert_one_line(result.degraded) == OPENGREP_NOT_FOUND


# --- the configured rules source reaches the runner and the report (FR-012) ---


def test_runner_passes_configured_rules_after_config(sample_project, tmp_path, monkeypatch):
    """`--config` -> Settings.opengrep_rules -> Runtime -> collect_sast(rules=...)."""
    from typer.testing import CliRunner

    import veritas.cli.app as cli_app
    from veritas.review.nodes import scope as scope_module

    seen: dict = {}

    def stub_collect_sast(files, **kwargs):
        seen["rules"] = kwargs.get("rules")
        return OpengrepResult(findings=[], rules=kwargs.get("rules") or "p/owasp-top-ten")

    monkeypatch.setattr(scope_module, "collect_sast", stub_collect_sast)
    config = tmp_path / "sast-config.toml"
    config.write_text('[security]\nopengrep_rules = "r/corp-pack"\n', encoding="utf-8")

    def fake_run_review(_settings, scope_val, target, **kwargs):
        return run_review(
            _settings, scope_val, target, llm=default_fake_llm(), **kwargs
        )

    monkeypatch.setattr(cli_app, "run_review", fake_run_review)

    result = CliRunner().invoke(
        cli_app.app,
        [
            "review",
            "--scope",
            "project",
            "--target",
            str(sample_project),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert seen["rules"] == "r/corp-pack"


def test_report_records_sast_rules_and_renders_the_line(
    monkeypatch, sample_project, settings
):
    from veritas.review.nodes import scope as scope_module

    monkeypatch.setattr(
        scope_module,
        "collect_sast",
        lambda *_args, **kwargs: OpengrepResult(findings=[], ran=True, rules="r/corp-pack"),
    )
    outcome = run_review(
        settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm()
    )
    assert outcome.exit_code == 0

    report = Report.model_validate_json(
        Path(LAST_REPORT_JSON).read_text(encoding="utf-8")
    )
    assert report.run.sast_rules == "r/corp-pack"
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert "- **SAST**: ran with `r/corp-pack` — 0 result(s)" in md.splitlines()
    assert "SAST rules" not in md


@pytest.mark.skipif(
    shutil.which("opengrep") is None, reason="opengrep binary not on PATH"
)
def test_real_opengrep_scans_a_local_rules_file(tmp_path):
    """A local rules file is a working, reproducible SAST rules source.

    One rule matching os.system(...) over a source file that calls it, so the
    scan's ground truth is exactly one finding at a known line (FR-012).
    """
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "rules:\n"
        "  - id: veritas.test.os-system\n"
        "    languages: [python]\n"
        "    severity: ERROR\n"
        "    message: os.system() call\n"
        "    pattern: os.system(...)\n",
        encoding="utf-8",
    )
    files = {
        "src/target.py": (
            "import os\n"
            "\n"
            "\n"
            "def run():\n"
            "    os.system(input())\n"
        )
    }

    result = collect_sast(files, scope_value="project", rules=str(rules))

    assert result.ran is True
    assert result.degraded is None
    assert result.rules == str(rules)
    assert len(result.findings) == 1
    finding = result_to_finding(result.findings[0])
    assert finding["source"] == "sast"
    assert finding["file"] == "src/target.py"
    assert finding["start_line"] == 5


# --- result paths map back to scoped keys on Windows, never silently dropped (FR-012) ---


@pytest.fixture
def fake_scan(monkeypatch):
    """Stub a scan whose results carry the path strings the caller chooses."""
    state: dict = {"paths": []}

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        out_json = cmd[-2]
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "results": [
                        {
                            "path": path,
                            "check_id": "veritas.test.rule",
                            "start": {"line": 5, "col": 1},
                            "end": {"line": 5, "col": 20},
                            "extra": {"severity": "ERROR", "message": "flagged"},
                        }
                        for path in state["paths"]
                    ]
                },
                fh,
            )
        return FakeProc()

    monkeypatch.setattr("veritas.security.opengrep.shutil.which", lambda _bin: "/fake/opengrep")
    monkeypatch.setattr("veritas.security.opengrep.subprocess.run", fake_run)
    return state


def test_short_name_backslash_result_path_maps_by_suffix(fake_scan):
    """An 8.3 short-name prefix (spelled differently from tmpdir) still maps."""
    fake_scan["paths"] = [
        r"C:\Users\SHASHA~1\AppData\Local\Temp\veritas-sast-short\src\target.py"
    ]
    result = collect_sast({"src/target.py": "import os\n"}, scope_value="project")
    assert [f["path"] for f in result.findings] == ["src/target.py"]
    assert result.degraded is None


def test_forward_slash_relative_result_path_maps(fake_scan):
    fake_scan["paths"] = ["src/target.py"]
    result = collect_sast({"src/target.py": "import os\n"}, scope_value="project")
    assert [f["path"] for f in result.findings] == ["src/target.py"]
    assert result.degraded is None


def test_longest_matching_scoped_key_wins(fake_scan):
    fake_scan["paths"] = [r"C:\work\repo\src\a\target.py"]
    files = {"a/target.py": "x = 1\n", "src/a/target.py": "x = 1\n"}
    result = collect_sast(files, scope_value="project")
    assert [f["path"] for f in result.findings] == ["src/a/target.py"]


def test_unmapped_result_is_counted_logged_and_reported(fake_scan, capsys):
    fake_scan["paths"] = [r"C:\elsewhere\unrelated\main.py"]
    result = collect_sast({"src/target.py": "import os\n"}, scope_value="project")
    assert result.findings == []
    assert "1 result(s) unmapped" in (result.degraded or "")
    captured = capsys.readouterr()
    assert "[warn] sast: 1 result(s) could not be mapped to reviewed files" in captured.err


def test_unmapped_result_appends_to_an_existing_degraded_reason(monkeypatch):
    monkeypatch.setattr("veritas.security.opengrep.shutil.which", lambda _bin: "/fake/opengrep")
    monkeypatch.setattr(
        "veritas.security.opengrep.run_opengrep",
        lambda *_args, **_kwargs: OpengrepResult(
            findings=[
                {
                    "path": r"C:\elsewhere\unrelated\main.py",
                    "check_id": "veritas.test.rule",
                    "start": {"line": 1, "col": 1},
                    "end": {"line": 1, "col": 2},
                    "extra": {"severity": "ERROR", "message": "flagged"},
                }
            ],
            degraded=OPENGREP_SCAN_FAILED,
        ),
    )
    result = collect_sast({"src/target.py": "import os\n"}, scope_value="project")
    assert result.findings == []
    assert result.degraded == f"{OPENGREP_SCAN_FAILED}; 1 result(s) unmapped"


# --- the report and stdout state whether SAST ran, and why not (T109, FR-012) ---


def _use_real_collect_sast(monkeypatch) -> None:
    """Undo the conftest stub so the run goes through collect_sast itself."""
    monkeypatch.setattr("veritas.review.nodes.scope.collect_sast", collect_sast)


def _review_outputs(outcome, capsys) -> tuple[Report, list[str], list[str]]:
    """(last-report.json, Markdown report lines, compact stdout lines) of a run."""
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    return report, md.splitlines(), capsys.readouterr().out.splitlines()


def test_opengrep_missing_from_path_is_reported_as_not_run(
    monkeypatch, sample_project, settings, capsys
):
    _use_real_collect_sast(monkeypatch)
    monkeypatch.setattr("veritas.security.opengrep.shutil.which", lambda _bin: None)

    outcome = run_review(
        settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm()
    )
    report, md_lines, stdout_lines = _review_outputs(outcome, capsys)

    assert outcome.exit_code == 0
    assert report.run.sast_status is SastStatus.NOT_RUN
    assert report.run.sast_result_count is None
    assert report.run.sast_reason == OPENGREP_NOT_FOUND
    assert report.run.sast_rules == "p/owasp-top-ten"
    assert (
        "- **SAST**: not run — OpenGrep not found on PATH "
        "(rules configured: `p/owasp-top-ten`)"
    ) in md_lines
    assert "SAST: not run — OpenGrep not found on PATH" in stdout_lines


def test_a_clean_scan_with_zero_results_is_reported_as_ran(
    monkeypatch, fake_scan, sample_project, settings, capsys
):
    _use_real_collect_sast(monkeypatch)
    fake_scan["paths"] = []

    outcome = run_review(
        settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm()
    )
    report, md_lines, stdout_lines = _review_outputs(outcome, capsys)

    assert report.run.sast_status is SastStatus.RAN
    assert report.run.sast_result_count == 0
    assert report.run.sast_reason is None
    assert "- **SAST**: ran with `p/owasp-top-ten` — 0 result(s)" in md_lines
    assert "SAST: ran (0 result(s))" in stdout_lines


def test_a_scan_with_unmapped_results_is_reported_as_ran_with_the_reason(
    monkeypatch, fake_scan, sample_project, settings, capsys
):
    _use_real_collect_sast(monkeypatch)
    fake_scan["paths"] = ["src/app.py", r"C:\elsewhere\unrelated\main.py"]

    outcome = run_review(
        settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm()
    )
    report, md_lines, stdout_lines = _review_outputs(outcome, capsys)

    assert report.run.sast_status is SastStatus.RAN
    assert report.run.sast_result_count == 1
    assert report.run.sast_reason == "1 result(s) unmapped"
    assert (
        "- **SAST**: ran with `p/owasp-top-ten` — 1 result(s); 1 result(s) unmapped"
    ) in md_lines
    assert "SAST: ran (1 result(s)) — 1 result(s) unmapped" in stdout_lines


def test_collect_sast_with_no_files_does_not_run():
    result = collect_sast({}, scope_value="project")
    assert result.ran is False
    assert result.degraded == SAST_NO_FILES == "no files to scan"


@pytest.mark.skipif(
    shutil.which("opengrep") is None, reason="opengrep binary not on PATH"
)
def test_real_opengrep_review_reports_ran_with_one_result(
    monkeypatch, tmp_path, settings, capsys
):
    """The real binary end to end: one rule, one matching call, one result."""
    _use_real_collect_sast(monkeypatch)
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "rules:\n"
        "  - id: veritas.test.os-system\n"
        "    languages: [python]\n"
        "    severity: ERROR\n"
        "    message: os.system() call\n"
        "    pattern: os.system(...)\n",
        encoding="utf-8",
    )
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "target.py").write_text(
        "import os\n\n\ndef run():\n    os.system(input())\n", encoding="utf-8"
    )
    configured = settings.model_copy(update={"opengrep_rules": str(rules)})

    outcome = run_review(configured, ReviewScope.PROJECT, str(project), llm=default_fake_llm())
    report, md_lines, stdout_lines = _review_outputs(outcome, capsys)

    assert report.run.sast_status is SastStatus.RAN
    assert report.run.sast_result_count == 1
    assert report.run.sast_reason is None
    assert f"- **SAST**: ran with `{rules}` — 1 result(s)" in md_lines
    assert "SAST: ran (1 result(s))" in stdout_lines