# Quickstart: Veritas Code Review (001-code-review)

**Date**: 2026-09-16
**Branch**: 001-code-review

Validation scenarios proving the feature works end-to-end. Prerequisites listed per scenario.

---

## Prerequisites (all scenarios)

1. Python 3.12+ installed (`.python-version` = 3.12)
2. Veritas installed: `pip install -e .` from repo root (installs Typer, LangGraph, openai, httpx)
3. OpenGrep binary on PATH or at a configured path (v1.30.0; download from github.com/opengrep/opengrep/releases)
4. OpenRouter API key set: `export VERITAS_API_KEY=sk-or-...`
5. A test repository with Python/JS/Java code

---

## Scenario 1: Review a whole project (FR-001 scope=project, US1/US2)

```bash
veritas review --scope project --target ./myproject
```

**Expected**:
- stderr: `[info] Scanning project: ./myproject` + language/framework detection messages
- stdout: Compact summary with counts by severity/category, requirement-status counts, overall verdict
- File: `./veritas-report-<timestamp>.md` created with full Markdown report
- Report contains sections for all 5 review types (code quality, security, requirements, test coverage, performance); each finding includes its own recommendation text rather than a separate 'suggested changes' section
- Security findings include `source` label: either `(SAST)` or `(LLM-verified)`
- If OpenGrep is not installed: `[warn] OpenGrep not found on PATH` + security findings are LLM-identified only + report states specific reason

**Validates**: FR-001, FR-003, FR-009, FR-012, FR-015, FR-016, FR-022, FR-023, FR-025, SC-001, SC-008

---

## Scenario 2: Review a single file (FR-001 scope=file)

```bash
veritas review --scope file --target ./myproject/src/auth.py
```

**Expected**:
- Review scoped to exactly `src/auth.py`
- No findings from other files
- Report scoped to single file

**Validates**: FR-001, SC-008

---

## Scenario 3: Review a pull request (FR-002, US1)

```bash
export VERITAS_GITHUB_TOKEN=ghp_...
veritas review --scope pr --target "myorg/myrepo#42"
```

**Expected**:
- Veritas fetches PR files and contents via GitHub API (no local git state read)
- Report includes all review types for files changed in the PR
- stdout summary shows `Report: ./veritas-report-<timestamp>.md`

**Validates**: FR-002, US1 acceptance scenarios, SC-001

---

## Scenario 4: Post report as PR comment (FR-028, SC-007)

```bash
export VERITAS_GITHUB_TOKEN=ghp_...
veritas review --scope pr --target "myorg/myrepo#42" --post
```

**Expected**:
- Report is generated and written to disk as usual
- The full rendered Markdown is additionally posted as a comment on PR #42 via the GitHub API
- Verify the Markdown renders correctly on GitHub's PR comment interface (no broken formatting, tables render, code blocks render)
- If posting fails (e.g. revoke the token first): stderr shows a `[warn]` line naming the failure and the report file path; exit code is still 0; the report file itself is valid and complete
- `--post` used with `--scope file` (or module/project) is rejected: exit code 1, clear stderr diagnostic, no review work performed

**Validates**: FR-016, FR-028, SC-007

---

## Scenario 5: ZDR toggle (FR-021)

```bash
# With ZDR off (default):
veritas review --scope file --target ./myproject/src/util.py
# Verify: LLM request does NOT include provider.zdr in body

# With ZDR on:
export VERITAS_ZDR=true
veritas review --scope file --target ./myproject/src/util.py
# Verify: LLM request includes provider.zdr: true in body
```

**Validates**: FR-021

---

## Scenario 6: Suppress a finding (FR-017, US3)

```bash
# First: get a finding ID from the report
veritas review --scope file --target ./myproject/src/auth.py
# Note a finding ID from stdout summary

# Suppress it
veritas suppress --finding-id "<finding-id>" --reason "Accepted risk"
# Verify: .veritas/suppressions.json created/updated with fingerprint entry

# Re-run: suppressed finding should NOT appear
veritas review --scope file --target ./myproject/src/auth.py
# Verify: the suppressed finding is absent from the report

# Edit an unrelated line in the same file, re-run: finding should still be suppressed
# Edit the flagged code snippet itself, re-run: finding should reappear
```

**Validates**: FR-017, FR-018, SC-006

---

## Scenario 7: Un-suppress a finding

```bash
veritas unsuppress --fingerprint "<sha256>"
# Re-run: previously suppressed finding should reappear
```

**Validates**: FR-018, SC-006

---

## Scenario 8: LLM provider failure mid-run (FR-027)

Simulate by setting an invalid API key after the first 2 review types complete:
```bash
export VERITAS_API_KEY=invalid
veritas review --scope project --target ./myproject
```

**Expected**:
- Exit code 2 (partial review)
- Report file written with completed review types
- Report status: `incomplete`
- Clear error message in report and stderr
- Not mistaken for a complete review

**Validates**: FR-027, edge case

---

## Scenario 9: Verification failure (FR-013)

Create a finding that cites a non-existent file/line (e.g. by a hallucinating LLM). The verification node should:
1. Re-read the cited file/line
2. Fail to confirm → exclude from report
3. Record as a verification-failure note in report summary

**Expected**:
- Verification-failure note appears in report summary
- The unverified finding does NOT appear in the findings list
- Note is visible and attributable (includes finding ID, file, reason)

**Validates**: FR-013, SC-002, SC-003

---

## Scenario 10: Ad-hoc module review (US2, FR-001 scope=module)

```bash
veritas review --scope module --target ./myproject/src/auth/
```

**Expected**:
- Review scoped to exactly `src/auth/` directory
- No findings from files outside that directory

**Validates**: FR-001, US2 acceptance scenarios

---

## Scenario 11: No requirements found (edge case)

Run against a project with no `spec.md`, `requirements.md`, or equivalent.

**Expected**:
- Requirement findings report status `unclear` with explanation "No requirements documentation found"
- No invented requirements
- All other review types proceed normally

**Validates**: FR-006, FR-007, FR-008

---

## Scenario 12: Unsupported language files (FR-022)

Run against a project containing `.cob`, `.lua`, and `.py` files.

**Expected**:
- `.cob` and `.lua` files are skipped and explicitly noted in the report
- `.py` files are fully reviewed
- Report contains a note listing skipped files/reasons

**Validates**: FR-022
