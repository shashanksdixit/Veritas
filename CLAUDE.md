# Veritas: working notes for Claude Code

Veritas is a Python CLI code reviewer (LangGraph/LangChain). Specs: specs/001-code-review/
(spec.md, data-model.md, tasks.md, contracts/). Constitution: .specify/memory/constitution.md (v6.0.1).

## Environment
- Windows, Git Bash. Corporate SSL inspection: use `uv run --native-tls ...`; curl needs `--ssl-no-revoke`.
- Tests: `uv run --native-tls pytest` (about 815 tests). One test needs `opengrep` on PATH.
- A full run can be bounded with `timeout 1200 uv run --native-tls pytest ...`.

## Rules
1. Every behaviour change updates: spec.md (REPLACE conflicting wording; never append text that contradicts existing text), tasks.md (next UNUSED task ID), CHANGELOG.md, and tests.
2. A change without tests is not done. Show new tests in full, not just their names.
3. Always show real command output (for example the pytest tail). Never summarise results you did not run.
4. Never call a failing test "pre-existing" unless you prove it: `git stash`, run that test at HEAD, show it fails, `git stash pop`.
5. Do not commit or push. The user runs a gated commit.
6. Tests must not contain secret-shaped literals; build them with fake_secret() from tests/conftest.py.
7. Do not create scratch files in the repository.
8. Work one change at a time and explain at a beginner-to-intermediate level.
