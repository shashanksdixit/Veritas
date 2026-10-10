"""Write tools (T011) — isolated for post-review nodes ONLY.

Constitutional boundary: this module is imported ONLY by the terminal render
node (and the CLI layer for explicit user commands). No review node may import
it; the graph is wired so write reachability never flows back into review.
"""

from __future__ import annotations

import json
from pathlib import Path

from veritas.models.entities import Report
from veritas.output.markdown import render_markdown
from veritas.suppression.store import SuppressionStore
from veritas.utils.redaction import redact_json_strings, redact_secrets


def write_report(path: str, report: Report, log=None) -> str:
    """Render and persist the report Markdown file. Returns the path.

    The finished Markdown passes through ``redact_secrets()`` as it is written
    (FR-013), so a secret in any field — redacted at its source or not — never
    reaches the file.
    """
    markdown = report.markdown_content
    if markdown is None:
        markdown = render_markdown(report)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(redact_secrets(markdown), encoding="utf-8")
    if log is not None:
        log.info(f"Report written to {target}", report_path=str(target))
    return str(target)


def update_suppressions(
    file: str,
    category,
    title: str,
    snippet: str,
    reason: str | None = None,
    *,
    path: str | None = None,
):
    """Upsert a suppression entry (used by CLI suppress). Not reachable from
    any review node."""
    store = SuppressionStore(path)
    saved = store.upsert_suppression(
        file=file,
        category=category,
        title=title,
        snippet=snippet,
        reason=reason,
    )
    return {"entry": saved, "store": store}


def persist_last_report_json(report: Report, path: str) -> None:
    """Write the machine-readable Report JSON used by suppress/unsuppress.

    Every string value is redacted as it is serialised (FR-013). ``suppress``
    recomputes a fingerprint from the stored ``cited_snippet``; that still
    matches the one render computed because every snippet is already redacted
    before it enters state and ``redact_secrets()`` is idempotent.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = redact_json_strings(report.model_dump(mode="json"))
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")