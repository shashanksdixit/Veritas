"""Integration test scaffolding: no real SAST binary, sandboxed CWD."""

import pytest

from veritas.security.opengrep import OpengrepResult


@pytest.fixture(autouse=True)
def _no_real_sast(monkeypatch):
    """SAST is grounded by real OpenGrep; without the binary every run would
    degrade. Tests stub it out with a clean scan that ran and found nothing,
    and assert determinism via injected results."""

    def _stub(files, *, scope_value, rules=None, opengrep_bin="opengrep"):
        return OpengrepResult(findings=[], ran=True, rules=rules or "p/owasp-top-ten")

    monkeypatch.setattr("veritas.review.nodes.scope.collect_sast", _stub)


@pytest.fixture(autouse=True)
def _sandbox_cwd(monkeypatch, tmp_path):
    """Run every integration test from a throwaway CWD so the report file,
    .veritas/last-report.json, and .veritas/suppressions.json never touch the
    real repository."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".veritas").mkdir(exist_ok=True)