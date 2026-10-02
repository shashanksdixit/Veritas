"""Render node (T042/T053, FR-016/FR-027/FR-028).

Terminal node — the ONLY node allowed to reach write capability
(``veritas.review.tools.write_tools``). Builds the Report from the verified
findings (single source of truth), applies render-time suppression matching,
writes the Markdown file + a machine-readable JSON sidecar, prints the compact
stdout summary, and optionally posts the report as a PR/MR comment (FR-028).
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Callable

from veritas.config.constants import LAST_REPORT_JSON, SCHEMA_VERSION
from veritas.models.entities import (
    CodeFinding,
    Coverage,
    FindingSource,
    Report,
    ReportStatus,
    RequirementFinding,
    ReviewRun,
)
from veritas.output.compact import render_compact
from veritas.output.markdown import render_markdown
from veritas.output.summary import compute_summary
from veritas.review.nodes.common import snippet_for
from veritas.review.state import ReviewState
from veritas.review.tools.write_tools import (
    persist_last_report_json,
    write_report,
)
from veritas.suppression.fingerprint import compute_fingerprint


def _apply_suppressions(
    findings: list[CodeFinding],
    files: dict[str, str],
    suppressions,
) -> list[CodeFinding]:
    if suppressions is None:
        return findings
    entries = suppressions.load()
    if not entries:
        return findings
    by_fingerprint = {entry.fingerprint: entry for entry in entries}
    kept: list[CodeFinding] = []
    for finding in findings:
        snippet = snippet_for(finding, files)
        fingerprint = compute_fingerprint(finding.file, finding.category.value, snippet)
        entry = by_fingerprint.get(fingerprint)
        if entry is not None:
            finding.is_suppressed = True
            finding.suppression_entry = entry
            continue  # excluded from report views (FR-017, US3)
        kept.append(finding)
    return kept


def _finalize_run(state: ReviewState, errors: list[str]) -> ReviewRun:
    run = state["run"]
    if errors:
        return run.model_copy(
            update={
                "report_status": ReportStatus.INCOMPLETE,
                "completed_at": datetime.now(),
                "error": "; ".join(errors),
            }
        )
    return run.model_copy(
        update={"report_status": ReportStatus.COMPLETE, "completed_at": datetime.now()}
    )


def _coverage(state: ReviewState, runtime) -> Coverage | None:
    """Coverage describing the batch plan (FR-029), or None when there is none.

    Coverage describes WHICH code the plan covered (budgets, batches used,
    reviewed/split/not-reviewed paths, and what exclusion withheld). It does not
    describe review success: a batch whose LLM call failed is reported through
    the errors channel and sets an incomplete report status (FR-027), not here.

    Stays None when the scope node produced no plan, so a report from a run
    without coverage data renders exactly as it did before.
    """
    plan = state.get("batch_plan")
    if plan is None:
        return None
    return Coverage(
        batch_chars=runtime.settings.batch_chars,
        max_batches=runtime.settings.max_batches,
        batches_used=len(plan.batches),
        reviewed_files=list(plan.reviewed_files),
        split_files=list(plan.split_files),
        excluded_files=list(state.get("excluded_files", [])),
        not_reviewed_files=list(plan.not_reviewed_files),
    )


def _post_report(runtime, report: Report) -> None:
    host = getattr(runtime, "hosting", None)
    parsed = getattr(runtime, "pr_parsed", None)
    if host is None or parsed is None:
        runtime.log.warn("--post set but no hosting client available; skipped")
        return
    body = report.markdown_content or render_markdown(report)
    try:
        if parsed.provider == "github":
            host.post_comment(parsed.owner, parsed.repo, parsed.number, body)
        else:
            host.post_note(parsed.owner, parsed.repo, parsed.number, body)
        runtime.log.info(f"Report posted as {parsed.provider} comment on {parsed.ref}")
    except Exception as exc:  # noqa: BLE001 - FR-028: posting failure is non-fatal
        runtime.log.warn(
            f"Failed to post report as {parsed.provider} comment: {exc}; "
            f"report saved to {runtime.report_path}"
        )


def make_render_node(runtime) -> Callable[[ReviewState], dict]:
    def node(state: ReviewState) -> dict:
        # FR-013: the verified channels are the only authority on what may be
        # published. They are None when verification never ran, and a list when it
        # did — a list that may legitimately be EMPTY. So the test is `is None`,
        # never truthiness: `or` would read "verification kept nothing" as
        # "verification never ran" and publish the very findings it had rejected.
        withheld_errors: list[str] = []

        verified_code = state.get("verified_code_findings")
        if verified_code is None:
            # Verification did not run, so no non-SAST citation has been grounded.
            # SAST findings are ground truth in their own right (FR-013 exemption
            # by actual source); everything else is withheld and counted, never
            # published unverified.
            all_code = state.get("code_findings", [])
            code = [f for f in all_code if f.source is FindingSource.SAST]
            withheld = len(all_code) - len(code)
            if withheld:
                withheld_errors.append(
                    f"verification did not run; {withheld} unverified finding(s) withheld"
                )
        else:
            code = list(verified_code)

        verified_req = state.get("verified_requirement_findings")
        if verified_req is None:
            # RequirementFindings carry no source and no SAST exemption, so
            # without verification all of them are withheld.
            withheld = len(state.get("requirement_findings", []))
            req: list[RequirementFinding] = []
            if withheld:
                withheld_errors.append(
                    "verification did not run; "
                    f"{withheld} unverified requirement finding(s) withheld"
                )
        else:
            req = list(verified_req)

        run = _finalize_run(state, [*state.get("errors", []), *withheld_errors])

        code = _apply_suppressions(code, state.get("files", {}), runtime.suppressions)

        summary = compute_summary(code, req, state.get("verification_failures", []))
        report = Report(
            schema_version=SCHEMA_VERSION,
            run=run,
            code_findings=code,
            requirement_findings=req,
            summary=summary,
            coverage=_coverage(state, runtime),
        )
        markdown = render_markdown(report)

        path = write_report(runtime.report_path, report, runtime.log)
        persist_last_report_json(report, LAST_REPORT_JSON)

        compact = render_compact(report, report_path=path)
        print(compact, file=sys.stdout, flush=True)

        if runtime.post:
            _post_report(runtime, report)

        return {
            "report_path": str(path),
            "report_markdown": markdown,
            "run": run,
            "phase": "done",
            # The errors channel is additive, so return only the entries added
            # here — returning the merged list would duplicate every earlier one.
            "errors": withheld_errors,
        }

    return node