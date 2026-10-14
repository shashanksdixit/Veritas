"""Frozen per-node contexts and hosting construction before the graph (T111, FR-026).

``run_review`` builds the hosting clients after the LLM client and before the
graph, then hands each node factory only its own frozen context: the scope node
the read-only fetcher, the render node the poster (only with ``--post``), and
the review nodes neither.
"""

from __future__ import annotations

import ast
import io
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace

import pytest

import veritas.review.nodes.scope as scope_module
from tests.conftest import EmptyRepoRoot, default_fake_llm
from veritas.config.settings import Settings
from veritas.hosting.factory import HostingClients
from veritas.hosting.resolver import parse_pr_target
from veritas.llm.client import LLMClient, split_model_string
from veritas.llm.zdr import zdr_warning_text
from veritas.models.entities import ReviewScope
from veritas.review import graph as graph_module
from veritas.review.contexts import RenderContext, ReviewContext, ScopeContext
from veritas.review.graph import run_review
from veritas.suppression.store import SuppressionStore
from veritas.utils.logging import Log

GITHUB_TOKEN_MISSING = (
    "PR review requires a GitHub token: set VERITAS_GITHUB_TOKEN "
    "(never commit tokens; see contracts/cli.md)."
)
GITLAB_TOKEN_MISSING = (
    "PR review requires a GitLab token: set VERITAS_GITLAB_TOKEN "
    "(never commit tokens; see contracts/cli.md)."
)


# --- hosting clients are built before the graph ---


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("acme/widget#3", GITHUB_TOKEN_MISSING),
        ("group/project!3", GITLAB_TOKEN_MISSING),
    ],
)
def test_missing_token_fails_before_the_graph_is_built(monkeypatch, tmp_path, target, message):
    from typer.testing import CliRunner

    from veritas.cli.app import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VERITAS_API_KEY", "test-key")
    built: list[object] = []
    monkeypatch.setattr(graph_module, "build_graph", lambda *a, **k: built.append(a))

    result = CliRunner().invoke(app, ["review", "--scope", "pr", "--target", target])

    defaults = Settings()
    zdr_line = "[warn] " + zdr_warning_text(
        defaults.base_url, split_model_string(defaults.model_runtime)[0]
    )
    lines = result.stderr.strip().splitlines()
    assert result.exit_code == 1
    assert lines == [zdr_line, f"[error] {message}"]
    assert "Scanning target in scope" not in result.stderr
    assert "Scanning target in scope" not in result.stdout
    assert built == []


def test_a_missing_api_key_still_wins_over_a_missing_token(monkeypatch, tmp_path):
    """The LLM client is built first, so its error is the one a user sees."""
    from typer.testing import CliRunner

    from veritas.cli.app import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    hosting_calls: list[str] = []
    monkeypatch.setattr(
        graph_module,
        "build_hosting_client",
        lambda _s, provider, _l: hosting_calls.append(provider),
    )

    result = CliRunner().invoke(app, ["review", "--scope", "pr", "--target", "acme/widget#3"])

    assert result.exit_code == 1
    last = result.stderr.strip().splitlines()[-1]
    assert last.startswith("[error] failed to initialize the LLM client for "), last
    assert hosting_calls == []


def _capture_contexts(monkeypatch) -> dict:
    """Record the three contexts run_review passes to build_graph."""
    seen: dict = {}
    real_build_graph = graph_module.build_graph

    def spy(review, scope_ctx, render_ctx):
        seen.update(review=review, scope=scope_ctx, render=render_ctx)
        return real_build_graph(review, scope_ctx, render_ctx)

    monkeypatch.setattr(graph_module, "build_graph", spy)
    return seen


class _Fetcher(EmptyRepoRoot):
    def head_sha(self, owner, repo, number):
        return "abcd1234"

    def list_pr_files(self, owner, repo, number):
        return [{"filename": "src/app.py"}]

    def get_file_contents(self, owner, repo, path, ref):
        return "def f():\n    return 1\n"


class _Poster:
    def __init__(self):
        self.posted: list[str] = []

    def post_comment(self, owner, repo, number, body):
        self.posted.append(body)


def test_without_post_the_poster_is_none_and_the_fetcher_is_still_built(
    monkeypatch, settings, tmp_path
):
    fetcher, poster = _Fetcher(), _Poster()
    monkeypatch.setattr(
        graph_module, "build_hosting_client", lambda _s, _p, _l: HostingClients(fetcher, poster)
    )
    seen = _capture_contexts(monkeypatch)

    outcome = run_review(
        settings,
        ReviewScope.PR,
        "acme/widget#3",
        output=str(tmp_path / "report.md"),
        llm=default_fake_llm(),
    )

    assert outcome.exit_code == 0
    assert seen["scope"].fetcher is fetcher
    assert seen["scope"].pr == parse_pr_target("acme/widget#3")
    assert seen["render"].poster is None
    assert seen["render"].pr == parse_pr_target("acme/widget#3")
    assert poster.posted == []


