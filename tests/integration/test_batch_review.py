"""Integration tests — batched code review consumption (T076, FR-029).

The scope node plans the batches once; every code review type drives that same
plan. These tests pin the three properties that makes safe:

* all four review types see exactly the same code, one LLM call per batch, in
  plan order;
* a failure in one batch loses that batch only — the remaining batches still
  produce findings, the failure becomes an error, and the run's report status is
  incomplete (FR-027);
* Coverage on the report describes the plan, and a citation into the second
  chunk of a split file still verifies at its original line number (FR-013).
"""

from __future__ import annotations

import json
from pathlib import Path

from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.models.entities import Report, ReviewRun, ReviewScope
from veritas.review.batching import plan_batches
from veritas.review.graph import Runtime, run_review
from veritas.review.nodes.code_quality import make_code_quality_node
from veritas.review.nodes.performance import make_performance_node
from veritas.review.nodes.requirements import make_requirements_node
from veritas.review.nodes.security import make_security_node
from veritas.review.nodes.test_coverage import make_test_coverage_node
from veritas.utils.logging import Log

_CODE_MARKER = "Code to review:\n\n"

# The four code review types. requirements is deliberately absent: it consumes
# code_package and is not batched (B2).
_REVIEW_TYPES = (
    ("code_quality", make_code_quality_node),
    ("security", make_security_node),
    ("test_coverage", make_test_coverage_node),
    ("performance", make_performance_node),
)

# Three files of 25 lines render to ~594-char blocks, so at batch_chars=1000 two
# blocks (594 + 2 + 594) do not fit one batch and each file takes its own.
_BATCH_CHARS = 1000


def _lines(count: int, tag: str = "line") -> str:
    return "".join(f"{tag}_{n:06d} = {n}\n" for n in range(1, count + 1))


def _three_files() -> dict[str, str]:
    return {
        "src/a.py": _lines(25, "a"),
        "src/b.py": _lines(25, "b"),
        "src/c.py": _lines(25, "c"),
    }


def _three_batch_plan():
    plan, warnings = plan_batches(_three_files(), batch_chars=_BATCH_CHARS, max_batches=8)
    assert warnings == []
    assert len(plan.batches) == 3
    assert plan.reviewed_files == ("src/a.py", "src/b.py", "src/c.py")
    return plan


def _finding_from_batch(user: str, finding_id: str) -> str:
    """A canned finding citing the batch's first numbered line.

    Derived from the message the node actually sent, so the finding is grounded
    in what the reviewer was shown and cites a line that really exists in the
    file (FR-013).
    """
    block = user.split(_CODE_MARKER, 1)[1].splitlines()
    header = block[0]
    path = header[len("### FILE: ") :].split(" (lines ")[0]
    number, text = block[1].split("| ", 1)
    return json.dumps(
        [
            {
                "id": finding_id,
                "file": path,
                "start_line": int(number),
                "start_col": 1,
                "end_line": int(number),
                "end_col": 1,
                "severity": "warning",
                "title": f"Issue at {path}:{number}",
                "description": "Something worth fixing.",
                "recommendation": "Fix it.",
                "confidence": 0.9,
                "cited_snippet": text,
            }
        ]
    )


class SpyLLM:
    """Records every call and answers each batch with a grounded finding.

    ``events`` is shared with the log capture so a test can assert that the
    per-batch info line was emitted before that batch's LLM call.
    """

    model_name = "spy-model"

    def __init__(
        self,
        *,
        fail_on: int | None = None,
        message: str = "provider exploded",
        events: list[str] | None = None,
    ):
        self.calls: list[tuple[str, str]] = []
        self.events: list[str] = events if events is not None else []
        self._fail_on = fail_on
        self._message = message

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        self.events.append(f"llm-call-{len(self.calls)}")
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise RuntimeError(self._message)
        return _finding_from_batch(user, f"cf-{len(self.calls)}")


def _runtime(llm, *, capture: list[str] | None = None, **settings_kwargs) -> Runtime:
    """A Runtime whose log appends to ``capture`` so ordering can be asserted."""
    log = Log(verbose=False)
    if capture is not None:
        log.info = lambda message, **_kw: capture.append(f"info: {message}")  # type: ignore[method-assign]
        log.warn = lambda message, **_kw: capture.append(f"warn: {message}")  # type: ignore[method-assign]
    settings = Settings(api_key="test-key", **settings_kwargs)
    kwargs = {"report_path": "unused.md"} if not capture else {}
    return Runtime(settings=settings, log=log, llm=llm, **kwargs)


