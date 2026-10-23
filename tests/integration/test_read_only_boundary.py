"""Read-only boundary, verified by reachability (T112, FR-026).

``tests/boundary_walker.py`` walks everything a node's function can reach and
reports each write or execute capability with the path that reached it. The
first half of this file proves the walker on small setups, one per kind of
path, and proves each gap rule (G1-G3) is what catches its case by switching
it off. The second half walks the real graph, built the way ``run_review``
builds it with ``--post`` on, and asserts that only the render node can write
and that the scope node's only execute route is OpenGrep.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import subprocess
import types
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
from langgraph._internal._runnable import RunnableCallable
from langgraph.graph import END, StateGraph
from langgraph.graph._node import StateNodeSpec

import veritas.review.nodes.scope as scope_module
import veritas.security.opengrep as opengrep_module
from tests.boundary_walker import walk
from veritas.config.settings import Settings
from veritas.hosting.github import GitHubFetcher, GitHubPoster
from veritas.hosting.resolver import parse_pr_target
from veritas.llm.client import LLMClient
from veritas.review.contexts import RenderContext, ReviewContext, ScopeContext
from veritas.review.graph import build_graph
from veritas.review.tools import write_tools
from veritas.review.tools.write_tools import persist_last_report_json
from veritas.security import sast_results
from veritas.suppression.store import SuppressionStore
from veritas.utils.logging import Log

# This module deliberately has no global named ``write_report``: the G1 and G3
# setups reach that name only as an attribute or a function-body import, so a
# global of the same name would let the plain global lookup find it and the
# switched-off runs would no longer prove anything.

# --- walker self-test setups ---------------------------------------------------


def _sink(*args, **kwargs):
    return None


class _Box:
    def __init__(self, item):
        self.item = item

    def get(self):
        return None


@dataclass(frozen=True, slots=True)
class _Frozen:
    item: object


class _Pair(NamedTuple):
    item: object


class _FakeNoter:
    def post_note(self, *args):
        return None


class _WithMethod:
    def run(self):
        return persist_last_report_json


class _WithProperty:
    @property
    def thing(self):
        return persist_last_report_json


def _closing_over(value):
    def root():
        return value

    return root


def _with_default(value):
    def root(arg=value):
        return arg

    return root


def _global_function_root():
    return persist_last_report_json


def _class_object_root():
    return SuppressionStore


def _subprocess_root():
    return subprocess.run


# A module object (not write_tools itself, which W3 flags on sight) holding a
# write callable, so only G1's attribute step can find it.
_FAKE_MODULE = types.ModuleType("boundary_fake_writer")
_FAKE_MODULE.write_report = write_tools.write_report


def _g1_module_function_root():
    return _FAKE_MODULE.write_report


def _g1_os_system_root():
    return os.system


def _g2_lambda_root():
    return lambda: persist_last_report_json


def _g2_nested_def_root():
    def inner():
        return persist_last_report_json

    return inner


def _g3_import_root():
    from veritas.review.tools.write_tools import write_report

    return write_report


def _pure_module_root():
    return sast_results.result_to_finding


def _os_path_root():
    return os.path.join


def _path_class_root():
    return Path


@pytest.fixture
def store(tmp_path: Path) -> SuppressionStore:
    return SuppressionStore(tmp_path / "suppressions.yaml")


SINGLE_HIT_CASES = {
    "closure cell": (
        lambda store: _closing_over(store),
        "W1",
        "root -> closure[value]",
    ),
    "partial arg": (
        lambda store: functools.partial(_sink, store),
        "W1",
        "root -> partial.args[0]",
    ),
    "partial keyword": (
        lambda store: functools.partial(_sink, target=store),
        "W1",
        "root -> partial.keywords[target]",
    ),
    "bound __self__": (
        lambda store: _Box(store).get,
        "W1",
        "root -> __self__ -> .item",
    ),
    "a.b nesting": (
        lambda store: _closing_over(_Box(_Box(store))),
        "W1",
        "root -> closure[value] -> .item -> .item",
    ),
    "frozen-slots field": (
        lambda store: _closing_over(_Frozen(store)),
        "W1",
        "root -> closure[value] -> .item",
    ),
    "NamedTuple field": (
        lambda store: _closing_over(_Pair(store)),
        "W1",
        "root -> closure[value] -> .item",
    ),
    "dict value": (
        lambda store: _closing_over({"key": store}),
        "W1",
        "root -> closure[value] -> ['key']",
    ),
    "default argument": (
        lambda store: _with_default(store),
        "W1",
        "root -> default[0]",
    ),
    "global function": (
        lambda store: _global_function_root,
        "W2",
        "root -> global:persist_last_report_json",
    ),
    "W1 class object": (
        lambda store: _class_object_root,
        "W1",
        "root -> global:SuppressionStore",
    ),
    "W4 fake with post_note": (
        lambda store: _closing_over(_FakeNoter()),
        "W4",
        "root -> closure[value]",
    ),
    "method on a reached instance's class": (
        lambda store: _closing_over(_WithMethod()),
        "W2",
        "root -> closure[value] -> _WithMethod.run -> global:persist_last_report_json",
    ),
    "property on a reached instance's class": (
        lambda store: _closing_over(_WithProperty()),
        "W2",
        "root -> closure[value] -> _WithProperty.thing.fget -> global:persist_last_report_json",
    ),
    "X1 subprocess": (
        lambda store: _subprocess_root,
        "X1",
        "root -> global:subprocess",
    ),
}

# Each gap case: (root, rule, path, the gap rule whose fix catches it).
GAP_CASES = {
    "G1 module.function": (
        _g1_module_function_root,
        "W2",
        "root -> global:_FAKE_MODULE -> .write_report",
        "g1",
    ),
    "G1 os.system": (
        _g1_os_system_root,
        "X1",
        "root -> global:os -> .system",
        "g1",
    ),
    "G2 lambda": (
        _g2_lambda_root,
        "W2",
        "root -> <code _g2_lambda_root.<locals>.<lambda>> -> global:persist_last_report_json",
        "g2",
    ),
    "G2 nested def": (
        _g2_nested_def_root,
        "W2",
        "root -> <code _g2_nested_def_root.<locals>.inner> -> global:persist_last_report_json",
        "g2",
    ),
    "G3 import inside a body": (
        _g3_import_root,
        "W2",
        "root -> from veritas.review.tools.write_tools import write_report",
        "g3",
    ),
}


def _found(result) -> list[tuple[str, str]]:
    return [(hit.rule, hit.path) for hit in result.hits]


@pytest.mark.parametrize("case", list(SINGLE_HIT_CASES))
def test_walker_finds_exactly_one_hit_per_path_kind(case: str, store: SuppressionStore) -> None:
    make_root, rule, path = SINGLE_HIT_CASES[case]
    assert _found(walk(make_root(store))) == [(rule, path)]


def test_walker_flags_a_client_without_the_read_only_hook() -> None:
    client = httpx.Client()
    try:
        assert _found(walk(_closing_over(client))) == [("W5", "root -> closure[value]")]
    finally:
        client.close()


@pytest.mark.parametrize("case", list(GAP_CASES))
def test_gap_rule_catches_its_case(case: str) -> None:
    root, rule, path, _gap = GAP_CASES[case]
    assert _found(walk(root)) == [(rule, path)]


@pytest.mark.parametrize("case", list(GAP_CASES))
def test_gap_case_is_missed_with_its_fix_switched_off(case: str) -> None:
    root, _rule, _path, gap = GAP_CASES[case]
    assert _found(walk(root, **{gap: False})) == []


@pytest.mark.parametrize(
    "root",
    [_pure_module_root, _os_path_root, _path_class_root],
    ids=["pure helper module", "os.path", "Path class"],
)
def test_walker_clean_setups_have_no_hits(root) -> None:
    assert _found(walk(root)) == []


def test_walker_clean_for_a_fetcher_with_the_get_only_hook() -> None:
    fetcher = GitHubFetcher("test-token")
    try:
        result = walk(_closing_over(fetcher))
        assert _found(result) == []
        # The walk did enter the fetcher: it reached the hooked client.
        assert result.reaches(fetcher.client)
    finally:
        fetcher.close()


# --- the real graph -------------------------------------------------------------

EXPECTED_NODES = {
    "scope",
    "context",
    "code_quality",
    "security",
    "requirements",
    "test_coverage",
    "performance",
    "verify",
    "render",
}
LLM_NODES = ("context", "code_quality", "security", "requirements", "test_coverage", "performance")
ANALYSIS_NODES = LLM_NODES + ("verify",)

# The scope node may execute OpenGrep (FR-012); these two routes, and only
# these, are allowed. Each X1 hit stops the walk, so a second route to either
# module is not listed and anything else is a new hit.
PINNED_SCOPE_X1 = {
    ("X1", "scope -> global:collect_sast -> global:shutil", "module shutil"),
    (
        "X1",
        "scope -> global:collect_sast -> global:run_opengrep -> global:subprocess",
        "module subprocess",
    ),
}


@pytest.fixture(autouse=True)
def _no_real_sast():
    """Override the integration conftest's autouse stub of ``scope.collect_sast``.

    This module never runs a node; it walks the production code, so the scope
    node must hold the real OpenGrep entry point, not the test double.
    """
    yield


def test_scope_holds_the_real_collect_sast() -> None:
    assert scope_module.collect_sast is opengrep_module.collect_sast


def _build(tmp_path: Path):
    """Build the graph the way ``run_review`` does for ``--scope pr --post``."""
    settings = Settings(api_key="test-key")
    log = Log()
    llm = LLMClient(settings, log)  # builds the chat model; no network call
    fetcher = GitHubFetcher("test-token", log)
    poster = GitHubPoster("test-token", log)
    pr = parse_pr_target("https://github.com/owner/repo/pull/1")
    review_ctx = ReviewContext(llm=llm, log=log)
    scope_ctx = ScopeContext(
        log=log,
        exclude=tuple(settings.exclude),
        batch_chars=settings.batch_chars,
        max_batches=settings.max_batches,
        opengrep_rules=settings.opengrep_rules,
        opengrep_jobs=settings.opengrep_jobs,
        fetcher=fetcher,
        pr=pr,
    )
    render_ctx = RenderContext(
        log=log,
        report_path=str(tmp_path / "report.md"),
        batch_chars=settings.batch_chars,
        max_batches=settings.max_batches,
        store=SuppressionStore(tmp_path / "suppressions.yaml"),
        poster=poster,  # --post on
        pr=pr,
    )
    compiled = build_graph(review_ctx, scope_ctx, render_ctx)
    return compiled, fetcher, poster


@pytest.fixture
def built(tmp_path: Path):
    compiled, fetcher, poster = _build(tmp_path)
    yield compiled, fetcher
    fetcher.close()
    poster.close()


@pytest.fixture
def review_graph(built):
    return built[0]


def _describe(hits) -> str:
    """Every hit on its own line, so a failure shows all of them untruncated."""
    lines = [f"  {hit.rule}: {hit.path}  ({hit.detail})" for hit in hits]
    return "reachable write/execute capabilities:\n" + "\n".join(lines)


def _walk_node(compiled, name: str):
    return walk(compiled.builder.nodes[name].runnable.func, name)


def test_langgraph_internals_the_walk_relies_on(review_graph) -> None:
    """Fails loudly if a LangGraph upgrade moves what the walk starts from."""
    field_names = {f.name for f in dataclasses.fields(StateNodeSpec)}
    assert {"runnable", "metadata", "ends"} <= field_names
    builder = review_graph.builder
    assert isinstance(builder, StateGraph)
    assert isinstance(builder.edges, set)
    assert isinstance(builder.waiting_edges, set)
    assert isinstance(builder.branches, dict)
    assert set(builder.nodes) == EXPECTED_NODES
    for name, spec in builder.nodes.items():
        assert isinstance(spec, StateNodeSpec)
        assert isinstance(spec.runnable, RunnableCallable), name
        assert callable(spec.runnable.func), name
        assert review_graph.nodes[name].metadata == spec.metadata, name


def test_node_roles_come_from_metadata(review_graph) -> None:
    def with_role(role: str) -> set[str]:
        return {
            name
            for name, spec in review_graph.builder.nodes.items()
            if (spec.metadata or {}).get("veritas_role") == role
        }

    assert with_role("output") == {"render"}
    assert with_role("input") == {"scope"}


def test_render_only_edge_is_to_end(review_graph) -> None:
    builder = review_graph.builder
    assert {edge for edge in builder.edges if edge[0] == "render"} == {("render", END)}
    assert not any("render" in starts for starts, _end in builder.waiting_edges)
    assert "render" not in builder.branches
    assert builder.nodes["render"].ends == ()


@pytest.mark.parametrize("name", ANALYSIS_NODES)
def test_analysis_nodes_reach_no_write_or_execute_capability(review_graph, name: str) -> None:
    hits = _walk_node(review_graph, name).hits
    assert not hits, _describe(hits)


def test_scope_node_reaches_no_write_capability_and_only_the_pinned_opengrep_routes(
    built,
) -> None:
    compiled, fetcher = built
    result = _walk_node(compiled, "scope")
    found = {(hit.rule, hit.path, hit.detail) for hit in result.hits}
    assert found == PINNED_SCOPE_X1, _describe(result.hits)
    # The walk did enter the scope context: it reached the fetcher's hooked client.
    assert result.reaches(fetcher.client)


@pytest.mark.parametrize("name", LLM_NODES)
def test_llm_nodes_reach_the_llm_client(review_graph, name: str) -> None:
    """Positive check: the walk really enters each node's context."""
    result = _walk_node(review_graph, name)
    assert result.reaches(LLMClient.complete)
    assert [hit.rule for hit in result.exceptions] == ["E1", "E2"]


def test_verify_node_reaches_only_the_log(review_graph) -> None:
    """verify reads ``ctx.log`` once and keeps only that: it never calls the model.

    The positive check for it is the log it closes over, and it must not reach
    the LLM client at all.
    """
    result = _walk_node(review_graph, "verify")
    assert result.reaches(Log.warn)
    assert not result.reaches(LLMClient.complete)
    assert [(hit.rule, hit.path) for hit in result.exceptions] == [
        ("E1", "verify -> closure[node] -> closure[_warn] -> closure[log] -> .stream")
    ]


def test_walker_sees_the_render_node_write(review_graph) -> None:
    """The output node is where the writes are, and the walk sees them."""
    found = {(hit.rule, hit.path) for hit in _walk_node(review_graph, "render").hits}
    assert ("W1", "render -> closure[ctx] -> .store") in found
    assert ("W1", "render -> closure[ctx] -> .poster") in found
    assert ("W2", "render -> global:write_report") in found
