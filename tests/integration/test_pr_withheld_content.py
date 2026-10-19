"""PR files whose content the hosting API withholds are reported as not
reviewed, never reviewed as empty (T124, FR-029).

GitHub's contents API answers a 1-100 MB file with ``encoding: "none"`` and
empty content; GitLab's documentation does not say what it returns, so empty
content for a blob of non-zero size counts as withheld. The fetchers raise
``ContentWithheldError`` and the scope node records the file as an excluded
file with the reason: it reaches no prompt and no SAST scan, and the coverage
section and line count it as excluded. A genuinely empty file is still
reviewed. Any other fetch error stays fatal (T121, tests/integration/test_pr_fetch_errors.py).
"""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import unquote

import httpx

from tests.conftest import EmptyRepoRoot, FakeLLM
from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.hosting import github
from veritas.hosting.factory import HostingClients
from veritas.models.entities import ExcludedFile, Report, ReviewScope
from veritas.review import ContentWithheldError
from veritas.review import graph as graph_module
from veritas.review.graph import run_review
from veritas.review.nodes import scope as scope_module
from veritas.security.opengrep import OpengrepResult

HEAD = "abcd1234ef567890abcd1234ef567890abcd1234"
NORMAL = "def f():\n    return 1\n"
GITHUB_REASON = "content not provided by the hosting API (file over 1 MB)"
GITLAB_REASON = "content not provided by the hosting API (5000000-byte file returned empty)"


class WithholdingHost(EmptyRepoRoot):
    """A PR changing ``contents`` (path -> text); a path in ``withheld``
    (path -> reason) raises ``ContentWithheldError`` as the real fetchers do.
    ``fetched`` records every file request."""

    def __init__(self, provider: str, contents: dict[str, str], withheld: dict[str, str]):
        self.provider = provider
        self.contents = contents
        self.withheld = withheld
        self.fetched: list[str] = []

    def head_sha(self, owner, repo, number):
        return HEAD

    def list_pr_files(self, owner, repo, number):
        return [{"filename": path} for path in self.contents]

    def list_mr_changes(self, owner, repo, number):
        return [{"new_path": path, "old_path": path, "deleted_file": False} for path in self.contents]

    def _get(self, path):
        self.fetched.append(path)
        if path in self.withheld:
            raise ContentWithheldError(path, self.withheld[path])
        return self.contents[path]

    def get_file_contents(self, owner, repo, path, ref):
        assert self.provider == "github"
        return self._get(path)

    def get_file_at_ref(self, owner, repo, path, ref):
        assert self.provider == "gitlab"
        return self._get(path)


def _capture_sast(monkeypatch) -> list[list[str]]:
    """Replace collect_sast; each call records the file set it was asked to scan."""
    scans: list[list[str]] = []

    def _capture(files, *, scope_value, rules=None, opengrep_bin="opengrep", jobs=4):
        scans.append(sorted(files))
        return OpengrepResult(findings=[], ran=True, rules=rules or "p/owasp-top-ten")

    monkeypatch.setattr(scope_module, "collect_sast", _capture)
    return scans


def _review(monkeypatch, capsys, host, target):
    """Run a PR review against ``host``: the outcome, the saved report, the
    Markdown text, stdout and stderr lines, the fake LLM and the SAST scans."""
    monkeypatch.setattr(
        graph_module, "build_hosting_client", lambda _s, _p, _l: HostingClients(host, None)
    )
    scans = _capture_sast(monkeypatch)
    settings = Settings(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        github_token="ghp_test",
        gitlab_token="glpat-test",
    )
    llm = FakeLLM({})
    outcome = run_review(settings, ReviewScope.PR, target, llm=llm)
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    captured = capsys.readouterr()
    return outcome, report, md, captured.out.splitlines(), captured.err.splitlines(), llm, scans


def _assert_withheld(outcome, report, md, stdout, stderr, llm, scans, path, reason):
    """``path`` was left out with ``reason`` and src/app.py was reviewed."""
    assert outcome.exit_code == 0
    coverage = report.coverage
    assert coverage.reviewed_files == ["src/app.py"]
    assert coverage.excluded_files == [ExcludedFile(path=path, reason=reason)]
    assert coverage.not_reviewed_files == []
    # Markdown: counted in the table and listed with its reason.
    assert "| Files excluded | 1 |" in md
    assert f"- `{path}`: {reason}" in md
    # Compact stdout: one coverage line counting it as excluded.
    assert [line for line in stdout if line.startswith("Coverage:")] == [
        "Coverage: 1 reviewed (0 split), 1 excluded, 0 not reviewed; 1/8 batches"
    ]
    # One warning naming the file and the reason.
    assert f"[warn] scope: {path} not reviewed: {reason}" in stderr
    # No prompt carries its path; the normal file did reach the model.
    prompts = [system + user for system, user in llm.calls]
    assert prompts
    assert not any(path in prompt for prompt in prompts)
    assert any("src/app.py" in prompt for prompt in prompts)
    # SAST was asked to scan the normal file only.
    assert scans == [["src/app.py"]]


