"""Integration tests — project-aware review (T057, FR-009/FR-010/FR-011)."""

from tests.conftest import FakeLLM, default_fake_llm

from veritas.models.entities import ReviewScope
from veritas.review.graph import run_review


def test_project_context_flows_into_prompts(sample_project, settings):
    llm = default_fake_llm()
    run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    all_user = "\n".join(user for _sys, user in llm.calls)

    assert "Python >=3.12" in all_user
    assert "Prefer early returns over nested ifs." in all_user
    assert "Flag any use of eval()" in all_user


def test_requirements_doc_reaches_requirements_prompt(sample_project, settings):
    llm = default_fake_llm()
    out = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    user = next(user for sys, user in llm.calls if "requirements-traceability" in sys.lower())
    assert "REQ-1" in user
    assert "SATISFIED" in user
    from pathlib import Path

    md = Path(str(out.report_path)).read_text(encoding="utf-8")
    assert "REQ-1" in md
    assert "- `src/app.py:2`" in md  # verified requirement evidence


def test_empty_project_uses_context_note(tmp_path, settings):
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.py").write_text("x = 1\n", encoding="utf-8")
    llm = FakeLLM({})
    out = run_review(settings, ReviewScope.PROJECT, str(tmp_path), llm=llm)
    assert out.exit_code == 0
    user_prompts = [user for _sys, user in llm.calls if user]
    assert any("Project context unavailable" in u for u in user_prompts)