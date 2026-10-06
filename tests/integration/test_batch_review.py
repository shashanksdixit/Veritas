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
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from random import Random

from tests.conftest import fake_secret
from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.models.entities import (
    FindingSource,
    Report,
    ReportStatus,
    ReviewRun,
    ReviewScope,
    Severity,
    VerificationReasonCode,
    Verdict,
)
from veritas.review.batching import plan_batches
from veritas.review.graph import Runtime, run_review
from veritas.review.nodes.code_quality import make_code_quality_node
from veritas.review.nodes.common import load_prompt
from veritas.review.nodes.performance import make_performance_node
from veritas.review.nodes.requirements import make_requirements_node
from veritas.review.nodes.security import make_security_node
from veritas.review.nodes.test_coverage import make_test_coverage_node
from veritas.security.opengrep import OpengrepResult
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


# Every review type the graph fans out to, named as its node names its prompt.
# requirements is deliberately in here too: it is unbatched but the graph drives
# it from the same superstep, so it calls the LLM from the same threads.
_REVIEW_PROMPT_NAMES = tuple(name for name, _factory in _REVIEW_TYPES) + ("requirements",)


@lru_cache(maxsize=None)
def _review_prompt(review: str) -> str:
    """The exact system string one review type sends on every one of its calls.

    Each node hands ``load_prompt(name)`` straight to ``complete``, so comparing
    the recorded system prompt against this identifies the caller without
    relying on thread order.
    """
    return load_prompt(review)


def _review_of(system: str) -> str:
    """Which review type a system prompt belongs to, or ``"other"``."""
    for name in _REVIEW_PROMPT_NAMES:
        if system == _review_prompt(name):
            return name
    return "other"


class SpyLLM:
    """Records every call and answers each batch with a grounded finding.

    The graph runs all five review nodes in one superstep, so they share this
    object across threads and the order of ``calls`` is not reproducible. A
    configured failure is therefore scoped to one review type by ``fail_review``
    and counted against that type's own calls, which ``llm_findings`` issues
    sequentially in plan order: ``fail_on=2, fail_review="code_quality"`` fails
    code quality's second batch however the threads interleave, and a global
    call number is refused outright.

    A test that drives ``llm_findings`` for a single review type from the main
    thread still sees one stream, so counting per review type is identical to
    counting globally there.

    ``events`` is shared with the log capture so a test can assert that the
    per-batch info line was emitted before that batch's LLM call.
    """

    model_name = "spy-model"

    def __init__(
        self,
        *,
        fail_on: int | None = None,
        fail_review: str | None = None,
        message: str = "provider exploded",
        events: list[str] | None = None,
    ):
        if fail_on is not None and fail_review is None:
            raise ValueError(
                "fail_on needs fail_review: a bare call number is not deterministic, "
                "because the review nodes call the LLM from parallel threads"
            )
        if fail_review is not None and fail_review not in _REVIEW_PROMPT_NAMES:
            raise ValueError(
                f"unknown review type {fail_review!r}; "
                f"expected one of {_REVIEW_PROMPT_NAMES}"
            )
        self.calls: list[tuple[str, str]] = []
        self.events: list[str] = events if events is not None else []
        self._fail_on = fail_on
        self._fail_review = fail_review
        self._message = message
        self._lock = threading.Lock()
        self._calls_per_review: dict[str, int] = {}

    def calls_for(self, review: str) -> list[tuple[str, str]]:
        """The recorded calls of one review type, in the order it issued them."""
        prompt = _review_prompt(review)
        with self._lock:
            return [(s, u) for s, u in self.calls if s == prompt]

    def complete(self, system: str, user: str) -> str:
        with self._lock:
            self.calls.append((system, user))
            index = len(self.calls)
            review = _review_of(system)
            nth = self._calls_per_review.get(review, 0) + 1
            self._calls_per_review[review] = nth
            self.events.append(f"llm-call-{index}")
            should_fail = review == self._fail_review and nth == self._fail_on
        if should_fail:
            raise RuntimeError(self._message)
        return _finding_from_batch(user, f"cf-{index}")


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
    llm = SpyLLM(fail_on=2, fail_review="code_quality")
    result = make_code_quality_node(_runtime(llm))(_state(plan))

    assert len(llm.calls) == 3  # every batch was still attempted
    assert [f.file for f in result["code_findings"]] == ["src/a.py", "src/c.py"]
    assert result["errors"] == [
        "code_quality: batch 2/3 failed (files: src/b.py): llm: provider exploded"
    ]


