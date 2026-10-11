"""Unit tests — do-not-report finding filters (T092, FR-014).

Two classes of finding are discarded after the LLM findings are built and
before verification, each counted and logged once per review type: a finding
whose recommendation states that no change is needed, and the ``Optional[`` /
``| None`` idiom opinion. Everything else — including a recommendation that
merely mentions "no change" mid-sentence, and a real ``Optional[]`` annotation
issue — is kept.
"""

import json

from veritas.models.entities import (
    Category,
    CodeFinding,
    LineRange,
    Severity,
)
from veritas.review.batching import plan_batches
from veritas.review.nodes.common import (
    discard_do_not_report,
    discard_reason,
    llm_findings,
)


def _finding(
    recommendation: str,
    snippet: str | None = "x = 1",
    *,
    severity: Severity = Severity.WARNING,
) -> CodeFinding:
    return CodeFinding(
        id="f-filter",
        file="src/app.py",
        line_range=LineRange(start_line=1, start_col=1, end_line=1, end_col=1),
        severity=severity,
        category=Category.CODE_QUALITY,
        title="A finding",
        description="d",
        recommendation=recommendation,
        confidence=0.9,
        cited_snippet=snippet,
    )


_NON_CHANGE_OPENINGS = (
    "no change",
    "no changes",
    "no action",
    "none needed",
    "nothing to change",
)


def test_every_no_change_opening_is_discarded():
    for opening in _NON_CHANGE_OPENINGS:
        finding = _finding(recommendation=f"{opening} is required here")
        assert discard_reason(finding) == "no-change", opening
        kept, no_change, idiom = discard_do_not_report([finding])
        assert kept == [], opening
        assert (no_change, idiom) == (1, 0), opening


def test_no_change_match_is_case_insensitive_and_trimmed():
    finding = _finding(recommendation="  No Change to this module.  ")
    assert discard_reason(finding) == "no-change"
    kept, no_change, idiom = discard_do_not_report([finding])
    assert kept == [] and (no_change, idiom) == (1, 0)


def test_a_recommendation_mentioning_no_change_mid_sentence_is_kept():
    # "no change to the API" starts inside the sentence, so the finding is real
    # and must not be filtered (FR-014).
    finding = _finding(recommendation="There is no change to the API required, but the loop is hot.")
    assert discard_reason(finding) is None
    kept, no_change, idiom = discard_do_not_report([finding])
    assert kept == [finding] and (no_change, idiom) == (0, 0)


def test_optional_idiom_recommended_over_union_none_snippet_is_discarded():
    finding = _finding(
        recommendation="Use `Optional[str]` here instead of writing `str | None`.",
        snippet="def lookup() -> str | None:",
    )
    assert discard_reason(finding) == "idiom-only"
    kept, no_change, idiom = discard_do_not_report([finding])
    assert kept == [] and (no_change, idiom) == (0, 1)


def test_optional_recommended_over_typer_option_snippet_is_a_real_issue_and_kept():
    # `Optional[str]` over a `typer.Option(None, ...)` default is a genuine
    # annotation defect, not a style opinion about `| None` (FR-014).
    finding = _finding(
        recommendation="Use `Optional[str]` for the optional flag so Typer accepts an explicit None default.",
        snippet="config: str = typer.Option(None, ...)",
    )
    assert discard_reason(finding) is None
    kept, no_change, idiom = discard_do_not_report([finding])
    assert kept == [finding] and (no_change, idiom) == (0, 0)


def test_union_none_in_snippet_alone_does_not_discard():
    # Both halves are required: a snippet with `| None` but no `Optional[`
    # recommendation is a finding about something else.
    finding = _finding(
        recommendation="Handle the None case before indexing.",
        snippet="value: str | None = get()",
    )
    assert discard_reason(finding) is None


def test_each_discarded_finding_is_attributed_to_exactly_one_reason():
    findings = [
        _finding(recommendation="no changes needed"),
        _finding(recommendation="use Optional[List[int]]", snippet="xs: list[int] | None = None"),
        _finding(recommendation="keep the None here", snippet="def f() -> None:"),
        _finding(recommendation="refactor the loop"),
    ]
    kept, no_change, idiom = discard_do_not_report(findings)
    assert len(kept) == 2
    assert (no_change, idiom) == (1, 1)


# ---------------------------------------------------------------------------
# The discard is counted and logged once per review type (FR-014)
# ---------------------------------------------------------------------------


class _OneShotLLM:
    """Answers every code-review prompt with one canned JSON payload list."""

    model_name = "fake-model"

    def __init__(self, payloads: list[dict]) -> None:
        self.text = json.dumps(payloads)
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.text


class _RecordingLog:
    def __init__(self) -> None:
        self.info_lines: list[str] = []

    def info(self, message: str, **structured) -> None:
        self.info_lines.append(message)

    def warn(self, message: str, **structured) -> None:
        pass


def _plan() -> object:
    files = {"src/app.py": 'x = 1\nprint("hello")\n'}
    plan, warnings = plan_batches(files, batch_chars=100_000, max_batches=8)
    assert warnings == []
    return plan


def _payload(recommendation: str, snippet: str = "print(\"hello\")") -> dict:
    return {
        "file": "src/app.py",
        "start_line": 2,
        "start_col": 1,
        "end_line": 2,
        "end_col": 1,
        "severity": "warning",
        "title": "t",
        "description": "d",
        "recommendation": recommendation,
        "confidence": 0.9,
        "cited_snippet": snippet,
    }


def test_discarded_findings_are_counted_and_logged_once_for_the_review_type():
    llm = _OneShotLLM(
        [
            _payload("no action required"),
            _payload("nothing to change here"),
            _payload("use Optional[int]", "def f() -> int | None:"),
            _payload("rework the naming"),
        ]
    )
    log = _RecordingLog()
    findings, errors = llm_findings(
        llm,
        _plan(),
        "code_quality",
        None,
        category=Category.CODE_QUALITY,
        log=log,
    )
    assert errors == []
    assert llm.calls == 1
    # Only the real finding survives the filter.
    assert len(findings) == 1
    assert findings[0].recommendation == "rework the naming"
    # One honest ASCII line per review type: the counts say why anything was
    # withheld, and the line is not repeated per batch or per finding.
    assert log.info_lines.count(
        "code_quality: discarded 3 finding(s): 2 no-change, 1 idiom-only"
    ) == 1


def test_no_discard_line_is_logged_when_nothing_is_discarded():
    llm = _OneShotLLM([_payload("rework the naming")])
    log = _RecordingLog()
    findings, errors = llm_findings(
        llm,
        _plan(),
        "code_quality",
        None,
        category=Category.CODE_QUALITY,
        log=log,
    )
    assert errors == [] and len(findings) == 1
    assert not [line for line in log.info_lines if "discarded" in line]


def test_the_filter_never_runs_on_requirement_findings():
    # llm_findings is the code-findings path only; the requirement path has no
    # recommendation field to filter on and must not be touched. Pin the surface:
    # discard helpers take CodeFinding only, so a requirement finding (which is
    # not a CodeFinding) cannot even be passed.
    from veritas.models.entities import RequirementFinding, RequirementStatus

    req = RequirementFinding(
        requirement_ref="FR-001",
        requirement_text="x",
        status=RequirementStatus.SATISFIED,
        evidence=[],
        explanation="e",
    )
    assert not isinstance(req, CodeFinding)