"""Shared helpers for review nodes (prompt loading, LLM JSON parsing, finding
construction, redaction). Kept read-only: no write/execute capability here."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:  # batching imports this module's renderers; import type-only
    from veritas.review.batching import BatchPlan
    from veritas.review.requirements_source import Requirement

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


def file_header(
    path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    total_lines: int | None = None,
) -> str:
    """Render a file block header, or a chunk range header for a split file.

    A whole file is ``### FILE: {path}``. A chunk of a file too large for one
    batch carries its original line range: ``### FILE: {path} (lines {s}-{e} of
    {total})``, so the reviewer cites real line numbers (FR-014, FR-029).
    """
    if start_line is None or end_line is None or total_lines is None:
        return f"### FILE: {path}"
    return f"### FILE: {path} (lines {start_line}-{end_line} of {total_lines})"


def numbered_lines(lines: Sequence[str], start_line: int = 1) -> list[str]:
    """Render lines as ``{n:>5}| {line}`` with 1-based ``n`` (FR-014).

    ``start_line`` is the ORIGINAL line number of ``lines[0]``, so a chunk of a
    split file keeps the numbers the reviewer must cite.
    """
    return [f"{n:>5}| {line}" for n, line in enumerate(lines, start=start_line)]


# The ``{n:>5}| `` prefix above: optional spaces, one or more digits, the bar,
# then at most the single space that separates it from the code.
_LINE_NUMBER_PREFIX = re.compile(r"^ *\d+\| ?")


def strip_line_number_prefixes(snippet: str) -> tuple[str, bool]:
    """Remove the review prompts' line-number prefixes from a cited snippet.

    Review code is shown line-numbered, and models copy that format straight
    into ``cited_snippet`` — ``"   21| def f():"``. The prefix is not in the
    file, so verification would reject an otherwise real citation for the
    prefix alone. Stripping where the finding is built means verification,
    state, the report and the suppression fingerprint all see the code the
    reviewer meant (FR-014).

    All or nothing per snippet: prefixes come off only when EVERY non-blank
    line carries one, because a half-strip would mangle real code (a snippet
    like ``x = a | b`` is not line-numbered). Blank and whitespace-only lines
    carry no prefix, so they are kept as empty lines and the remaining lines
    keep their positions. Indentation after the prefix is preserved.

    The removed numbers are ignored — the finding's cited line range, not the
    prefix, says where the code is — so ``"   99| def f():"`` cleans to
    ``"def f():"`` and is then located by verification as usual.

    Returns ``(cleaned, True)`` when prefixes were removed, else the snippet
    unchanged with ``False``.
    """
    lines = snippet.splitlines()
    if not any(line.strip() for line in lines):
        return (snippet, False)
    cleaned: list[str] = []
    for line in lines:
        if not line.strip():
            cleaned.append("")
            continue
        match = _LINE_NUMBER_PREFIX.match(line)
        if match is None:
            return (snippet, False)
        cleaned.append(line[match.end() :])
    return ("\n".join(cleaned), True)


def _numbered_file_block(path: str, content: str, budget: int) -> str | None:
    """Render one file as a header plus numbered lines, fitting ``budget``.

    Each line is rendered as ``{n:>5}| {line}`` with 1-based ``n`` so the
    reviewer copies citation line numbers rather than estimating them
    (FR-014). Truncation only ever drops whole trailing lines, so no line is
    ever split. Returns ``None`` when ``budget`` cannot hold the header, at
    least one numbered line and the truncation marker: a file that cannot be
    shown usefully is omitted rather than shown misleadingly.
    """
    header = file_header(path)
    lines = numbered_lines(content.splitlines())
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


def _batch_paths(chunks) -> list[str]:
    """Distinct paths in a batch, in first-appearance order.

    A batch holds at most one chunk per file today, so this is normally the
    chunk paths themselves; distincting keeps the log and the error message
    accurate if a future planner ever puts two chunks of one file in a batch.
    """
    return list(dict.fromkeys(chunk.path for chunk in chunks))


def llm_findings(
    llm,
    plan: BatchPlan | None,
    prompt_name: str,
    context: str | None,
    *,
    category: Category,
    log,
    source: FindingSource | None = None,
    extra: str = "",
    batch_extra: dict[int, str] | None = None,
) -> tuple[list[CodeFinding], list[str]]:
    """Drive the LLM for a code-findings review type over the shared batch plan.

    The scope node planned the batches once and every code review type drives the
    same plan, so all four see identical code (FR-029). Each batch is one LLM
    call whose user message is the usual context/extra prefix followed by that
    batch's text.

    ``batch_extra`` carries per-batch context keyed by ``Batch.index`` (1-based) -
    the test-coverage review uses it for that batch's test index (FR-004), since
    which tests matter depends on which code is in the batch. The entry for the
    batch being sent goes after ``extra`` and before the code; a batch with no
    entry, and every caller that passes nothing, send the message unchanged.

    No payload is filtered on the way out. Whether a citation is real is decided
    once, by the verification node, which re-reads the cited file from the full
    scoped contents and records a ``VerificationFailure`` for a citation it
    cannot confirm. Filtering here would drop such a finding silently, leaving
    no record that it was ever made (FR-013).

    Per-batch failure isolation: a failure in one batch — anywhere between the
    LLM call and building its findings — is recorded as an error and the
    remaining batches still run, so a provider error loses one batch of review
    rather than the whole review type. The error text is redacted before it is
    recorded or logged (constitution Privacy & Data Handling).

    Returns ``(findings, errors)``; the caller routes ``errors`` into the shared
    errors channel, which makes the run's report status incomplete (FR-027).
    """
    sys_prompt = load_prompt(prompt_name)
    batches = list(plan.batches) if plan is not None else []
    if not batches:
        # No source files in scope, or every file was dropped by the planner.
        if log is not None:
            log.info(f"{prompt_name}: no source files to review")
        return ([], [])

    prefix = ""
    if context:
        prefix += f"{context}\n\n"
    if extra:
        prefix += f"{extra}\n\n"

    total = len(batches)
    findings: list[CodeFinding] = []
    errors: list[str] = []
    for position, batch in enumerate(batches, start=1):
        paths = _batch_paths(batch.chunks)
        if log is not None:
            # Logged before the call so a hung or slow batch is identifiable.
            log.info(f"{prompt_name}: batch {position}/{total} ({len(paths)} file(s))")
        try:
            batch_note = ""
            if batch_extra is not None:
                # Assembled per batch, so a lookup miss or a caller that passes
                # nothing leaves the message exactly as it was.
                note = batch_extra.get(batch.index)
                if note:
                    batch_note = f"{note}\n\n"
            text = llm.complete(sys_prompt, prefix + batch_note + "Code to review:\n\n" + batch.text)
            payloads = parse_json_array(text)
            # Every payload the LLM returns becomes a finding. Nothing is
            # filtered here: a finding must never be dropped silently (FR-013).
            # Verification is the single authority on whether a citation is real,
            # and it records a finding citing an out-of-scope file as a
            # file_not_in_scope failure instead of discarding it. It also checks
            # an in-scope file against its full content, not just the batch the
            # finding happened to be produced from, so a citation into another
            # batch of the same file still verifies.
            #
            # The cited snippet is cleaned of the prompts' line-number prefixes
            # first, so a reviewer who quoted the code as it was shown is not
            # rejected for the quoting (FR-014). Each removal is counted and
            # logged: silently rewriting what the model said would be as
            # dishonest as dropping it.
            batch_findings: list[CodeFinding] = []
            cleaned_count = 0
            for raw in payloads:
                payload = dict(raw)
                cited = payload.get("cited_snippet")
                if cited:
                    cleaned, stripped = strip_line_number_prefixes(str(cited))
                    if stripped:
                        payload["cited_snippet"] = cleaned
                        cleaned_count += 1
                batch_findings.append(
                    build_code_finding(payload, category=category, source=source)
                )
            if cleaned_count > 0 and log is not None:
                log.info(
                    f"{prompt_name}: batch {position}/{total}: removed line-number "
                    f"prefixes from {cleaned_count} snippet(s)"
                )
            findings.extend(batch_findings)
        except Exception as exc:  # noqa: BLE001 - isolate one batch, keep going
            error = redact_secrets(
                f"{prompt_name}: batch {position}/{total} failed "
                f"(files: {', '.join(paths)}): {exc}"
            )
            errors.append(error)
            if log is not None:
                log.warn(error)
    return (findings, errors)


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


def llm_requirement_answers(
    llm,
    plan: BatchPlan | None,
    requirements: list[Requirement],
    context: str | None,
    *,
    log,
) -> tuple[list[dict], list[str]]:
    """Ask each batch how it answers every structured requirement (FR-007).

    One call per batch of the shared plan, mirroring :func:`llm_findings`: the same
    per-batch log line before the call, the same per-batch failure isolation, and
    the same redacted error text, so a provider error costs one batch of
    requirement coverage rather than the whole review. Errors come back for the
    caller to route into the shared errors channel (FR-027) *and* to consult when
    merging, because a failed batch means a gap cannot be concluded (FR-007).

    The returned payloads are the raw answer objects; parsing the individual
    answers, dropping ids that are not in ``requirements`` and merging per batch
    into final statuses is :func:`veritas.review.nodes.requirements.merge_answers`
    and lives with the node, because only the node knows the coverage facts the
    merge depends on.
    """
    sys_prompt = load_prompt("requirements_structured")
    batches = list(plan.batches) if plan is not None else []
    if not batches or not requirements:
        return ([], [])

    listing = "\n".join(f"{r.id}: {r.text}" for r in requirements)
    prefix = ""
    if context:
        prefix += f"{context}\n\n"
    prefix += f"Requirements:\n\n{listing}\n\n"

    total = len(batches)
    payloads: list[dict] = []
    errors: list[str] = []
    for position, batch in enumerate(batches, start=1):
        paths = _batch_paths(batch.chunks)
        if log is not None:
            # Logged before the call so a hung or slow batch is identifiable.
            log.info(f"requirements: batch {position}/{total} ({len(paths)} file(s))")
        try:
            text = llm.complete(sys_prompt, prefix + "Code to review:\n\n" + batch.text)
            payloads.extend(parse_json_array(text))
        except Exception as exc:  # noqa: BLE001 - isolate one batch, keep going
            error = redact_secrets(
                f"requirements: batch {position}/{total} failed "
                f"(files: {', '.join(paths)}): {exc}"
            )
            errors.append(error)
            if log is not None:
                log.warn(error)
    return (payloads, errors)


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