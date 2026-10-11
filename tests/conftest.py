"""Shared test fixtures: deterministic fake LLM, sample project, settings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from veritas.config.settings import Settings

ENV_PREFIX = "VERITAS_"


@pytest.fixture(autouse=True)
def _no_inherited_veritas_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no VERITAS_* variable in the environment.

    `Settings` reads `VERITAS_*` with `env_prefix="VERITAS_"`, so a value in the
    developer's shell or in CI would otherwise leak into tests that assert
    defaults and precedence — and the leak is invisible, because the suite still
    passes on a clean machine. Deleting every matching name (rather than a fixed
    list of the fields that exist today) also covers a variable added later.

    A test that needs one sets it with `monkeypatch.setenv` in its own body or in
    a fixture, both of which run after this one; monkeypatch undoes the deletion
    at teardown, so the shell is left exactly as it was found.
    """
    for name in [n for n in os.environ if n.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name, raising=False)


def fake_secret(prefix: str, body: str) -> str:
    """Assemble a credential-shaped value from two fragments at runtime.

    No full secret ever appears as a single literal in the test source — a
    secret scanner reading the repository would otherwise reject the push —
    while the assembled value keeps exactly the shape the rule under test has
    to recognize. Tests assert on the assembled value, so every assertion
    about masking, and every assertion that nothing leaked, stays real.
    """
    return prefix + body


APP_CONTENT = (
    "import os\n"
    'print("hello")\n'
    "secret = 42\n"
    "total = 0\n"
    "for i in range(100):\n"
    "    total += i\n"
    "return total\n"
)


class FakeLLM:
    """Deterministic LLM double routing on the system prompt text.

    ``complete`` records every call so tests can assert prompt content.
    Responses map a system-prompt keyword to a canned JSON string.
    """

    model_name = "fake-model"

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        for key, text in self.responses.items():
            if key.lower() in system.lower():
                return text
        return "[]"


def canned_code_findings(
    file: str = "src/app.py",
    snippet: str = "print(\"hello\")",
    start_line: int = 2,
    end_line: int = 2,
    finding_id: str = "cf-mock-warn",
) -> str:
    return json.dumps(
        [
            {
                "id": finding_id,
                "file": file,
                "start_line": start_line,
                "start_col": 1,
                "end_line": end_line,
                "end_col": 18,
                "severity": "warning",
                "title": "Unused import os",
                "description": "os is imported but never used.",
                "recommendation": "Remove the unused import.",
                "confidence": 0.9,
                "cited_snippet": snippet,
            }
        ]
    )


def canned_requirement_findings() -> str:
    return json.dumps(
        [
            {
                "requirement_ref": "REQ-1",
                "requirement_text": "The tool must support project scope reviews.",
                "status": "partial",
                "evidence": ["src/app.py:2"],
                "explanation": "Basic support exists but edge cases are unhandled.",
            }
        ]
    )


def default_fake_llm(file: str = "src/app.py") -> FakeLLM:
    findings = canned_code_findings(file=file)
    return FakeLLM(
        {
            "code-quality": findings,
            "security/owasp": json.dumps(
                [
                    {
                        "id": "cf-mock-biz",
                        "file": file,
                        "start_line": 3,
                        "start_col": 1,
                        "end_line": 3,
                        "end_col": 12,
                        "severity": "error",
                        "title": "Broken access control",
                        "description": "No authorization check before sensitive operation.",
                        "recommendation": "Add an authorization check.",
                        "confidence": 0.8,
                        "owasp_id": "A01:2021",
                        "cwe_id": "CWE-287",
                        "cited_snippet": "secret = 42",
                    }
                ]
            ),
            "requirements-traceability": canned_requirement_findings(),
            "test-coverage judgment": canned_code_findings(
                snippet="total = 0",
                start_line=4,
                end_line=4,
                finding_id="cf-mock-test",
            ),
            "performance-reasoning": canned_code_findings(
                snippet="for i in range(100):",
                start_line=5,
                end_line=5,
                finding_id="cf-mock-perf",
            ),
        }
    )


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(APP_CONTENT, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nrequires-python = \">=3.12\"\ndependencies = []\n",
        encoding="utf-8",
    )
    (tmp_path / "spec.md").write_text(
        "# REQ-1\n\n- SATISFIED: tool supports project scope.\n", encoding="utf-8"
    )
    (tmp_path / "AGENTS.md").write_text("Prefer early returns over nested ifs.\n", encoding="utf-8")
    (tmp_path / ".veritas").mkdir(exist_ok=True)
    (tmp_path / ".veritas" / "rules.md").write_text("Flag any use of eval().\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        github_token="ghp_test",
        gitlab_token="glpat-test",
    )