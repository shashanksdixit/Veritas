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

Cases l to o (T119) walk up from a module or file target to a project root
marked by ``.git``. They are new behaviour, so their expected context, log,
sources and origin are written by hand below rather than captured.

Case g (a PR review) changed on purpose at T120: it no longer uses a
hard-coded "unavailable for a remote PR review" note but reads the PR's
repository root through the fetcher, like any other scope. Its fake root is
empty, so g now renders case f's "nothing detectable" note with no log line.

Case ``a`` reviews this repository, so its golden follows the repository's own
``pyproject.toml``: a dependency change there means recapturing it.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import EmptyRepoRoot
from tests.integration.project_context_cases import (
    CASE_K_SECRET,
    PR_TARGET,
    REPO_ROOT,
    TMP_CASES,
    WALK_UP_CASES,
)
from veritas.config.settings import Settings
from veritas.hosting.resolver import parse_pr_target
from veritas.models.entities import ProjectContextStatus, ReviewRun, ReviewScope
from veritas.review.contexts import ReviewContext, ScopeContext
from veritas.review.nodes.context import make_context_node
from veritas.review.nodes.scope import make_scope_node
from veritas.utils.logging import Log

GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "project_context_golden.json").read_text(encoding="utf-8")
)


class _FakeFetcher(EmptyRepoRoot):
    """A PR with one changed file and an empty repository root at both commits."""

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


def _run_scope_then_context(scope: ReviewScope, target: str, *, fetcher=None) -> tuple[dict, ReviewRun]:
    """Run the scope node, then the context node on the state it produced:
    the context string and log (as in the golden capture: only the context
    node's log), and the ``run`` the context node returned."""
    state = _state(scope, target)
    state.update(make_scope_node(_scope_ctx(fetcher=fetcher))(state))
    stream = io.StringIO()
    result = make_context_node(ReviewContext(llm=None, log=Log(stream=stream)))(state)
    captured = {"context": result["project_context"], "log": stream.getvalue().splitlines()}
    return captured, result["run"]


def _scope_then_context(scope: ReviewScope, target: str, *, fetcher=None) -> dict:
    return _run_scope_then_context(scope, target, fetcher=fetcher)[0]


