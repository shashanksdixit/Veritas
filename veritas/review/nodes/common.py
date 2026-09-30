"""Shared helpers for review nodes (prompt loading, LLM JSON parsing, finding
construction, redaction). Kept read-only: no write/execute capability here."""

from __future__ import annotations

import json
from pathlib import Path

from veritas.config.constants import PROMPT_VERSION, REQUIREMENTS_SOURCES
from veritas.models.entities import (
    Category,
    CodeFinding,
    FindingSource,
    LineRange,
    RequirementFinding,
    RequirementStatus,
    Severity,
)
from veritas.utils.redaction import redact_secrets

_PROMPT_DIR = Path(__file__).parent.parent / "prompts"

_SOURCE_LABEL: dict[FindingSource, str] = {
    FindingSource.SAST: "sast",
    FindingSource.LLM_IDENTIFIED: "llm-verified",
}


def load_prompt(name: str) -> str:
    """Read a prompt file, discarding the ``prompt_version:`` header line."""
    path = _PROMPT_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and lines[0].startswith("prompt_version:"):
        return "\n".join(lines[1:]).strip()
    return text.strip()


def current_prompt_version() -> str:
    """Prompt set version (FR-024). Reads the first prompt header, else fallback."""
    try:
        for path in sorted(_PROMPT_DIR.glob("*.md")):
            first = path.read_text(encoding="utf-8").splitlines()
            if first and first[0].startswith("prompt_version:"):
                return first[0].split(":", 1)[1].strip()
    except OSError:
        pass
    return PROMPT_VERSION


def parse_json_array(text: str) -> list[dict]:
    """Tolerantly parse a JSON array from an LLM response."""
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _severity(value: str) -> Severity:
    try:
        return Severity(value)
    except ValueError:
        return Severity.WARNING


def _category(value: str) -> Category:
    for category in Category:
        if value and value.lower() in (category.value, category.value.replace("_", "")):
            return category
    return Category.CODE_QUALITY


def __status(value: str) -> RequirementStatus:
    try:
        return RequirementStatus(value)
    except ValueError:
        return RequirementStatus.UNCLEAR


def build_code_finding(
    payload: dict,
    *,
    category: Category,
    source: FindingSource | None = None,
) -> CodeFinding:
    """Construct a CodeFinding from an LLM/SAST payload, applying redaction."""
    line_range = LineRange(
        start_line=_as_int(payload.get("start_line", payload.get("line", 1)), 1),
        start_col=_as_int(payload.get("start_col", 1), 1),
        end_line=_as_int(payload.get("end_line", payload.get("start_line", 1)), 1),
        end_col=_as_int(payload.get("end_col", 1), 1),
    )
    kwargs: dict = {
        "file": str(payload.get("file", "")).strip(),
        "line_range": line_range,
        "severity": _severity(str(payload.get("severity", "warning"))),
        "category": category,
        "source": source,
        "owasp_id": str(payload["owasp_id"]) if payload.get("owasp_id") else None,
        "cwe_id": str(payload["cwe_id"]) if payload.get("cwe_id") else None,
        "title": str(payload.get("title", "Finding")).strip(),
        "description": str(payload.get("description", "")).strip(),
        "recommendation": str(payload.get("recommendation", "")).strip(),
        "confidence": _as_float(payload.get("confidence", 0.5), 0.5),
        "cited_snippet": str(payload["cited_snippet"]) if payload.get("cited_snippet") else None,
    }
    if payload.get("id"):
        kwargs["id"] = str(payload["id"])
    finding = CodeFinding(**kwargs)
    finding.title = redact_secrets(finding.title)
    finding.description = redact_secrets(finding.description)
    finding.recommendation = redact_secrets(finding.recommendation)
    if finding.cited_snippet:
        finding.cited_snippet = redact_secrets(finding.cited_snippet)
    return finding


def build_requirement_finding(payload: dict) -> RequirementFinding:
    """Construct a RequirementFinding from an LLM payload, applying redaction."""
    evidence = [str(ref).strip() for ref in payload.get("evidence", []) if str(ref).strip()]  # type: ignore[union-attr]
    finding = RequirementFinding(
        requirement_ref=str(payload.get("requirement_ref", "")).strip(),
        requirement_text=redact_secrets(str(payload.get("requirement_text", "")).strip()),
        status=__status(str(payload.get("status", "unclear"))),
        evidence=evidence,
        explanation=redact_secrets(str(payload.get("explanation", "")).strip()),
    )
    return finding


