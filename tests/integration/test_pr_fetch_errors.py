"""Integration tests — a hosting API error while fetching the PR ends the run
with the contract's [error] line, never a traceback (T121).

contracts/hosting-api.md "Error Handling Contract": an error from
``pull_refs``, the PR file list or the PR's file contents is fatal: exit 1,
``[error] {provider} API error: {status} — {detail}`` on stderr, and no
report. Project-context reads stay the exception (T120): they degrade and the
review goes on.

Each test drives the real CLI through ``CliRunner`` with
``catch_exceptions=False``, so an exception that escapes the CLI fails the
test instead of being captured. The fake host raises the real
``GitHubAPIError`` / ``GitLabAPIError``, built as the request helpers build
them, and serves as both fetcher and poster.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import EmptyRepoRoot, default_fake_llm, fake_secret

import veritas.cli.app as cli_app
from veritas.hosting.factory import HostingClients
from veritas.hosting.github import GitHubAPIError
from veritas.hosting.gitlab import GitLabAPIError
from veritas.hosting.types import RootEntry
from veritas.review import graph as graph_module
from veritas.review.graph import run_review

HEAD = "head1234"
GITHUB_PR = "octo/demo#7"
GITLAB_MR = "https://gitlab.com/octo/demo/-/merge_requests/7"


class ErrorHost(EmptyRepoRoot):
    """A PR changing src/app.py whose reads raise ``errors[method]``.

    ``roots`` maps a commit to its root files (name -> text) for the
    project-context reads; ``errors`` may also key ``("read_optional", path)``.
    """

    def __init__(self, provider: str, errors: dict, roots: dict | None = None):
        self.provider = provider
        self.api_error = GitHubAPIError if provider == "github" else GitLabAPIError
        self.errors = errors
        self.roots = roots or {}
        self.posted: list[str] = []

    def _maybe_raise(self, key):
        if key in self.errors:
            raise self.errors[key]

    def head_sha(self, owner, repo, number):
        return HEAD

    def pull_refs(self, owner, repo, number):
        self._maybe_raise("pull_refs")
        return super().pull_refs(owner, repo, number)

    def list_pr_files(self, owner, repo, number):
        self._maybe_raise("list_pr_files")
        return [{"filename": "src/app.py"}]

    def list_mr_changes(self, owner, repo, number):
        self._maybe_raise("list_mr_changes")
        return [{"new_path": "src/app.py", "old_path": "src/app.py", "deleted_file": False}]

    def get_file_contents(self, owner, repo, path, ref):
        self._maybe_raise("get_file_contents")
        return "def f():\n    return 1\n"

    def get_file_at_ref(self, owner, repo, path, ref):
        self._maybe_raise("get_file_at_ref")
        return "def f():\n    return 1\n"

    def list_root(self, owner, repo, ref):
        return [RootEntry(name, "file", len(text)) for name, text in self.roots.get(ref, {}).items()]

    def read_optional(self, owner, repo, path, ref, *, strict_utf8=False):
        self._maybe_raise(("read_optional", path))
        return self.roots.get(ref, {}).get(path)

    def post_comment(self, owner, repo, number, body):
        self.posted.append(body)

    def post_note(self, owner, repo, number, body):
        self.posted.append(body)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """The working directory: the integration conftest's ``_sandbox_cwd`` has
    already moved into ``tmp_path`` and created an empty ``.veritas/``, so
    every file a run writes shows up under it."""
    assert Path.cwd() == tmp_path
    return tmp_path


def _written_files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def _invoke(monkeypatch, host: ErrorHost, target: str, *extra: str):
    monkeypatch.setattr(
        graph_module, "build_hosting_client", lambda _s, _p, _l: HostingClients(host, host)
    )
    monkeypatch.setattr(
        cli_app,
        "run_review",
        lambda settings, scope, tgt, **kw: run_review(settings, scope, tgt, llm=default_fake_llm(), **kw),
    )
    env = {
        "VERITAS_API_KEY": "test-key",
        "VERITAS_BASE_URL": "https://openrouter.ai/api/v1",
        "VERITAS_GITHUB_TOKEN": "ghp_test",
        "VERITAS_GITLAB_TOKEN": "glpat-test",
    }
    args = ["review", "--scope", "pr", "--target", target, *extra]
    return CliRunner().invoke(cli_app.app, args, env=env, catch_exceptions=False)


def _assert_fatal(result, expected_line: str, workdir: Path, host: ErrorHost) -> None:
    assert result.exit_code == 1
    # Exit 1 came from typer.Exit, not from an exception escaping the CLI.
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    error_lines = [line for line in result.stderr.splitlines() if line.startswith("[error]")]
    assert error_lines == [expected_line]
    assert result.stderr.splitlines()[-1] == expected_line
    # No report file, no .veritas/last-report.json, no comment: nothing that
    # could pass for a review.
    assert _written_files(workdir) == []
    assert host.posted == []


@pytest.mark.parametrize("post", [False, True], ids=["no-post", "post"])
def test_github_502_listing_pr_files_is_one_error_line(monkeypatch, workdir, post):
    host = ErrorHost("github", {"list_pr_files": GitHubAPIError(502, "Bad Gateway")})
    result = _invoke(monkeypatch, host, GITHUB_PR, *(["--post"] if post else []))

    _assert_fatal(result, "[error] github API error: 502 — Bad Gateway", workdir, host)


def test_github_404_on_pull_refs_is_one_error_line(monkeypatch, workdir):
    body = '{"message":"Not Found","documentation_url":"https://docs.github.com/rest","status":"404"}'
    host = ErrorHost("github", {"pull_refs": GitHubAPIError(404, body)})
    result = _invoke(monkeypatch, host, GITHUB_PR)

    _assert_fatal(result, f"[error] github API error: 404 — {body}", workdir, host)


def test_github_network_failure_reading_a_file_is_one_error_line(monkeypatch, workdir):
    error = GitHubAPIError(0, "network error: [Errno 11001] getaddrinfo failed", path="src/app.py")
    host = ErrorHost("github", {"get_file_contents": error})
    result = _invoke(monkeypatch, host, GITHUB_PR)

    _assert_fatal(
        result,
        "[error] github API error: 0 — network error: [Errno 11001] getaddrinfo failed (path: src/app.py)",
        workdir,
        host,
    )


def test_gitlab_502_listing_mr_changes_is_one_error_line(monkeypatch, workdir):
    host = ErrorHost("gitlab", {"list_mr_changes": GitLabAPIError(502, "Bad Gateway")})
    result = _invoke(monkeypatch, host, GITLAB_MR)

    _assert_fatal(result, "[error] gitlab API error: 502 — Bad Gateway", workdir, host)


def test_a_multi_line_response_body_stays_on_one_error_line(monkeypatch, workdir):
    body = "<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n</html>"
    host = ErrorHost("github", {"list_pr_files": GitHubAPIError(502, body)})
    result = _invoke(monkeypatch, host, GITHUB_PR)

    _assert_fatal(
        result,
        "[error] github API error: 502 — <html> <head><title>502 Bad Gateway</title></head> </html>",
        workdir,
        host,
    )


def test_a_token_in_the_error_detail_is_redacted(monkeypatch, workdir):
    token = fake_secret("ghp_", "FetchErrorToken0123456789abcdefABCDEF")
    host = ErrorHost(
        "github",
        {"list_pr_files": GitHubAPIError(401, f"Bad credentials for token {token}")},
    )
    result = _invoke(monkeypatch, host, GITHUB_PR)

    assert result.exit_code == 1
    assert token not in result.stderr
    assert result.stderr.splitlines()[-1].startswith("[error] github API error: 401 — Bad credentials")
    assert _written_files(workdir) == []


def test_a_502_reading_pyproject_still_degrades_the_context(monkeypatch, workdir):
    host = ErrorHost(
        "github",
        {("read_optional", "pyproject.toml"): GitHubAPIError(502, "Bad Gateway", path="pyproject.toml")},
        roots={
            HEAD: {"pyproject.toml": '[project]\nname = "demo"\n'},
            EmptyRepoRoot.BASE_SHA: {"AGENTS.md": "Prefer early returns.\n"},
        },
    )
    result = _invoke(monkeypatch, host, GITHUB_PR)

    assert result.exit_code == 0
    assert "[error]" not in result.stderr
    context = [line for line in result.stdout.splitlines() if line.startswith("Context:")]
    assert len(context) == 1
    assert context[0].startswith("Context: partial")
    assert "reading pyproject.toml at the head commit failed: github API error: 502 — Bad Gateway" in context[0]
    assert any(path.name.startswith("veritas-report-") for path in workdir.iterdir())