def test_failed_batch_error_names_the_batch_and_its_files():
    plan = _three_batch_plan()
    llm = SpyLLM(fail_on=1, fail_review="code_quality", message="429 rate limited")
    result = make_code_quality_node(_runtime(llm))(_state(plan))
    (error,) = result["errors"]
    assert error == "code_quality: batch 1/3 failed (files: src/a.py): llm: 429 rate limited"
    # Only that batch is lost; the two later batches still reviewed their files.
    assert [f.file for f in result["code_findings"]] == ["src/b.py", "src/c.py"]


def test_failing_batch_is_isolated_for_one_review_type_only():
    """Only the failing type records an error; the other three still finish."""
    plan = _three_batch_plan()
    per_type_errors = {}
    for name, factory in _REVIEW_TYPES:
        llm = SpyLLM(fail_on=2, fail_review="code_quality")
        result = factory(_runtime(llm))(_state(plan))
        per_type_errors[name] = result["errors"]

    assert len(per_type_errors["code_quality"]) == 1
    assert "batch 2/3 failed (files: src/b.py)" in per_type_errors["code_quality"][0]
    assert per_type_errors["security"] == []
    assert per_type_errors["test_coverage"] == []
    assert per_type_errors["performance"] == []


def test_batch_failure_message_is_redacted():
    plan = _three_batch_plan()
    secret = fake_secret("sk-", "abcdefghijklmnop1234")
    llm = SpyLLM(fail_on=2, fail_review="code_quality", message=f"auth failed for {secret}")
    result = make_code_quality_node(_runtime(llm))(_state(plan))
    (error,) = result["errors"]
    assert secret not in error
    assert "[REDACTED]" in error
    # The batch identity survives redaction, so the reader still knows what failed.
    assert "code_quality: batch 2/3 failed (files: src/b.py)" in error


def test_redacted_batch_error_is_also_redacted_in_the_log():
    plan = _three_batch_plan()
    secret = fake_secret("sk-", "abcdefghijklmnop1234")
    events: list[str] = []
    llm = SpyLLM(fail_on=2, fail_review="code_quality", message=f"auth failed for {secret}")
    make_code_quality_node(_runtime(llm, capture=events))(_state(plan))
    warned = [e for e in events if e.startswith("warn:")]
    assert warned and secret not in warned[0] and "[REDACTED]" in warned[0]


# --- the fake's failure targeting survives the graph's thread fan-out ---


def _tagged_body(tag: str) -> str:
    """A user message shaped like one batch, tagged so calls can be told apart."""
    return f"{_CODE_MARKER}### FILE: src/{tag}.py (lines 1-1)\n1 | {tag}\n"


def _tag_of(user: str) -> str:
    """The tag baked into a :func:`_tagged_body` message."""
    return user.split(_CODE_MARKER, 1)[1].splitlines()[1].split("| ", 1)[1]


def _race(llm: SpyLLM, schedule: list[tuple[str, str]]) -> list[str]:
    """Issue one ``llm`` call per schedule entry from its own thread at once.

    Every caller waits on a barrier, so all of them are in flight before any of
    them reaches the spy and the arrival order is the scheduler's, not the
    schedule's. Returns the tags of the calls that raised.
    """
    start = threading.Barrier(len(schedule))

    def call(review: str, tag: str) -> str | None:
        start.wait()
        try:
            llm.complete(_review_prompt(review), _tagged_body(tag))
        except RuntimeError:
            return tag
        return None

    with ThreadPoolExecutor(max_workers=len(schedule)) as pool:
        return [tag for tag in pool.map(lambda pair: call(*pair), schedule) if tag]