def test_github_withheld_file_is_excluded_with_its_reason(monkeypatch, capsys):
    host = WithholdingHost(
        "github",
        {"src/app.py": NORMAL, "data/big.py": ""},
        {"data/big.py": GITHUB_REASON},
    )
    result = _review(monkeypatch, capsys, host, "acme/widget#3")
    _assert_withheld(*result, path="data/big.py", reason=GITHUB_REASON)
    # Requested once: the requirement-source loop does not ask again.
    assert host.fetched == ["src/app.py", "data/big.py"]


def test_gitlab_withheld_file_is_excluded_with_its_reason(monkeypatch, capsys):
    host = WithholdingHost(
        "gitlab",
        {"src/app.py": NORMAL, "data/big.py": ""},
        {"data/big.py": GITLAB_REASON},
    )
    result = _review(
        monkeypatch, capsys, host, "https://gitlab.com/acme/widget/-/merge_requests/3"
    )
    _assert_withheld(*result, path="data/big.py", reason=GITLAB_REASON)


def test_withheld_requirement_source_is_excluded_not_read_as_empty(monkeypatch, capsys):
    """The second _fetch_pr loop (requirement sources, FR-008) records it too."""
    host = WithholdingHost(
        "github",
        {"src/app.py": NORMAL, "docs/requirements.md": ""},
        {"docs/requirements.md": GITHUB_REASON},
    )
    outcome, report, md, stdout, stderr, llm, scans = _review(
        monkeypatch, capsys, host, "acme/widget#3"
    )
    assert outcome.exit_code == 0
    assert report.coverage.excluded_files == [
        ExcludedFile(path="docs/requirements.md", reason=GITHUB_REASON)
    ]
    assert f"- `docs/requirements.md`: {GITHUB_REASON}" in md
    assert "[warn] scope: docs/requirements.md not reviewed: " + GITHUB_REASON in stderr
    # Not read as an empty requirements document: the run found none at all.
    assert [f.requirement_ref for f in report.requirement_findings] == [
        "No requirements documentation found"
    ]
    assert not any("docs/requirements.md" in system + user for system, user in llm.calls)
    assert scans == [["src/app.py"]]


def test_genuinely_empty_changed_file_is_reviewed_not_excluded(monkeypatch, capsys):
    host = WithholdingHost("github", {"src/app.py": NORMAL, "src/empty.py": ""}, {})
    outcome, report, md, stdout, stderr, llm, scans = _review(
        monkeypatch, capsys, host, "acme/widget#3"
    )
    assert outcome.exit_code == 0
    assert report.coverage.reviewed_files == ["src/app.py", "src/empty.py"]
    assert report.coverage.excluded_files == []
    assert "| Files excluded | 0 |" in md
    assert [line for line in stdout if line.startswith("Coverage:")] == [
        "Coverage: 2 reviewed (0 split), 0 excluded, 0 not reviewed; 1/8 batches"
    ]
    assert not any("not reviewed:" in line for line in stderr)
    assert any("src/empty.py" in system + user for system, user in llm.calls)
    assert scans == [["src/app.py", "src/empty.py"]]


def test_real_github_fetcher_encoding_none_is_excluded_end_to_end(monkeypatch, capsys):
    """The production GitHubFetcher over a mock transport: GitHub's real answer
    for a large file becomes an excluded file, not an empty one."""
    contents_prefix = "/repos/acme/widget/contents/"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path = request.url.path
        if path == "/repos/acme/widget/pulls/3":
            return httpx.Response(200, json={"head": {"sha": HEAD}, "base": {"sha": "b" * 40}})
        if path == "/repos/acme/widget/pulls/3/files":
            return httpx.Response(200, json=[{"filename": "src/app.py"}, {"filename": "data/big.py"}])
        if path.rstrip("/") == "/repos/acme/widget/contents":
            return httpx.Response(200, json=[])
        name = unquote(path[len(contents_prefix):])
        if name == "src/app.py":
            encoded = base64.b64encode(NORMAL.encode()).decode()
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": encoded})
        if name == "data/big.py":
            return httpx.Response(
                200, json={"type": "file", "encoding": "none", "content": "", "size": 5_000_000}
            )
        return httpx.Response(404, json={"message": "Not Found"})

    fetcher = github.GitHubFetcher("ghp_test")
    fetcher.client = github._new_client(
        "ghp_test", github.BASE_URL, read_only=True, transport=httpx.MockTransport(handler)
    )
    result = _review(monkeypatch, capsys, fetcher, "acme/widget#3")
    _assert_withheld(*result, path="data/big.py", reason=GITHUB_REASON)