def test_golden_a_this_repository(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    assert _scope_then_context(ReviewScope.PROJECT, ".") == GOLDEN["a"]


@pytest.mark.parametrize("case", sorted(TMP_CASES))
def test_golden_tmp_project(case, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    scope, target = TMP_CASES[case](root)
    assert _scope_then_context(scope, target) == GOLDEN[case]


# --- T119: walking up to the project root (cases l to o) ----------------------
#
# Written by hand from the case layouts in project_context_cases.py, not
# captured from the implementation. The integration conftest makes tmp_path
# the working directory, so the project root tmp_path/project shows as
# "project".

_B_CONTEXT = (
    "Detected manifest language/framework versions:\n"
    "  - Python >=3.12\n"
    "  - pydantic >=2.0\n"
    "  - httpx >=0.27,<1\n"
    "Project conventions to honor:\n"
    "Prefer early returns.\n"
    "No print() in library code.\n"
    "Project-specific natural-language review rules:\n"
    "Flag any eval() usage."
)
_B_LOG = [
    "[info] Detected: Python >=3.12",
    "[info] Detected: pydantic >=2.0",
    "[info] Detected: httpx >=0.27,<1",
]

WALK_UP_EXPECTED = {
    # --scope module src: the root's pyproject.toml, AGENTS.md and rules, as in b.
    "l": {
        "context": _B_CONTEXT,
        "log": _B_LOG,
        "sources": ["pyproject.toml", "AGENTS.md", ".veritas/rules.md"],
        "origin": "project",
    },
    # --scope file src/pkg/mod.py: the same, two levels down.
    "m": {
        "context": _B_CONTEXT,
        "log": _B_LOG,
        "sources": ["pyproject.toml", "AGENTS.md", ".veritas/rules.md"],
        "origin": "project",
    },
    # --scope module src/pkg: the package's pyproject.toml only (no Node from
    # the root's package.json, no Python >=3.12 or pins from the root's
    # pyproject.toml); conventions and rules from the root.
    "n": {
        "context": (
            "Detected manifest language/framework versions:\n"
            "  - Python >=3.13\n"
            "Project conventions to honor:\n"
            "Prefer early returns.\n"
            "No print() in library code.\n"
            "Project-specific natural-language review rules:\n"
            "Flag any eval() usage."
        ),
        "log": ["[info] Detected: Python >=3.13"],
        "sources": ["src/pkg/pyproject.toml", "AGENTS.md", ".veritas/rules.md"],
        "origin": "project",
    },
    # .git is a file; the blank src/pkg/AGENTS.md is passed over for the root's STYLE.md.
    "o": {
        "context": (
            "Detected manifest language/framework versions:\n"
            "  - Go 1.22\n"
            "Project conventions to honor:\n"
            "Use four-space indentation.\n"
            "Project-specific natural-language review rules:\n"
            "Never log request bodies."
        ),
        "log": ["[info] Detected: Go 1.22"],
        "sources": ["src/pkg/go.mod", "STYLE.md", ".veritas/rules.md"],
        "origin": "project",
    },
}


@pytest.mark.parametrize("case", sorted(WALK_UP_CASES))
def test_golden_walk_up(case, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    scope, target = WALK_UP_CASES[case](root)
    captured, run = _run_scope_then_context(scope, target)
    expected = WALK_UP_EXPECTED[case]
    assert captured == {"context": expected["context"], "log": expected["log"]}
    assert run.project_context_status is ProjectContextStatus.DETECTED
    assert run.project_context_reason is None
    assert run.project_context_sources == expected["sources"]
    assert run.project_context_origin == expected["origin"]


def test_walk_up_cases_l_and_m_render_exactly_case_b():
    """The hand-written expectation agrees with b's capture from before T116."""
    assert GOLDEN["b"]["context"] == _B_CONTEXT
    assert GOLDEN["b"]["log"] == _B_LOG


def test_no_ancestor_of_tmp_path_is_a_project_root(tmp_path):
    """Guard (T119): the goldens without a marker (case f, and b to k reading
    only their own directory) assume nothing above tmp_path marks a project
    root. If this fails, pytest's temporary directory sits inside a git
    checkout or a directory with .veritas/config.toml (check TMP, TEMP or
    --basetemp), the walk-up would stop there and read that project's files,
    and case f's golden would no longer match. Point the temporary directory
    outside any project."""
    for directory in (tmp_path, *tmp_path.parents):
        assert not os.path.lexists(directory / ".git"), (
            f"{directory} holds .git: the no-marker goldens (case f) depend on no "
            "ancestor of tmp_path being a project root"
        )
        assert not (directory / ".veritas" / "config.toml").is_file(), (
            f"{directory} holds .veritas/config.toml: the no-marker goldens (case f) "
            "depend on no ancestor of tmp_path being a project root"
        )


def test_golden_g_pr_with_an_empty_repository_root():
    """T120: a PR takes the local path; an empty root renders f's note."""
    captured, run = _run_scope_then_context(ReviewScope.PR, PR_TARGET, fetcher=_FakeFetcher())
    assert captured == GOLDEN["g"]
    assert GOLDEN["g"] == GOLDEN["f"]
    assert run.project_context_status is ProjectContextStatus.UNAVAILABLE
    assert run.project_context_reason == "no manifest, conventions file or rules file found"
    assert run.project_context_origin == "repository root at abcd1234"


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


def test_pr_scope_writes_project_sources_from_the_repository_root():
    state = _state(ReviewScope.PR, PR_TARGET)
    sources = make_scope_node(_scope_ctx(fetcher=_FakeFetcher()))(state)["project_sources"]
    assert sources.origin == "repository root at abcd1234"
    assert sources.files == ()
    assert sources.problems == ()


def test_scope_writes_project_sources_for_a_local_scope(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    scope, target = TMP_CASES["c"](root)
    sources = make_scope_node(_scope_ctx())(_state(scope, target))["project_sources"]
    assert sources.origin == str(root)
    assert [name for name, _text in sources.files] == ["package.json", "go.mod"]
    assert sources.problems == ()