def test_configured_failure_targets_its_own_review_type_under_thread_interleaving():
    """``fail_on`` counts one review type's calls, not the interleaved stream.

    ``run_review`` drives the five review nodes in one superstep, so a single
    LLM object sees their calls in an order the scheduler picks. Counting the
    failure against the target review type's own sequential call order keeps the
    failure on the same batch every run, so this shuffles the callers, races
    them from a barrier, and still expects exactly the 2nd code_quality call to
    raise.
    """
    counts = {
        "code_quality": 3,
        "security": 2,
        "test_coverage": 2,
        "performance": 2,
        "requirements": 1,
    }
    for seed in range(8):
        order: list[str] = []
        for review, count in counts.items():
            order += [review] * count
        Random(seed).shuffle(order)

        llm = SpyLLM(fail_on=2, fail_review="code_quality")
        schedule = [(review, f"{seed}-{n}-{review}") for n, review in enumerate(order)]
        raised = _race(llm, schedule)

        # Exactly one call raised, and it was a code_quality one.
        assert len(raised) == 1, (seed, order)
        failed = raised[0]

        # It is the 2nd call of its own review type — the batch ordinal the node
        # fixes — and not the 2nd call of the shared stream.
        code_quality = llm.calls_for("code_quality")
        assert len(code_quality) == 3, seed
        assert _tag_of(code_quality[1][1]) == failed, (seed, order)
        # The other two code_quality calls were answered.
        others = {_tag_of(code_quality[0][1]), _tag_of(code_quality[2][1])}
        assert failed not in others, (seed, order)

        # Every call was recorded once under its own review type, and the
        # failure never disturbed the shared bookkeeping.
        assert len(llm.calls) == len(schedule), seed
        for review, count in counts.items():
            if review != "code_quality":
                assert len(llm.calls_for(review)) == count, (seed, review)
        assert len([e for e in llm.events if e.startswith("llm-call-")]) == len(schedule)


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
    # Named review type, not a bare call number: the five nodes share this LLM
    # across threads, so "the 2nd call" would land on whichever review type the
    # scheduler happened to run second.
    llm = SpyLLM(fail_on=2, fail_review="code_quality")
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


# --- nothing is filtered on the way out of a batch (FR-013) ---


class ConstantLLM:
    """Returns one canned payload for every code-review call, whatever batch.

    Answering every batch identically is what lets a test assert about a
    citation that does not match the batch that produced it.
    """

    model_name = "fake-model"

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        if "requirements-traceability" in system.lower():
            return "[]"  # keep requirement findings out of these assertions
        return self.payload


def _payload(
    file: str,
    line: int,
    snippet: str,
    finding_id: str = "cf-fixed",
    severity: str = "warning",
) -> str:
    return json.dumps(
        [
            {
                "id": finding_id,
                "file": file,
                "start_line": line,
                "start_col": 1,
                "end_line": line,
                "end_col": 1,
                "severity": severity,
                "title": f"Issue at {file}:{line}",
                "description": "Something worth fixing.",
                "recommendation": "Fix it.",
                "confidence": 0.9,
                "cited_snippet": snippet,
            }
        ]
    )


def _write_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def _review(tmp_path: Path, llm) -> tuple:
    """Run the whole pipeline over ``tmp_path`` with ``llm``; return outcome+report."""
    outcome = run_review(
        Settings(api_key="test-key", batch_chars=_BATCH_CHARS, max_batches=8),
        ReviewScope.PROJECT,
        str(tmp_path),
        llm=llm,
    )
    # A verification failure is not a run error, so the run still succeeds.
    assert outcome.exit_code == 0
    return (outcome, _read_report())


