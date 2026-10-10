"""Integration tests — every finished output is redacted where it is written or sent (T105, FR-013).

A credentialed GitLab MR URL is the --target and also appears in requirement
evidence. Its token must reach none of: the Markdown report file, the compact
stdout text, .veritas/last-report.json, or the --post body. The suppress flow
must keep working from the redacted last-report.json.
"""

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from tests.conftest import APP_CONTENT, FakeLLM, canned_code_findings, fake_secret

from veritas.cli.app import app
from veritas.config.constants import LAST_REPORT_JSON, SUPPRESSIONS_PATH
from veritas.models.entities import Report, ReviewScope
from veritas.review.graph import run_review
from veritas.utils.redaction import redact_json_strings

# Assembled at runtime so no token-shaped literal sits in the test source.
GLPAT = fake_secret("glpat-", "OutputRedaction0123456789")
MR_URL = f"https://oauth2:{GLPAT}@gitlab.com/group/repo/-/merge_requests/1"
# What a masked MR_URL reads as: the credentials are gone, the location is kept.
MASKED_MR_URL = "[REDACTED]gitlab.com/group/repo/-/merge_requests/1"


class FakeGitLab:
    """Serves one MR (src/app.py and spec.md) and records every posted note."""

    provider = "gitlab"

    def __init__(self):
        self.notes: list[str] = []

    def head_sha(self, owner, repo, number):
        return "abcd1234"

    def list_mr_changes(self, owner, repo, number):
        return [{"new_path": "src/app.py"}, {"new_path": "spec.md"}]

    def get_file_at_ref(self, owner, repo, path, ref):
        if path == "spec.md":
            return "# REQ-1\n\n- SATISFIED: tool supports project scope.\n"
        return APP_CONTENT

    def post_note(self, owner, repo, number, body):
        self.notes.append(body)


def _mr_llm() -> FakeLLM:
    requirement = [
        {
            "requirement_ref": "REQ-1",
            "requirement_text": "The tool must support project scope reviews.",
            "status": "partial",
            "evidence": ["src/app.py:2", MR_URL],
            "explanation": "Basic support exists.",
        }
    ]
    return FakeLLM(
        {
            "code-quality": canned_code_findings(),
            "requirements-traceability": json.dumps(requirement),
        }
    )


def test_credentialed_mr_url_token_appears_in_no_output(monkeypatch, settings, capsys):
    from veritas.review.nodes import scope as scope_module

    host = FakeGitLab()
    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: host)

    outcome = run_review(settings, ReviewScope.PR, MR_URL, post=True, llm=_mr_llm())
    captured = capsys.readouterr()

    report_md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    last_report = Path(LAST_REPORT_JSON).read_text(encoding="utf-8")
    assert len(host.notes) == 1
    body = host.notes[0]

    for name, text in {
        "report file": report_md,
        "stdout": captured.out,
        "last-report.json": last_report,
        "post body": body,
        "stderr": captured.err,
    }.items():
        assert GLPAT not in text, f"token leaked into {name}"
        assert "oauth2:" not in text, f"credentials leaked into {name}"

    # The URL did reach each written output (the run target and the
    # unconfirmed evidence reference), masked rather than dropped.
    assert f"- **Target**: `{MASKED_MR_URL}`" in report_md
    assert MASKED_MR_URL in body
    report = Report.model_validate_json(last_report)
    assert report.run.target == MASKED_MR_URL
    assert any(MASKED_MR_URL in vf.reason for vf in report.summary.verification_failures)
    # The compact stdout summary was printed and points at the report.
    assert str(outcome.report_path) in captured.out


def test_redacted_last_report_still_validates_and_keeps_hex_values(monkeypatch, settings):
    from veritas.review.nodes import scope as scope_module

    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: FakeGitLab())
    run_review(settings, ReviewScope.PR, MR_URL, llm=_mr_llm())

    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{64}", report.run.config_hash)
    assert report.run.input_revision == "abcd1234"


def test_redact_json_strings_masks_values_and_leaves_keys_and_numbers():
    data = {"target": MR_URL, "count": 3, "ok": True, "none": None, "list": [MR_URL, 1.5]}

    out = redact_json_strings(data)

    assert out == {
        "target": MASKED_MR_URL,
        "count": 3,
        "ok": True,
        "none": None,
        "list": [MASKED_MR_URL, 1.5],
    }


# --- the suppress flow works from a redacted last-report.json --------------------

KEY = fake_secret("sk-", "SuppressFlowKey0123456789abcdef")
SECRET_LINE = f'api_key = "{KEY}"'


def _secret_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text(
        f"import os\n{SECRET_LINE}\nprint(api_key)\n", encoding="utf-8"
    )
    return project


def _secret_llm() -> FakeLLM:
    return FakeLLM(
        {
            "code-quality": canned_code_findings(
                snippet=SECRET_LINE, start_line=2, end_line=2, finding_id="cf-hardcoded-key"
            )
        }
    )


def test_finding_suppressed_from_redacted_last_report(tmp_path, settings):
    project = _secret_project(tmp_path)

    first = run_review(settings, ReviewScope.PROJECT, str(project), llm=_secret_llm())
    # Reported before suppression, so its absence after the rerun means something.
    assert "Unused import os" in Path(str(first.report_path)).read_text(encoding="utf-8")
    raw = Path(LAST_REPORT_JSON).read_text(encoding="utf-8")
    assert KEY not in raw
    report = Report.model_validate_json(raw)
    (finding,) = [f for f in report.code_findings if f.id == "cf-hardcoded-key"]
    assert finding.cited_snippet == "api_key = [REDACTED]"

    result = CliRunner().invoke(app, ["suppress", "--finding-id", finding.id])
    assert result.exit_code == 0, result.output
    fingerprint = re.search(r"Fingerprint: ([0-9a-f]{64})", result.stdout).group(1)
    stored = json.loads(Path(SUPPRESSIONS_PATH).read_text(encoding="utf-8"))
    assert [e["fingerprint"] for e in stored["entries"]] == [fingerprint]
    assert KEY not in Path(SUPPRESSIONS_PATH).read_text(encoding="utf-8")

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=_secret_llm())

    rerun = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert all(f.id != "cf-hardcoded-key" for f in rerun.code_findings)
    assert "Unused import os" not in Path(str(outcome.report_path)).read_text(encoding="utf-8")
