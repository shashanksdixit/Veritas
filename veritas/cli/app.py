"""CLI (T043/T048/T051/T052) — Typer app: review, suppress, unsuppress.

Exit codes (contracts/cli.md): 0 complete, 1 fatal, 2 partial/incomplete.
stdout = results only; stderr = diagnostics/errors.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from pydantic import ValidationError

from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import load_settings
from veritas.models.entities import Report, ReportStatus, ReviewScope
from veritas.review import ReviewFatalError
from veritas.review.graph import run_review
from veritas.suppression.resolver import resolve_by_id, resolve_by_location
from veritas.suppression.store import SuppressionStore
from veritas.utils.logging import Log

app = typer.Typer(
    name="veritas",
    help="CLI-based, multi-agent code review tool.",
    no_args_is_help=True,
    # An uncaught exception must not print local variables (settings carry the
    # API key); the traceback text itself is not redacted (FR-013).
    pretty_exceptions_show_locals=False,
)

_SCOPE_VALUES: dict[str, ReviewScope] = {
    "project": ReviewScope.PROJECT,
    "module": ReviewScope.MODULE,
    "file": ReviewScope.FILE,
    "pr": ReviewScope.PR,
}


def _error(message: str) -> None:
    """Write ``[error] message`` to stderr through Log, which redacts it (FR-013).

    A new Log per call binds to the current ``sys.stderr``, so a stream swapped
    in after import (a test runner, for example) still receives the line.
    """
    Log().error(message)


def _configure_console() -> None:
    """Make result/error streams UTF-8 with loss-tolerant decoding on Windows.

    Windows consoles default to the ANSI codepage (e.g. cp1252); reconfiguring
    to UTF-8 with ``errors="replace"`` keeps non-ASCII report output from
    crashing a run. Streams that do not support ``reconfigure`` (e.g. some
    interceptors) are left untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


@app.callback()
def _startup() -> None:
    """Pre-command start-up: normalize the console before anything is printed."""
    _configure_console()


@app.command()
def review(
    scope: str = typer.Option(..., "--scope", help="Review scope: project|module|file|pr"),
    target: str = typer.Option(..., "--target", help="Directory, file, or PR ref e.g. owner/repo#N"),
    config: str = typer.Option(None, "--config", help="Config file path (default: .veritas/config.toml)"),
    output: str = typer.Option(None, "--output", help="Report file path (default: ./veritas-report-<timestamp>.md)"),
    verbose: bool = typer.Option(False, "--verbose", help="Enable verbose logging to stderr"),
    post: bool = typer.Option(False, "--post", help="Post the report as a PR comment (--scope pr only)"),
) -> None:
    """Run a code review (primary command)."""
    scope_lower = scope.lower()
    scope_val = _SCOPE_VALUES.get(scope_lower)
    if scope_val is None:
        _error("Invalid scope. Use one of: project, module, file, pr.")
        raise typer.Exit(1)
    try:
        settings = load_settings(config)
    except ValidationError as exc:
        for problem in exc.errors():
            field = ".".join(str(part) for part in problem["loc"])
            _error(f"invalid configuration: {field}: {problem['msg']}")
        raise typer.Exit(1) from None
    except ValueError as exc:
        _error(f"invalid configuration: {exc}")
        raise typer.Exit(1) from None
    try:
        outcome = run_review(
            settings,
            scope_val,
            target,
            output=output,
            post=post,
            verbose=verbose,
        )
    except ReviewFatalError as exc:
        _error(str(exc))
        raise typer.Exit(1) from exc
    raise typer.Exit(outcome.exit_code)


@app.command()
def suppress(
    finding_id: str = typer.Option(None, "--finding-id", help="Stable finding ID from report"),
    file: str = typer.Option(None, "--file", help="Filename (alternative to --finding-id)"),
    line: int = typer.Option(None, "--line", help="Line number (with --file)"),
    reason: str = typer.Option(None, "--reason", help="Reason for suppression"),
) -> None:
    """Suppress a finding by ID or filename+line (FR-017)."""
    report = _load_last_report()
    if report is None:
        _error("No previous review found (missing .veritas/last-report.json). Run `veritas review` first.")
        raise typer.Exit(1)

    if finding_id:
        resolution = resolve_by_id(report, finding_id)
    elif file and line is not None:
        resolution = resolve_by_location(report, file, line)
    elif file or line is not None:
        _error("--file requires --line and vice versa (or use --finding-id).")
        raise typer.Exit(1)
    else:
        _error("Provide --finding-id, or --file with --line.")
        raise typer.Exit(1)

    if resolution.status == "no_match":
        _error("No finding matched the given reference; nothing was suppressed.")
        raise typer.Exit(1)
    if resolution.status == "ambiguous":
        lines = "\n".join(f"  - {f.id}: {f.file}:{f.line_range.start_line} {f.title}" for f in resolution.candidates)
        _error(
            f"Ambiguous: {len(resolution.candidates)} findings match. Nothing suppressed. Candidates:\n{lines}"
        )
        raise typer.Exit(1)

    finding = resolution.finding
    snippet = finding.cited_snippet or ""
    store = SuppressionStore()
    entry = store.upsert_suppression(
        file=finding.file,
        category=finding.category,
        title=finding.title,
        snippet=snippet,
        reason=reason,
    )
    print(
        f"Suppressed. Fingerprint: {entry.fingerprint}  "
        "(use with 'veritas unsuppress --fingerprint' to reverse)"
    )


@app.command()
def unsuppress(
    fingerprint: str = typer.Option(..., "--fingerprint", help="SHA-256 fingerprint of suppression entry"),
) -> None:
    """Remove a suppression entry (FR-018)."""
    store = SuppressionStore()
    if store.remove(fingerprint):
        print(f"Un-suppressed: fingerprint {fingerprint}")
        return
    _error("No matching suppression entry found.")
    raise typer.Exit(1)


def _load_last_report() -> Report | None:
    try:
        data = Path(LAST_REPORT_JSON).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        report = Report.model_validate_json(data)
    except Exception:  # noqa: BLE001 - stale/malformed sidecar
        return None
    if report.run.report_status == ReportStatus.INCOMPLETE:
        return None
    return report