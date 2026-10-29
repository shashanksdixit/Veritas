"""Unit tests — concise run failure output (T098, FR-027).

Many failed LLM batch calls must not turn the report status into a wall of
near-identical text: run.error and the compact "Report status" line give the
failed batch count, the count per review type and each distinct reason once,
within 500 characters, while the report keeps every batch in a Failed batches
section.
"""

from veritas.models.entities import (
    FailedBatch,
    Report,
    ReportStatus,
    ReviewRun,
    ReviewScope,
    Summary,
    Verdict,
)
from veritas.output.compact import (
    ERROR_SUMMARY_LIMIT,
    STATUS_LINE_LIMIT,
    STATUS_PREFIX,
    render_compact,
    summarize_errors,
)
from veritas.output.markdown import render_markdown
from veritas.review.nodes.render import _finalize_run

REVIEW_TYPES = ("code_quality", "security", "performance", "test_coverage", "requirements")

# A realistic provider summary: summarize_llm_error keeps up to 200 characters.
CREDIT_REASON = (
    "openai 402: This request requires more credits, or fewer max_tokens. "
    "You requested up to 8192 tokens, but can only afford 1043. "
    "To increase, visit the account settings page (insufficient_credits)"
)


def _run() -> ReviewRun:
    return ReviewRun(
        scope=ReviewScope.PROJECT,
        target=".",
        config_hash="abc",
        model_name="m",
        prompt_version="1.0.0",
    )


def _failed(review_type: str, batch: int, total: int, reason: str) -> FailedBatch:
    return FailedBatch(
        review_type=review_type,
        batch=batch,
        total=total,
        files=[f"src/{review_type}_{batch}_{n}.py" for n in range(3)],
        reason=reason,
    )


def _forty_identical() -> list[FailedBatch]:
    """8 failed batches for each of the 5 review types, all for one reason."""
    return [
        _failed(review_type, batch, 8, CREDIT_REASON)
        for review_type in REVIEW_TYPES
        for batch in range(1, 9)
    ]


def _finished_report(failed: list[FailedBatch], other_errors: list[str] = ()) -> Report:
    """A report finalised the way the render node does it: the errors channel
    holds each failed batch's message plus any other errors."""
    errors = [f.message for f in failed] + list(other_errors)
    run = _finalize_run({"run": _run()}, errors, failed)
    return Report(
        run=run,
        summary=Summary(
            total_code_findings=0,
            total_requirement_findings=0,
            verification_failure_count=0,
            verdict=Verdict.CLEAN,
        ),
        failed_batches=failed,
    )


def _status_line(report: Report) -> str:
    (line,) = [
        line for line in render_compact(report).splitlines() if line.startswith(STATUS_PREFIX)
    ]
    return line


# --- the status line and run.error ---


def test_forty_identical_failures_give_a_short_status_line():
    report = _finished_report(_forty_identical())
    line = _status_line(report)

    assert report.run.report_status is ReportStatus.INCOMPLETE
    assert len(line) <= STATUS_LINE_LIMIT
    assert len(report.run.error) <= ERROR_SUMMARY_LIMIT
    assert "40 LLM batch call(s) failed" in line
    for review_type in REVIEW_TYPES:
        assert f"{review_type}: 8" in line
    # The reason is named exactly once, not forty times.
    assert line.count("openai 402:") == 1
    # One distinct reason, so nothing is left uncounted.
    assert " more (see Failed batches" not in line
    # No per-batch file lists leak into the summary.
    assert "files:" not in line
    assert "(see Failed batches in the report)" in line


def test_three_distinct_reasons_are_all_listed():
    reasons = ["openai 402: out of credits", "openai 429: rate limited", "timeout after 120s"]
    failed = [_failed("security", i + 1, 3, reason) for i, reason in enumerate(reasons)]
    error = _finished_report(failed).run.error

    for reason in reasons:
        assert error.count(reason) == 1
    assert "more" not in error


def test_five_distinct_reasons_list_three_and_count_the_rest():
    # Distinct frequencies (5, 4, 3, 2, 1) so which three are named is fixed:
    # the most frequent first.
    reasons = [f"provider error {n}" for n in range(1, 6)]
    failed = []
    batch = 0
    for n, reason in enumerate(reasons):
        for _ in range(5 - n):
            batch += 1
            failed.append(_failed("code_quality", batch, 15, reason))
    error = _finished_report(failed).run.error

    assert "15 LLM batch call(s) failed (code_quality: 15)" in error
    assert "provider error 1; provider error 2; provider error 3; and 2 more" in error
    assert "provider error 4" not in error
    assert "provider error 5" not in error


