"""Write tools (T011) — isolated for post-review nodes ONLY.

Constitutional boundary: this module is imported ONLY by the terminal render
node (and the CLI layer for explicit user commands). No review node may import
it; the graph is wired so write reachability never flows back into review.
"""

from __future__ import annotations

from pathlib import Path

from veritas.models.entities import Report
from veritas.output.markdown import render_markdown
from veritas.suppression.store import SuppressionStore


def write_report(path: str, report: Report, log=None) -> str:
    """Render and persist the report Markdown file. Returns the path."""
    markdown = report.markdown_content
    if markdown is None:
        markdown = render_markdown(report)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
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
    """Write the machine-readable Report JSON used by suppress/unsuppress."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")