def snippet_for(finding: CodeFinding, files: dict[str, str]) -> str:
    """Resolve the flagged-code snippet used for fingerprinting (FR-017).

    Prefers the finding's ``cited_snippet``; falls back to the real file
    content at the cited line range so suppression is keyed to actual code.
    """
    if finding.cited_snippet:
        return finding.cited_snippet
    content = files.get(finding.file, "")
    if not content:
        return finding.cited_snippet or ""
    lines = content.splitlines()
    start = max(1, finding.line_range.start_line)
    end = max(start, finding.line_range.end_line if finding.line_range.end_line >= start else start)
    return "\n".join(lines[start - 1 : end])


_BLOCK_SEPARATOR = "\n\n"
_TRUNCATION_MARKER = "…(truncated)"


def _numbered_file_block(path: str, content: str, budget: int) -> str | None:
    """Render one file as a header plus numbered lines, fitting ``budget``.

    Each line is rendered as ``{n:>5}| {line}`` with 1-based ``n`` so the
    reviewer copies citation line numbers rather than estimating them
    (FR-014). Truncation only ever drops whole trailing lines, so no line is
    ever split. Returns ``None`` when ``budget`` cannot hold the header, at
    least one numbered line and the truncation marker: a file that cannot be
    shown usefully is omitted rather than shown misleadingly.
    """
    header = f"### FILE: {path}"
    lines = [f"{n:>5}| {line}" for n, line in enumerate(content.splitlines(), start=1)]
    if not lines:
        return header if len(header) <= budget else None
    whole = "\n".join([header, *lines])
    if len(whole) <= budget:
        return whole
    used = len(header) + 1
    kept: list[str] = []
    for line in lines:
        if used + len(line) + 1 + len(_TRUNCATION_MARKER) > budget:
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:
        return None
    return "\n".join([header, *kept, _TRUNCATION_MARKER])


def code_package(files: dict[str, str], *, max_files: int = 12, max_chars: int = 24_000) -> str:
    """Build a bounded, line-numbered concatenation of scoped file contents.

    The returned string — headers, separators and truncation markers included —
    never exceeds ``max_chars`` (FR-014).
    """
    parts: list[str] = []
    used = 0
    for path in sorted(files):
        if len(parts) >= max_files:
            break
        separator = len(_BLOCK_SEPARATOR) if parts else 0
        block = _numbered_file_block(path, files[path], max_chars - used - separator)
        if block is None:
            break
        parts.append(block)
        used += separator + len(block)
    return _BLOCK_SEPARATOR.join(parts)


def llm_findings(
    llm,
    prompt_name: str,
    files: dict[str, str],
    context: str | None,
    *,
    category: Category,
    source: FindingSource | None = None,
    extra: str = "",
) -> list[CodeFinding]:
    """Drive the LLM for a code-findings review type and parse results."""
    sys_prompt = load_prompt(prompt_name)
    user = ""
    if context:
        user += f"{context}\n\n"
    if extra:
        user += f"{extra}\n\n"
    user += f"Code to review:\n\n{code_package(files)}"
    text = llm.complete(sys_prompt, user)
    payloads = parse_json_array(text)
    findings = [
        build_code_finding(raw, category=category, source=source)
        for raw in payloads
    ]
    return [f for f in findings if f.file in files]


def llm_requirement_findings(
    llm,
    requirements_source: str | None,
    files: dict[str, str],
    context: str | None,
) -> list[RequirementFinding]:
    """Drive the LLM for requirements traceability and parse results."""
    sys_prompt = load_prompt("requirements")
    user = ""
    if context:
        user += f"{context}\n\n"
    if requirements_source:
        user += f"Requirements documentation:\n\n{requirements_source}\n\n"
    else:
        user += "No requirements documentation was found in the project scope.\n\n"
    user += f"Code to review:\n\n{code_package(files)}"
    text = llm.complete(sys_prompt, user)
    payloads = parse_json_array(text)
    return [build_requirement_finding(raw) for raw in payloads]


def grounding_filter(finding: CodeFinding, files: dict[str, str]) -> bool:
    """True if the finding cites a file present in the reviewed scope (FR-013)."""
    return finding.file in files


def find_requirements_source(files: dict[str, str]) -> str | None:
    """Return the first requirements documentation found in scope (FR-008)."""
    for candidate in REQUIREMENTS_SOURCES:
        if candidate in files:
            text = files[candidate]
            if text.strip():
                return text[:16_000]
    return None


def guarded(node) -> callable:
    """Wrap a review-type node so a mid-run LLM/provider failure becomes a
    partial-review error (FR-027) rather than aborting the run."""

    def wrapper(state):
        try:
            return node(state)
        except Exception as exc:  # noqa: BLE001 - FR-027 partial-review path
            return {"errors": [f"{node.__name__}: {exc}"]}

    wrapper.__name__ = f"guarded_{getattr(node, '__name__', 'node')}"
    return wrapper