def _state(plan, files: dict[str, str] | None = None) -> dict:
    return {
        "files": files if files is not None else _three_files(),
        "batch_plan": plan,
        "project_context": None,
        "sast_findings": [],
        "degraded_sast": None,
        "code_findings": [],
        "requirement_findings": [],
        "errors": [],
    }


def _user_bodies(llm: SpyLLM) -> list[str]:
    """The code portion of each user message — what the reviewer was shown."""
    return [user.split(_CODE_MARKER, 1)[1] for _system, user in llm.calls]


# --- one call per batch, in plan order, identical across the four types ---


def test_each_review_type_makes_exactly_one_call_per_batch_in_plan_order():
    plan = _three_batch_plan()
    expected = [batch.text for batch in plan.batches]
    for name, factory in _REVIEW_TYPES:
        llm = SpyLLM()
        result = factory(_runtime(llm))(_state(plan))
        assert result["errors"] == [], name
        assert len(llm.calls) == 3, name
        assert _user_bodies(llm) == expected, name


def test_all_four_review_types_see_identical_batch_texts():
    plan = _three_batch_plan()
    seen = []
    for name, factory in _REVIEW_TYPES:
        llm = SpyLLM()
        factory(_runtime(llm))(_state(plan))
        seen.append(_user_bodies(llm))
        assert len(seen[-1]) == 3, name
    for bodies in seen[1:]:
        assert bodies == seen[0]
    assert seen[0] == [batch.text for batch in plan.batches]


def test_every_review_type_sees_a_split_files_chunk_header():
    """A file too large for one batch is shown with its original line range."""
    plan = _three_batch_plan()
    files = {"src/big.py": _lines(200, "big"), "src/small.py": _lines(3, "small")}
    split_plan, _ = plan_batches(files, batch_chars=_BATCH_CHARS, max_batches=8)
    assert split_plan.split_files == ("src/big.py",)
    for name, factory in _REVIEW_TYPES:
        llm = SpyLLM()
        factory(_runtime(llm))(_state(split_plan, files))
        bodies = _user_bodies(llm)
        assert any("(lines " in body for body in bodies), name


# --- per-batch failure isolation (FR-027) ---


def test_one_failing_batch_keeps_findings_from_the_other_batches():
    plan = _three_batch_plan()
    llm = SpyLLM(fail_on=2)
    result = make_code_quality_node(_runtime(llm))(_state(plan))

    assert len(llm.calls) == 3  # every batch was still attempted
    assert [f.file for f in result["code_findings"]] == ["src/a.py", "src/c.py"]
    assert result["errors"] == [
        "code_quality: batch 2/3 failed (files: src/b.py): provider exploded"
    ]


def test_failed_batch_error_names_the_batch_and_its_files():
    plan = _three_batch_plan()
    llm = SpyLLM(fail_on=1, message="429 rate limited")
    result = make_code_quality_node(_runtime(llm))(_state(plan))
    (error,) = result["errors"]
    assert error == "code_quality: batch 1/3 failed (files: src/a.py): 429 rate limited"
    # Only that batch is lost; the two later batches still reviewed their files.
    assert [f.file for f in result["code_findings"]] == ["src/b.py", "src/c.py"]


def test_failing_batch_is_isolated_for_one_review_type_only():
    """Only the failing type records an error; the other three still finish."""

    class FailsOnCodeQualityBatch2(SpyLLM):
        """Raises on the second code-quality call, ignoring the other types."""

        def __init__(self):
            super().__init__()
            self._code_quality_calls = 0

        def complete(self, system: str, user: str) -> str:
            if "code-quality" not in system.lower():
                return super().complete(system, user)
            self._code_quality_calls += 1
            self.calls.append((system, user))
            if self._code_quality_calls == 2:
                raise RuntimeError("provider exploded")
            return _finding_from_batch(user, "cf-cq")

    plan = _three_batch_plan()
    per_type_errors = {}
    for name, factory in _REVIEW_TYPES:
        result = factory(_runtime(FailsOnCodeQualityBatch2()))(_state(plan))
        per_type_errors[name] = result["errors"]

    assert len(per_type_errors["code_quality"]) == 1
    assert "batch 2/3 failed (files: src/b.py)" in per_type_errors["code_quality"][0]
    assert per_type_errors["security"] == []
    assert per_type_errors["test_coverage"] == []
    assert per_type_errors["performance"] == []


def test_batch_failure_message_is_redacted():
    plan = _three_batch_plan()
    secret = "sk-abcdefghijklmnop1234"
    llm = SpyLLM(fail_on=2, message=f"auth failed for {secret}")
    result = make_code_quality_node(_runtime(llm))(_state(plan))
    (error,) = result["errors"]
    assert secret not in error
    assert "[REDACTED]" in error
    # The batch identity survives redaction, so the reader still knows what failed.
    assert "code_quality: batch 2/3 failed (files: src/b.py)" in error


