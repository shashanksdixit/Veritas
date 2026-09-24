"""Integration tests — OpenGrep integration (T032, FR-003/FR-012)."""

import json

import pytest

from veritas.security.opengrep import (
    OPENGPRE_NOT_FOUND,
    OpengrepResult,
    result_to_finding,
    run_opengrep,
)


def test_missing_binary_degrades_gracefully(monkeypatch):
    monkeypatch.setattr("veritas.security.opengrep.shutil.which", lambda _bin: None)
    result = run_opengrep("some/dir")
    assert result.degraded == OPENGPRE_NOT_FOUND
    assert result.findings == []


@pytest.fixture
def fake_opengrep(monkeypatch):
    """Stub the subprocess so a run produces a known JSON output file."""
    state = {"saw_cmd": None}

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output=True, text=True, timeout=120):
        state["saw_cmd"] = cmd
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