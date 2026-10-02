"""Security/OWASP review node (T037, FR-012).

Sources:
  * SAST findings (OpenGrep raw results collected at scope time) → ground truth,
    ``source=FindingSource.SAST``.
  * LLM-identified findings (business logic, access control, etc.) →
    ``source=FindingSource.LLM_IDENTIFIED`` and MUST pass FR-013 verification.
Every finding is redacted before entering state.
"""

from __future__ import annotations

from typing import Callable

from veritas.models.entities import Category, CodeFinding, FindingSource
from veritas.review.nodes.common import build_code_finding, llm_findings
from veritas.review.state import ReviewState
from veritas.security.opengrep import result_to_finding


def _sast_findings(raw_results: list[dict], files: dict[str, str]) -> list[CodeFinding]:
    seen: set[tuple[str, str]] = set()
    findings: list[CodeFinding] = []
    for raw in raw_results:
        flat = result_to_finding(raw)
        key = (flat["file"], f"{flat['start_line']}-{flat.get('cwe_id') or flat.get('owasp_id') or flat['title']}")
        if key in seen or flat["file"] not in files:
            continue
        seen.add(key)
        findings.append(
            build_code_finding(flat, category=Category.SECURITY, source=FindingSource.SAST)
        )
    return findings


def _sast_brief(findings: list[CodeFinding]) -> str:
    if not findings:
        return "OpenGrep (SAST) produced no findings."
    lines = [f"- {item.file}:{item.line_range.start_line} [{item.severity.value}] {item.title}" for item in findings]
    return "OpenGrep (SAST) findings already flagged (do NOT re-report):\n" + "\n".join(lines)


def make_security_node(runtime) -> Callable[[ReviewState], dict]:
    def security_node(state: ReviewState) -> dict:
        files = state["files"]
        sast = _sast_findings(state["sast_findings"], files)
        runtime.log.info(f"security: {len(sast)} SAST findings")

        llm_raw, errors = llm_findings(
            runtime.llm,
            state["batch_plan"],
            "security",
            state["project_context"],
            category=Category.SECURITY,
            source=FindingSource.LLM_IDENTIFIED,
            extra=_sast_brief(sast),
            log=runtime.log,
        )
        sast_keys = {(f.file, f.line_range.start_line) for f in sast}
        llm = [f for f in llm_raw if (f.file, f.line_range.start_line) not in sast_keys]
        runtime.log.info(f"security: {len(llm)} LLM-identified findings")

        combined = sast + llm
        if state.get("degraded_sast"):
            runtime.log.warn(
                f"Security coverage degraded: {state['degraded_sast']}; "
                "LLM-identified findings remain subject to re-verification."
            )
        # errors rides the shared FR-027 channel: any recorded error makes the
        # run's report status incomplete.
        return {"code_findings": combined, "errors": errors}

    return security_node