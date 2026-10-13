"""Golden tests: moving project-context reading into the scope node (T116)
changed nothing the model receives, and stating reading problems (T118)
changed only the log.

``data/project_context_golden.json`` holds, for each case, the exact
``project_context`` string and the log lines the context node produced. Cases a
to g were captured before T116, when the context node read the files itself;
T118 added one ``[warn]`` line to d and e and left every ``context`` string
byte-identical. Cases h to k were captured at T118 for the problems it states
(non-UTF-8 and non-TOML pyproject.toml) and what it fixed (a blank AGENTS.md
no longer hides STYLE.md; conventions are redacted). Here the scope node reads
the files into ``project_sources`` and the context node renders that, and both
must match the captures byte for byte.

Case ``a`` reviews this repository, so its golden follows the repository's own
``pyproject.toml``: a dependency change there means recapturing it.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.integration.project_context_cases import CASE_K_SECRET, PR_TARGET, REPO_ROOT, TMP_CASES
from veritas.config.settings import Settings
from veritas.hosting.resolver import parse_pr_target
from veritas.models.entities import ReviewRun, ReviewScope
from veritas.review.contexts import ReviewContext, ScopeContext
from veritas.review.nodes.context import make_context_node
from veritas.review.nodes.scope import make_scope_node
from veritas.utils.logging import Log

GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "project_context_golden.json").read_text(encoding="utf-8")
)


class _FakeFetcher:
    """A PR with one changed file; enough for the scope node to finish."""

    def head_sha(self, owner, repo, number):
        return "abcd1234"

    def list_pr_files(self, owner, repo, number):
        return [{"filename": "src/app.py"}]

    def get_file_contents(self, owner, repo, path, ref):
        return "x = 1\n"


def _scope_ctx(*, fetcher=None) -> ScopeContext:
    settings = Settings(api_key="test-key")
    return ScopeContext(
        log=Log(stream=io.StringIO()),
        exclude=tuple(settings.exclude),
        batch_chars=settings.batch_chars,
        max_batches=settings.max_batches,
        opengrep_rules=settings.opengrep_rules,
        fetcher=fetcher,
        pr=parse_pr_target(PR_TARGET) if fetcher is not None else None,
    )


def _state(scope: ReviewScope, target: str) -> dict:
    run = ReviewRun(
        scope=scope,
        target=target,
        config_hash="h",
        model_name="m",
        prompt_version="1.7.0",
        started_at=datetime.now(),
    )
    return {"messages": [], "scope": scope, "target": target, "run": run, "files": {}}


def _scope_then_context(scope: ReviewScope, target: str, *, fetcher=None) -> dict:
    """Run the scope node, then the context node on the state it produced.

    Only the context node's log is captured, as in the golden capture.
    """
    state = _state(scope, target)
    state.update(make_scope_node(_scope_ctx(fetcher=fetcher))(state))
    stream = io.StringIO()
    result = make_context_node(ReviewContext(llm=None, log=Log(stream=stream)))(state)
    return {"context": result["project_context"], "log": stream.getvalue().splitlines()}


def test_golden_a_this_repository(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    assert _scope_then_context(ReviewScope.PROJECT, ".") == GOLDEN["a"]


@pytest.mark.parametrize("case", sorted(TMP_CASES))
def test_golden_tmp_project(case, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    scope, target = TMP_CASES[case](root)
    assert _scope_then_context(scope, target) == GOLDEN[case]


def test_golden_g_pr_keeps_the_hard_coded_note():
    assert _scope_then_context(ReviewScope.PR, PR_TARGET, fetcher=_FakeFetcher()) == GOLDEN["g"]


def test_goldens_state_the_limits_they_hit():
    """The captures themselves (T118): d still truncated at 4,000 characters and
    e still skipped, now each with a [warn] line; f unavailable and silent."""
    conventions = GOLDEN["d"]["context"].split("Project conventions to honor:\n", 1)[1]
    assert len(conventions) == 4000
    assert GOLDEN["d"]["log"] == [
        "[info] Detected: .NET net8.0",
        "[warn] Project context: AGENTS.md truncated to 4000 characters",
    ]
    assert "Node" not in GOLDEN["e"]["context"]
    assert GOLDEN["e"]["log"] == [
        "[info] Detected: Go 1.21",
        "[warn] Project context: package.json over 200000 bytes, skipped",
    ]
    assert GOLDEN["f"]["context"].startswith("Note: Project context unavailable (no detectable")
    assert GOLDEN["f"]["log"] == []


def test_goldens_for_the_t118_cases():
    """h: non-UTF-8 pyproject.toml skipped, go.mod still used. i: non-TOML
    pyproject.toml, nothing detected. j: a blank AGENTS.md passed over for
    STYLE.md. k: the token in AGENTS.md reaches the model masked."""
    assert "Python" not in GOLDEN["h"]["context"]
    assert GOLDEN["h"]["log"][-1] == "[warn] Project context: pyproject.toml is not valid UTF-8, skipped"
    assert GOLDEN["i"]["context"].startswith("Note: Project context unavailable")
    assert GOLDEN["i"]["log"] == ["[warn] Project context: pyproject.toml is not valid TOML"]
    assert GOLDEN["j"]["context"] == "Project conventions to honor:\nUse four-space indentation."
    assert GOLDEN["k"]["context"] == "Project conventions to honor:\nDeploy with [REDACTED] only from CI."


def test_case_k_secret_never_reaches_the_context_or_the_log(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    result = _scope_then_context(*TMP_CASES["k"](root))
    assert CASE_K_SECRET not in result["context"]
    assert all(CASE_K_SECRET not in line for line in result["log"])
    # The file on disk really holds it: the masking happened on the way to the model.
    assert CASE_K_SECRET in (root / "AGENTS.md").read_text(encoding="utf-8")


def test_pr_scope_writes_no_project_sources():
    state = _state(ReviewScope.PR, PR_TARGET)
    result = make_scope_node(_scope_ctx(fetcher=_FakeFetcher()))(state)
    assert result["project_sources"] is None


def test_scope_writes_project_sources_for_a_local_scope(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    scope, target = TMP_CASES["c"](root)
    sources = make_scope_node(_scope_ctx())(_state(scope, target))["project_sources"]
    assert sources.origin == str(root)
    assert [name for name, _text in sources.files] == ["package.json", "go.mod"]
    assert sources.problems == ()