def test_redacted_batch_error_is_also_redacted_in_the_log():
    plan = _three_batch_plan()
    secret = "sk-abcdefghijklmnop1234"
    events: list[str] = []
    llm = SpyLLM(fail_on=2, message=f"auth failed for {secret}")
    make_code_quality_node(_runtime(llm, capture=events))(_state(plan))
    warned = [e for e in events if e.startswith("warn:")]
    assert warned and secret not in warned[0] and "[REDACTED]" in warned[0]


# --- zero batches ---


def test_zero_batch_plan_makes_no_llm_call():
    empty, _ = plan_batches({}, batch_chars=_BATCH_CHARS, max_batches=8)
    assert empty.batches == ()
    for name, factory in _REVIEW_TYPES:
        llm = SpyLLM()
        events: list[str] = []
        result = factory(_runtime(llm, capture=events))(_state(empty, {}))
        assert llm.calls == [], name
        assert result == {"code_findings": [], "errors": []}, name
        assert any(f"{name}: no source files to review" in e for e in events), name


def test_missing_plan_makes_no_llm_call():
    for name, factory in _REVIEW_TYPES:
        llm = SpyLLM()
        events: list[str] = []
        result = factory(_runtime(llm, capture=events))(_state(None, {}))
        assert llm.calls == [], name
        assert (result["code_findings"], result["errors"]) == ([], []), name
        assert any(f"{name}: no source files to review" in e for e in events), name


# --- log ordering ---


def test_per_batch_info_line_is_logged_before_each_llm_call():
    plan = _three_batch_plan()
    events: list[str] = []
    llm = SpyLLM(events=events)
    make_code_quality_node(_runtime(llm, capture=events))(_state(plan))

    batch_lines = [e for e in events if "batch " in e and e.startswith("info:")]
    assert batch_lines == [
        "info: code_quality: batch 1/3 (1 file(s))",
        "info: code_quality: batch 2/3 (1 file(s))",
        "info: code_quality: batch 3/3 (1 file(s))",
    ]
    # Every per-batch line precedes that batch's call, in order.
    expected = [
        "info: code_quality: batch 1/3 (1 file(s))",
        "llm-call-1",
        "info: code_quality: batch 2/3 (1 file(s))",
        "llm-call-2",
        "info: code_quality: batch 3/3 (1 file(s))",
        "llm-call-3",
    ]
    assert events[:6] == expected


def test_per_batch_info_line_counts_the_files_in_the_batch():
    files = {"src/a.py": _lines(3, "a"), "src/b.py": _lines(3, "b")}
    plan, _ = plan_batches(files, batch_chars=_BATCH_CHARS, max_batches=8)
    assert len(plan.batches) == 1
    events: list[str] = []
    llm = SpyLLM()
    make_code_quality_node(_runtime(llm, capture=events))(_state(plan, files))
    assert "info: code_quality: batch 1/1 (2 file(s))" in events


# --- the requirements node keeps code_package (B2, unchanged) ---


def test_requirements_node_still_receives_code_package_over_all_files():
    plan = _three_batch_plan()
    files = {**_three_files(), "spec.md": "# REQ-1\n- SATISFIED: yes\n"}
    llm = SpyLLM()
    make_requirements_node(_runtime(llm))(_state(plan, files))

    assert len(llm.calls) == 1  # one call: not batched
    (_system, user) = llm.calls[0]
    # code_package shows every scoped file, including the requirement doc that
    # the code review types never see.
    for path in sorted(files):
        assert f"### FILE: {path}" in user
    assert "### FILE: spec.md" in user


def test_code_review_types_never_see_the_requirement_document():
    files = {**_three_files(), "spec.md": "# REQ-1\n- SATISFIED: yes\n"}
    plan, _ = plan_batches(
        {path: text for path, text in files.items() if path != "spec.md"},
        batch_chars=_BATCH_CHARS,
        max_batches=8,
    )
    for name, factory in _REVIEW_TYPES:
        llm = SpyLLM()
        factory(_runtime(llm))(_state(plan, files))
        assert all("spec.md" not in body for body in _user_bodies(llm)), name


# --- end to end: coverage on the report, and a second-chunk citation ---


def _split_project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "vendor").mkdir(exist_ok=True)
    (tmp_path / "src" / "big.py").write_text(_lines(200, "big"), encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text(_lines(4, "app"), encoding="utf-8")
    (tmp_path / "vendor" / "thirdparty.py").write_text(_lines(30, "vendor"), encoding="utf-8")
    return tmp_path


def _end_to_end_settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="test-key",
        batch_chars=_BATCH_CHARS,
        max_batches=8,
        exclude=["vendor/"],
    )