def test_finding_citing_a_file_not_in_scope_is_recorded_as_a_failure(tmp_path):
    """A payload naming a file outside the scope is not filtered away by the
    review type: verification records it as file_not_in_scope (FR-013)."""
    _write_project(tmp_path, {"src/a.py": _lines(25, "a"), "src/b.py": _lines(25, "b")})
    llm = ConstantLLM(_payload("src/does_not_exist.py", 3, "nope = 1"))
    outcome, report = _review(tmp_path, llm)

    failures = report.summary.verification_failures
    assert failures
    assert {f.reason_code for f in failures} == {VerificationReasonCode.FILE_NOT_IN_SCOPE}
    assert {f.file for f in failures} == {"src/does_not_exist.py"}
    assert "not in the reviewed file set" in failures[0].reason

    # Not silently missing: nothing is published, and every finding the LLM
    # produced is instead accounted for as a recorded failure.
    assert [f for f in report.code_findings if f.source is not FindingSource.SAST] == []
    assert len(failures) == report.summary.verification_failure_count == 8
    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert "src/does_not_exist.py" in markdown
    assert "Verification failures" in markdown


def test_finding_citing_an_in_scope_file_in_a_different_batch_is_kept(tmp_path):
    """Verification re-reads the cited file from the full scoped contents, not
    from the batch that produced the finding, so a cross-batch citation with a
    real snippet still verifies."""
    files = {
        "src/a.py": _lines(25, "a"),
        "src/b.py": _lines(25, "b"),
        "src/c.py": _lines(25, "c"),
    }
    _write_project(tmp_path, files)
    plan, _ = plan_batches(files, batch_chars=_BATCH_CHARS, max_batches=8)
    assert len(plan.batches) == 3
    assert {chunk.path for chunk in plan.batches[-1].chunks} == {"src/c.py"}

    # Every batch returns a citation into the LAST batch's file, so batches 1
    # and 2 produce citations into a batch they were never shown.
    line_no = 3
    snippet = files["src/c.py"].splitlines()[line_no - 1]
    _outcome, report = _review(tmp_path, ConstantLLM(_payload("src/c.py", line_no, snippet)))

    kept = [f for f in report.code_findings if f.file == "src/c.py"]
    assert kept
    assert {f.line_range.start_line for f in kept} == {line_no}
    # Verified as cited: no correction was needed.
    assert all(f.citation_adjusted_from is None for f in kept)
    assert report.summary.verification_failures == []


def test_finding_citing_a_dot_slash_path_is_recorded_as_a_failure(tmp_path):
    """Observed outcome: "./src/a.py" is not corrected and not verified.

    The cited path is looked up verbatim against the scope keys, and the scope
    holds "src/a.py", so the citation resolves to nothing. Verification records
    it as file_not_in_scope rather than normalising it away or dropping it
    silently (FR-013).
    """
    files = {"src/a.py": _lines(25, "a"), "src/b.py": _lines(25, "b")}
    _write_project(tmp_path, files)
    line_no = 3
    snippet = files["src/a.py"].splitlines()[line_no - 1]
    outcome, report = _review(tmp_path, ConstantLLM(_payload("./src/a.py", line_no, snippet)))

    failures = report.summary.verification_failures
    assert failures
    # Recorded, not corrected: a corrected citation would have been kept and so
    # would not appear here at all (correction only applies to
    # SNIPPET_FOUND_ELSEWHERE).
    assert {f.reason_code for f in failures} == {VerificationReasonCode.FILE_NOT_IN_SCOPE}
    assert {f.file for f in failures} == {"./src/a.py"}
    assert "not in the reviewed file set" in failures[0].reason
    # Not silently missing: the report discloses it.
    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert "./src/a.py" in markdown


# --- the render node is the grounding gate (FR-013) ---