def test_long_reasons_and_errors_still_fit_the_limit():
    reasons = [f"{n} " + "x" * 200 for n in range(6)]
    failed = [_failed("performance", n + 1, 6, reason) for n, reason in enumerate(reasons)]
    others = [f"verification did not run; {n} " + "y" * 150 for n in range(3)]
    report = _finished_report(failed, others)

    assert len(report.run.error) <= ERROR_SUMMARY_LIMIT
    assert len(_status_line(report)) <= STATUS_LINE_LIMIT
    # The counts come first, so clipping never loses them.
    assert report.run.error.startswith("6 LLM batch call(s) failed (performance: 6)")


def test_non_batch_error_is_still_reported_once():
    withheld = "verification did not run; 8 unverified finding(s) withheld"
    report = _finished_report(_forty_identical(), [withheld, withheld])

    assert report.run.error.count(withheld) == 1
    assert "40 LLM batch call(s) failed" in report.run.error
    assert withheld in _status_line(report)


def test_non_batch_errors_alone_are_reported_deduplicated():
    crash = "code_quality: llm: connection reset"
    withheld = "verification did not run; 2 unverified finding(s) withheld"
    report = _finished_report([], [crash, withheld, crash])

    assert report.run.report_status is ReportStatus.INCOMPLETE
    assert report.run.error == f"{crash}; {withheld}"
    assert "LLM batch call" not in report.run.error


def test_no_errors_leaves_the_run_complete():
    report = _finished_report([])

    assert report.run.report_status is ReportStatus.COMPLETE
    assert report.run.error is None
    assert summarize_errors([], [], limit=ERROR_SUMMARY_LIMIT) is None


def test_status_line_is_clipped_even_for_an_overlong_stored_error():
    # A report finalised before this limit existed can carry any run.error.
    report = _finished_report([])
    report.run.report_status = ReportStatus.INCOMPLETE
    report.run.error = "e" * 2000
    line = _status_line(report)

    assert len(line) == STATUS_LINE_LIMIT
    assert line.startswith(STATUS_PREFIX)
    assert line.endswith("…")


# --- the Failed batches section ---


def test_failed_batches_section_lists_every_batch():
    md = render_markdown(_finished_report(_forty_identical()))
    section = md.split("### Failed batches (FR-027)", 1)[1].split("</details>", 1)[0]

    assert "<details>" in section
    assert "<summary>40 failed batch call(s)</summary>" in section
    assert "| Review type | Batch | Files | Reason |" in section
    rows = [line for line in section.splitlines() if line.startswith("| ") and "/8 |" in line]
    assert len(rows) == 40
    assert f"| security | 3/8 | 3 | {CREDIT_REASON} |" in rows
    # It comes after Verification failures and before the code findings.
    assert md.index("### Failed batches") < md.index("## Code Findings")


def test_failed_batches_section_escapes_pipes_in_reasons():
    failed = [_failed("security", 1, 1, "proxy said a|b")]
    md = render_markdown(_finished_report(failed))

    assert "| security | 1/1 | 3 | proxy said a\\|b |" in md


def test_no_failed_batches_section_without_batch_failures():
    withheld = "verification did not run; 1 unverified finding(s) withheld"
    md = render_markdown(_finished_report([], [withheld]))

    assert "Failed batches" not in md
    assert withheld in md


# --- the record itself ---


def test_failed_batch_message_matches_the_logged_error_format():
    failed = FailedBatch(
        review_type="requirements",
        batch=2,
        total=5,
        files=["src/a.py", "src/b.py"],
        reason="openai 500: upstream error",
    )

    assert failed.message == (
        "requirements: batch 2/5 failed (files: src/a.py, src/b.py): openai 500: upstream error"
    )


def test_failed_batches_round_trip_and_older_reports_still_load():
    report = _finished_report(_forty_identical()[:2])
    restored = Report.model_validate_json(report.model_dump_json())
    assert restored.failed_batches == report.failed_batches
    assert "message" not in report.model_dump()["failed_batches"][0]

    older = report.model_dump(mode="json")
    del older["failed_batches"]
    assert Report.model_validate(older).failed_batches == []