def _read_report() -> Report:
    return Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))


def test_end_to_end_coverage_describes_the_plan(tmp_path):
    project = _split_project(tmp_path)
    outcome = run_review(
        _end_to_end_settings(tmp_path),
        ReviewScope.PROJECT,
        str(project),
        llm=SpyLLM(),
    )
    assert outcome.exit_code == 0
    coverage = _read_report().coverage

    assert coverage is not None
    assert coverage.batch_chars == _BATCH_CHARS
    assert coverage.max_batches == 8
    assert "src/big.py" in coverage.split_files
    assert coverage.reviewed_files == sorted(coverage.reviewed_files)
    assert "src/app.py" in coverage.reviewed_files
    assert "vendor/thirdparty.py" not in coverage.reviewed_files
    # exclusion is reported with the pattern that withheld the file
    assert [e.path for e in coverage.excluded_files] == ["vendor/thirdparty.py"]
    assert coverage.excluded_files[0].pattern == "vendor/"
    assert coverage.batches_used > 1
    assert coverage.batches_used <= coverage.max_batches


def test_end_to_end_coverage_is_none_without_a_plan(tmp_path):
    """A report built without a plan keeps coverage None (schema default)."""
    from veritas.review.nodes.render import make_render_node

    project = _split_project(tmp_path)
    run = ReviewRun(
        scope=ReviewScope.PROJECT,
        target=str(project),
        config_hash="h",
        model_name="m",
        prompt_version="1.3.0",
    )
    state = {
        "run": run,
        "files": {},
        "batch_plan": None,
        "excluded_files": [],
        "verification_failures": [],
        "errors": [],
        "verified_code_findings": [],
        "verified_requirement_findings": [],
    }
    runtime = _runtime(SpyLLM(), capture=[])
    report_out = make_render_node(runtime)(state)
    assert report_out["phase"] == "done"
    assert _read_report().coverage is None


def test_finding_citing_the_second_chunk_verifies_at_its_original_line(tmp_path):
    project = _split_project(tmp_path)
    outcome = run_review(
        _end_to_end_settings(tmp_path),
        ReviewScope.PROJECT,
        str(project),
        llm=SpyLLM(),
    )
    assert outcome.exit_code == 0
    report = _read_report()

    # Independently replan the same inputs to learn the split file's chunks.
    scoped = {
        "src/big.py": _lines(200, "big"),
        "src/app.py": _lines(4, "app"),
    }
    expected_plan, _ = plan_batches(scoped, batch_chars=_BATCH_CHARS, max_batches=8)
    assert expected_plan.split_files == ("src/big.py",)
    big_chunks = [
        chunk
        for batch in expected_plan.batches
        for chunk in batch.chunks
        if chunk.path == "src/big.py"
    ]
    second_chunk = big_chunks[1]
    assert second_chunk.start_line > 1  # genuinely a later chunk of the same file
    assert big_chunks[0].end_line < second_chunk.start_line

    in_big = [f for f in report.code_findings if f.file == "src/big.py"]
    assert in_big, "expected findings from the split file"
    cited = {f.line_range.start_line for f in in_big}
    assert second_chunk.start_line in cited

    kept = next(f for f in in_big if f.line_range.start_line == second_chunk.start_line)
    # The citation survived verification unchanged: no correction, and the number
    # the reviewer cited is the file's own line number.
    assert kept.citation_adjusted_from is None
    assert kept.line_range.start_line == second_chunk.start_line
    assert scoped["src/big.py"].splitlines()[second_chunk.start_line - 1] == kept.cited_snippet


def test_split_file_chunks_are_reviewed_by_every_type_end_to_end(tmp_path):
    project = _split_project(tmp_path)
    run_review(_end_to_end_settings(tmp_path), ReviewScope.PROJECT, str(project), llm=SpyLLM())
    report = _read_report()
    # One finding per chunk per review type, so the whole split file is covered
    # rather than only its first chunk.
    assert len(report.code_findings) >= 2
    assert report.run.report_status.value == "complete"


def test_end_to_end_batch_failure_makes_the_report_incomplete(tmp_path):
    project = _split_project(tmp_path)
    llm = SpyLLM(fail_on=2)
    outcome = run_review(
        _end_to_end_settings(tmp_path),
        ReviewScope.PROJECT,
        str(project),
        llm=llm,
    )
    report = _read_report()

    assert outcome.exit_code == 2
    assert report.run.report_status.value == "incomplete"
    assert "batch 2/" in (report.run.error or "")
    # Coverage still describes the plan: a failed batch is an error, not a gap
    # in coverage.
    assert report.coverage is not None
    assert report.coverage.batches_used >= 2