def _section(markdown: str, heading: str) -> str:
    """The body of one ``## `` section of the report, up to the next one."""
    body = markdown.split(f"{heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def test_findings_all_failing_verification_are_excluded_from_the_report(tmp_path):
    """Verification kept nothing, so nothing unverified may reach the report.

    ``verified_code_findings`` is an empty list here, which is falsy — a render
    node that treated "empty" as "absent" would fall back to the raw
    ``code_findings`` and publish the 8 findings (4 review types × 2 batches) it
    had just rejected.
    """
    _write_project(tmp_path, {"src/a.py": _lines(25, "a"), "src/b.py": _lines(25, "b")})
    # severity=error so that a leak is also visible in the verdict.
    llm = ConstantLLM(_payload("src/does_not_exist.py", 3, "nope = 1", severity="error"))
    outcome, report = _review(tmp_path, llm)

    # Rejected findings are not in the report, whatever their source.
    assert [f for f in report.code_findings if f.source != FindingSource.SAST] == []
    assert report.code_findings == []
    # Nor do they drive any count...
    assert report.summary.total_code_findings == 0
    assert report.summary.severity_counts == {}
    assert report.summary.category_counts == {}
    # ...or the verdict. All 8 are error-severity, so publishing even one would
    # force RequiresModification; the verdict that remains comes from the
    # requirements node's own "unclear" finding, not from a rejected citation.
    assert Severity.ERROR not in report.summary.severity_counts
    assert report.summary.verdict is not Verdict.REQUIRES_MODIFICATION
    # Nor do they appear in the Code Findings section.
    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    code_section = _section(markdown, "## Code Findings")
    assert "No code findings." in code_section
    assert "src/does_not_exist.py" not in code_section
    # They are all accounted for as recorded failures instead of vanishing.
    failures = report.summary.verification_failures
    assert len(failures) == report.summary.verification_failure_count == 8
    assert {f.reason_code for f in failures} == {VerificationReasonCode.FILE_NOT_IN_SCOPE}
    assert {f.file for f in failures} == {"src/does_not_exist.py"}


def test_render_withholds_non_sast_findings_when_verification_did_not_run(
    tmp_path, monkeypatch
):
    """When the verification node never produced a verdict, only SAST ground truth
    may be published; the rest are withheld and the run is marked incomplete."""
    files = {"src/a.py": _lines(25, "a"), "src/b.py": _lines(25, "b")}
    _write_project(tmp_path, files)

    # One SAST finding, at a location no LLM finding cites, so it survives the
    # security node's own SAST de-duplication.
    def _stub(files_seen, *, scope_value, rules=None, opengrep_bin="opengrep"):
        return OpengrepResult(
            findings=[
                {
                    "path": "src/b.py",
                    "start": {"line": 7, "col": 1},
                    "end": {"line": 7, "col": 10},
                    "check_id": "py.lang.security.audit.eval-detected",
                    "extra": {
                        "severity": "WARNING",
                        "message": "Audit: use of eval detected",
                        "lines": files_seen["src/b.py"].splitlines()[6],
                    },
                }
            ],
            rules="r",
        )

    monkeypatch.setattr("veritas.review.nodes.scope.collect_sast", _stub)

    def _exploding_verify_node(runtime):
        def verify_node(state):
            raise RuntimeError("verification exploded")

        return verify_node

    monkeypatch.setattr(
        "veritas.review.nodes.verification.make_verify_node", _exploding_verify_node
    )

    # The LLM citation is genuine (real file, real line, real snippet): it is
    # withheld because verification never ran, not because it was invalid.
    line_no = 3
    snippet = files["src/a.py"].splitlines()[line_no - 1]
    outcome = run_review(
        Settings(api_key="test-key", batch_chars=_BATCH_CHARS, max_batches=8),
        ReviewScope.PROJECT,
        str(tmp_path),
        llm=ConstantLLM(_payload("src/a.py", line_no, snippet, severity="error")),
    )
    report = _read_report()

    # Only SAST ground truth is published.
    assert [f.source for f in report.code_findings] == [FindingSource.SAST]
    assert report.code_findings[0].title == "Audit: use of eval detected"
    # The 8 unverifiable LLM findings are counted as withheld, in the error.
    assert report.summary.verification_failures == []
    assert "verification did not run" in report.run.error
    assert "8 unverified finding(s) withheld" in report.run.error
    # And the report is marked incomplete, with a non-zero exit code.
    assert report.run.report_status == ReportStatus.INCOMPLETE
    assert outcome.exit_code == 2
