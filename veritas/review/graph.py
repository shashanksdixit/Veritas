"""LangGraph pipeline (T017/T044) and top-level run entry point.

Graph shape: ``scope → context → [5 review nodes ∥] → verify → render (write) →
END``. Review nodes are guarded (FR-027 partial-review path); the render node is
the ONLY node with write reachability, and there is NO edge leading back into
the review path (constitution Read-Only Safety Boundary). Scoped files are read
only through in-memory state populated by the scope node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langgraph.graph import END, START, StateGraph

from veritas.config.constants import REPORT_PATTERN, SCHEMA_VERSION
from veritas.config.settings import Settings
from veritas.hosting.resolver import UnresolvableTarget, parse_pr_target
from veritas.llm.client import LLMClient, split_model_string
from veritas.llm.zdr import backend_is_openrouter, zdr_warning_text
from veritas.models.entities import ReviewRun, ReviewScope
from veritas.review import ReviewFatalError
from veritas.review.nodes import (  # noqa: F401  (node factories registered below)
    code_quality,
    context,
    performance,
    render,
    requirements,
    scope,
    security,
    test_coverage,
    verification,
)
from veritas.review.nodes.common import current_prompt_version, guarded
from veritas.review.state import ReviewState
from veritas.suppression.store import SuppressionStore
from veritas.utils.logging import Log


@dataclass
class Runtime:
    settings: Settings
    log: Log
    llm: LLMClient
    suppressions: SuppressionStore | None = None
    report_path: str = ""
    post: bool = False
    opengrep_rules: str | None = None
    hosting: Any = None
    pr_parsed: Any = None
    target: str = ""

    def __post_init__(self) -> None:
        if self.suppressions is None:
            self.suppressions = SuppressionStore()


@dataclass
class ReviewOutcome:
    exit_code: int
    report_path: str | None = None


def build_graph(runtime: Runtime):
    """Construct the compiled review graph (structural boundary enforced by
    module imports: write_tools is only imported inside render.py)."""
    graph = StateGraph(ReviewState)

    graph.add_node("scope", scope.make_scope_node(runtime))
    graph.add_node("context", context.make_context_node(runtime))
    graph.add_node(
        "code_quality", guarded(code_quality.make_code_quality_node(runtime))
    )
    graph.add_node("security", guarded(security.make_security_node(runtime)))
    graph.add_node("requirements", guarded(requirements.make_requirements_node(runtime)))
    graph.add_node(
        "test_coverage", guarded(test_coverage.make_test_coverage_node(runtime))
    )
    graph.add_node("performance", guarded(performance.make_performance_node(runtime)))
    graph.add_node("verify", guarded(verification.make_verify_node(runtime)))
    graph.add_node("render", render.make_render_node(runtime))

    graph.add_edge(START, "scope")
    graph.add_edge("scope", "context")
    for review_node in ("code_quality", "security", "requirements", "test_coverage", "performance"):
        graph.add_edge("context", review_node)
        graph.add_edge(review_node, "verify")
    graph.add_edge("verify", "render")
    graph.add_edge("render", END)
    return graph.compile()


def _default_report_path() -> str:
    from datetime import datetime

    return REPORT_PATTERN.format(timestamp=datetime.now().strftime("%Y%m%d-%H%M%S"))


def _initial_state(
    scope_val: ReviewScope,
    target: str,
    run: ReviewRun,
    report_path: str,
) -> dict:
    return {
        "messages": [],
        "scope": scope_val,
        "target": target,
        "code_findings": [],
        "requirement_findings": [],
        # None, not []: these two channels mean "verification produced a verdict".
        # A verdict that kept nothing is an empty list; only None means the
        # verification node never ran, and the render node needs to tell those
        # two apart to publish only grounded findings (FR-013).
        "verified_code_findings": None,
        "verified_requirement_findings": None,
        "verification_failures": [],
        "run": run,
        "phase": "scope",
        "files": {},
        "skipped_languages": [],
        "excluded_files": [],
        "batch_plan": None,
        "test_index": None,
        "project_context": None,
        "degraded_sast": None,
        "sast_findings": [],
        "errors": [],
        "report_path": report_path,
        "report_markdown": None,
    }


def _precheck_local_target(scope_val: ReviewScope, target: str) -> None:
    """Early target existence validation for ad-hoc scopes (US2 edge case).

    Runs before any LLM/provider construction so a mistyped path produces a
    clear diagnostic rather than an unrelated credentials error, and no review
    work has started (contracts/cli.md). Deliberately a lightweight existence
    check only; the scope node performs the full read/walk afterwards.
    """
    from pathlib import Path

    from veritas.review import ReviewNotFoundError

    if scope_val == ReviewScope.FILE:
        if not Path(target).is_file():
            raise ReviewNotFoundError(
                f"target file does not exist or is not readable: {target}"
            )
    else:
        if not Path(target).is_dir():
            raise ReviewNotFoundError(
                f"target directory does not exist or is not readable: {target}"
            )


def _gate_zdr(settings: Settings, log: Log) -> None:
    """Refuse an unenforceable ZDR setup, else warn once — the first thing a run does.

    ZDR is a per-request OpenRouter feature, so on any other backend the setting
    cannot be honoured (FR-021, constitution Privacy & Data Handling v5.1.0). This
    is the first statement in ``run_review`` so that a run configured for ZDR can
    never have fetched a PR, read a file, built a hosting client or called a model
    before the refusal.
    """
    provider, _model_id = split_model_string(settings.model_runtime)
    if not settings.zdr:
        log.warn(zdr_warning_text(settings.base_url, provider))
        return
    if backend_is_openrouter(provider, settings.base_url):
        return
    raise ReviewFatalError(
        "ZDR is only supported with OpenRouter. The configured backend is "
        f"{provider} at {settings.base_url}; zero data retention there depends on "
        "your account agreement with that provider and cannot be enforced per "
        "request. Nothing was sent. Set zdr = false (VERITAS_ZDR=false) if your "
        "account already has zero data retention, or point base_url at "
        "https://openrouter.ai/api/v1."
    )


def run_review(
    settings: Settings,
    scope_val: ReviewScope,
    target: str,
    *,
    output: str | None = None,
    post: bool = False,
    verbose: bool = False,
    llm: Any = None,
) -> ReviewOutcome:
    """Execute a full review and return exit code + report path.

    ``llm`` may be injected in tests; defaults to a real LLMClient built from
    settings.
    """
    log = Log(verbose=verbose)
    report_path = output or _default_report_path()

    # Before anything is fetched, read, or called (FR-021).
    _gate_zdr(settings, log)

    if scope_val == ReviewScope.PR:
        try:
            parsed = parse_pr_target(target)
        except UnresolvableTarget as exc:
            raise ReviewFatalError(str(exc)) from exc
    else:
        parsed = None
        _precheck_local_target(scope_val, target)

    if post and scope_val != ReviewScope.PR:
        raise ReviewFatalError(
            "--post is only valid with --scope pr (FR-028); rejected before any review work starts."
        )

    if llm is None:
        try:
            llm = LLMClient(settings, log)
        except Exception as exc:  # noqa: BLE001 - surface provider-config failures clearly
            raise ReviewFatalError(
                f"failed to initialize the LLM client for {settings.model_runtime}: {exc}"
            ) from exc
    runtime = Runtime(
        settings=settings,
        log=log,
        llm=llm,
        report_path=report_path,
        post=post,
        pr_parsed=parsed,
    )
    runtime.target = target

    run = ReviewRun(
        scope=scope_val,
        target=target,
        config_hash=settings.config_hash,
        model_name=llm.model_name,
        prompt_version=current_prompt_version(),
        started_at=datetime.now(),
    )

    compiled = build_graph(runtime)
    initial = _initial_state(scope_val, target, run, report_path)
    result = compiled.invoke(initial)

    final_run: ReviewRun = result["run"]
    errors = result.get("errors", [])
    exit_code = 2 if final_run.report_status.value == "incomplete" and errors else 0
    if errors and not final_run.error:
        # render finalizes run with the error text; keep exit code consistent
        exit_code = 2
    return ReviewOutcome(exit_code=exit_code, report_path=result.get("report_path"))