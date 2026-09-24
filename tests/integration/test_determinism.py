"""Integration tests — run-level determinism (T022a/T026, SC-004).

The output-schema version tripwire lives in ``test_schema_version.py`` (T022b).
"""

from pathlib import Path

from tests.conftest import default_fake_llm

from veritas.models.entities import ReviewScope
from veritas.review.graph import run_review
from veritas.suppression.fingerprint import compute_fingerprint
from veritas.security.opengrep import OpengrepResult


def _fixed_sast(files, *, scope_value, rules=None, opengrep_bin="opengrep"):
    raw = [
        {
            "path": "src/app.py",
            "check_id": "py.security.snapshot",
            "start": {"line": 3, "col": 1},
            "end": {"line": 3, "col": 12},
            "extra": {
                "severity": "ERROR",
                "message": "Hardcoded secret-like constant",
                "metadata": {"cwe": ["CWE-798"]},
                "lines": "secret = 42",
            },
        }
    ]
    return OpengrepResult(findings=raw, rules="p/owasp-top-ten")


def test_two_runs_identical_report(sample_project, settings, monkeypatch):
    from veritas.review.nodes import scope as scope_module

    monkeypatch.setattr(scope_module, "collect_sast", _fixed_sast)

    llm = default_fake_llm()
    out1 = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    md1 = Path(str(out1.report_path)).read_text(encoding="utf-8")
    out2 = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm())
    md2 = Path(str(out2.report_path)).read_text(encoding="utf-8")

    # SC-004: identical SAST findings, requirement evidence, and fingerprints.
    # Narrative descriptions are also deterministic here because responses are canned.
    assert md1 == md2


def test_sast_finding_grounded_and_deterministic(sample_project, settings, monkeypatch):
    from veritas.review.nodes import scope as scope_module

    monkeypatch.setattr(scope_module, "collect_sast", _fixed_sast)
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm())
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert "*(SAST)*" in md
    assert "CWE-798" in md


def test_fingerprint_identical_across_runs(sample_project, settings):
    a = compute_fingerprint("src/app.py", "code_quality", 'print("hello")')
    b = compute_fingerprint("src/app.py", "code_quality", 'print("hello")\n')
    assert a == b


def test_provenance_recorded_in_full_run(sample_project, settings):
    """Attribution (FR-024 / constitution Principle I): a full pipeline run's
    report records the model, prompt version, and (pr-only) input revision.
    This also serves as integration coverage for prompt-version changes: the
    recorded version must equal the header actually loaded from the prompt
    files (T064: integration coverage for prompt changes)."""
    from veritas.review.nodes.common import current_prompt_version

    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm())
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert f"**Model**: `{default_fake_llm().model_name}`" in md or "fake-model" in md
    assert f"**Prompt version**: `{current_prompt_version()}`" in md
    assert "**Input revision**: `n/a`" in md  # revision tracking is PR-only