def test_with_post_the_render_context_gets_the_poster(monkeypatch, settings, tmp_path):
    fetcher, poster = _Fetcher(), _Poster()
    monkeypatch.setattr(
        graph_module, "build_hosting_client", lambda _s, _p, _l: HostingClients(fetcher, poster)
    )
    seen = _capture_contexts(monkeypatch)

    run_review(
        settings,
        ReviewScope.PR,
        "acme/widget#3",
        output=str(tmp_path / "report.md"),
        post=True,
        llm=default_fake_llm(),
    )

    assert seen["render"].poster is poster
    assert len(poster.posted) == 1


def test_a_local_review_builds_no_hosting_client(monkeypatch, settings, sample_project, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(
        graph_module, "build_hosting_client", lambda _s, provider, _l: calls.append(provider)
    )
    seen = _capture_contexts(monkeypatch)

    run_review(
        settings,
        ReviewScope.PROJECT,
        str(sample_project),
        output=str(tmp_path / "report.md"),
        llm=default_fake_llm(),
    )

    assert calls == []
    assert seen["scope"].fetcher is None
    assert seen["scope"].pr is None
    assert seen["render"].poster is None


# --- which node holds what ---


def test_only_the_render_context_holds_the_store_and_the_poster():
    review = {f.name for f in fields(ReviewContext)}
    scope = {f.name for f in fields(ScopeContext)}
    render = {f.name for f in fields(RenderContext)}

    assert review == {"llm", "log"}
    assert scope == {
        "log", "exclude", "batch_chars", "max_batches", "opengrep_rules", "fetcher", "pr"
    }
    assert render == {
        "log", "report_path", "batch_chars", "max_batches", "store", "poster", "pr"
    }
    assert not {"store", "poster"} & (review | scope)
    assert "fetcher" not in review | render


def test_graph_nodes_carry_their_role(settings):
    log = Log()
    review = ReviewContext(llm=default_fake_llm(), log=log)
    scope_ctx = ScopeContext(
        log=log, exclude=(), batch_chars=48000, max_batches=8,
        opengrep_rules=settings.opengrep_rules, fetcher=None, pr=None,
    )
    render_ctx = RenderContext(
        log=log, report_path="unused.md", batch_chars=48000, max_batches=8,
        store=SuppressionStore(), poster=None, pr=None,
    )

    nodes = graph_module.build_graph(review, scope_ctx, render_ctx).builder.nodes

    assert nodes["scope"].metadata == {"veritas_role": "input"}
    assert nodes["render"].metadata == {"veritas_role": "output"}
    for name in ("context", "code_quality", "security", "requirements",
                 "test_coverage", "performance", "verify"):
        assert not (nodes[name].metadata or {}).get("veritas_role"), name


def test_the_scope_node_imports_no_hosting_module():
    tree = ast.parse(Path(scope_module.__file__).read_text(encoding="utf-8"))
    imported = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not [name for name in imported if name.startswith("veritas.hosting")], imported


# --- LLMClient keeps only the output limit ---


def test_llm_client_holds_no_settings_and_names_the_limit_when_truncated():
    stream = io.StringIO()
    client = LLMClient(
        Settings(api_key="test-key", model="openai:gpt-4o-mini", max_output_tokens=1234),
        Log(stream=stream),
    )
    client._model = SimpleNamespace(
        invoke=lambda *_a, **_k: SimpleNamespace(
            content="[]", response_metadata={"finish_reason": "length"}
        )
    )

    assert not hasattr(client, "settings")
    assert client.complete("system", "user") == "[]"
    assert (
        "[warn] llm: response truncated at max_output_tokens (1234); "
        "raise [llm] max_output_tokens if findings are missing"
    ) in stream.getvalue()


# --- the contexts are frozen and slotted ---


def _contexts() -> list:
    log = Log()
    return [
        ReviewContext(llm=None, log=log),
        ScopeContext(
            log=log, exclude=("vendor/",), batch_chars=1000, max_batches=1,
            opengrep_rules="auto", fetcher=None, pr=None,
        ),
        RenderContext(
            log=log, report_path="r.md", batch_chars=1000, max_batches=1,
            store=SuppressionStore(), poster=None, pr=None,
        ),
    ]


@pytest.mark.parametrize("ctx", _contexts(), ids=lambda c: type(c).__name__)
def test_a_context_rejects_assignment_and_has_no_dict(ctx):
    for field in fields(ctx):
        with pytest.raises(FrozenInstanceError):
            setattr(ctx, field.name, None)
    # A new attribute cannot be bolted on either. Which error says so depends
    # on the Python version: with slots=True, 3.12 raises TypeError here rather
    # than FrozenInstanceError (the slotted class is a new class object).
    with pytest.raises((FrozenInstanceError, TypeError, AttributeError)):
        ctx.hosting = object()
    assert not hasattr(ctx, "hosting")
    assert not hasattr(ctx, "__dict__")
