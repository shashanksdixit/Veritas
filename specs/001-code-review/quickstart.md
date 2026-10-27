# Quickstart: Veritas Code Review (001-code-review)

**Date**: 2026-09-16
**Branch**: 001-code-review

Validation scenarios proving the feature works end-to-end. Prerequisites listed per scenario.

---

## Prerequisites (all scenarios)

1. Python 3.12+ installed (`.python-version` = 3.12)
2. Veritas installed: `pip install -e .` from repo root (installs Typer, LangGraph, openai, httpx)
3. OpenGrep binary on PATH (v1.30.1; download from github.com/opengrep/opengrep/releases)
4. API key for the configured LLM backend set: `export VERITAS_API_KEY=...` (the default backend is OpenAI direct, `openai:gpt-4o-mini` at `https://api.openai.com/v1`, so an OpenAI key; an OpenRouter key only when `base_url` points at OpenRouter, as in Scenario 5)
5. A test repository with Python/JS/Java code

---

## Scenario 1: Review a whole project (FR-001 scope=project, US1/US2)

```bash
veritas review --scope project --target ./myproject
```

**Expected**:
- stderr: `[info] Scanning target in scope: ./myproject` + `[info] Detected: ...` lines for the detected languages/frameworks, and the ZDR-off warning for the default backend (`[warn] ZDR is OFF: zero data retention cannot be enforced per request on this backend; data retention is governed by your account agreement with openai. ...`)
- stdout: Compact summary with counts by severity/category, requirement-status counts, overall verdict, one `SAST: ran (<n> result(s))` line (followed by ` — <reason>` when it ran degraded, for example `<n> file(s) not scanned (OpenGrep's default ignore list: tests/)`), one `Context: <status> (<sources>)` line (`Context: unavailable — <reason>` when no project context was found), and `Report: veritas-report-<timestamp>.md`
- File: `veritas-report-<timestamp>.md` created in the current directory with full Markdown report
- Report contains sections for all 5 review types (code quality, security, requirements, test coverage, performance); each code finding (code quality, security, test coverage, performance) includes its own recommendation text rather than a separate 'suggested changes' section, and each requirement finding carries no recommendation but a non-empty explanation of its status
- Security findings carry a source label in their heading: `*(SAST)*` or `*(LLM-identified, citation verified)*`
- If OpenGrep is not installed: stderr `[warn] OpenGrep not found on PATH` and `[warn] Security coverage degraded: OpenGrep not found on PATH; LLM-identified findings remain subject to re-verification.`, stdout `SAST: not run — OpenGrep not found on PATH`, the report header `- **SAST**: not run — OpenGrep not found on PATH (rules configured: ...)`; security findings are LLM-identified only

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
- stdout summary shows `Report: veritas-report-<timestamp>.md`
- Without `VERITAS_GITHUB_TOKEN`: stderr `[error] PR review requires a GitHub token: set VERITAS_GITHUB_TOKEN (never commit tokens; see contracts/cli.md).`, exit code 1, no report written

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
- If posting fails (e.g. revoke the token first): stderr shows `[warn] Failed to post report as github comment: <failure, e.g. 403 Forbidden>; report saved to <report path>`; exit code is still 0; the report file itself is valid and complete
- `--post` used with `--scope file` (or module/project) is rejected: stderr `[error] --post is only valid with --scope pr (FR-028); rejected before any review work starts.`, exit code 1, no review work performed

**Validates**: FR-016, FR-028, SC-007

---

## Scenario 5: ZDR toggle (FR-021)

ZDR is enforced per request only on OpenRouter, so this scenario configures OpenRouter explicitly (with an OpenRouter API key in `VERITAS_API_KEY`).

```bash
export VERITAS_BASE_URL=https://openrouter.ai/api/v1
export VERITAS_MODEL=openai:openai/gpt-4o-mini

# With ZDR off (default):
veritas review --scope file --target ./myproject/src/util.py
# Verify: LLM request does NOT include provider.zdr in body; stderr carries the OpenRouter ZDR-off warning

# With ZDR on:
export VERITAS_ZDR=true
veritas review --scope file --target ./myproject/src/util.py
# Verify: LLM request includes provider.zdr: true in body

# ZDR on with the default backend (OpenAI direct) is refused:
unset VERITAS_BASE_URL VERITAS_MODEL
veritas review --scope file --target ./myproject/src/util.py
# Verify: exit code 1, stderr "[error] ZDR is only supported with OpenRouter. ... Nothing was sent. ...", no LLM request made
```

**Validates**: FR-021

---

## Scenario 6: Suppress a finding (FR-017, US3)

```bash
# First: get a finding ID from the report
veritas review --scope file --target ./myproject/src/auth.py
# Note a finding ID from the report file (the stdout summary lists headline findings by file:line, not by ID)

# Suppress it (or use --file <path> --line <n> instead of --finding-id)
veritas suppress --finding-id "<finding-id>" --reason "Accepted risk"
# Verify: stdout "Suppressed. Fingerprint: <sha256>  (use with 'veritas unsuppress --fingerprint' to reverse)"
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

## Scenario 8: LLM provider failure (FR-027)

Settings are loaded once, when the run starts, so the API key cannot change
partway through a run. Simulate a provider failure with a key that is invalid
from the start:
```bash
export VERITAS_API_KEY=invalid
veritas review --scope project --target ./myproject
```

**Expected**:
- Every LLM call is rejected by the provider; the run does not stop, and each review type records its failure
- stderr: each failed call writes an `[error] LLM call <model> (<n> ms) failed: <reason> {...}` line and each failed batch a `[warn] <review type>: batch <i>/<n> failed (files: <files>): <reason>` line, for example for a one-file project (`<reason>` is the provider's error message as Veritas summarizes it, `<provider> <status>: <message> [<reason code>]`, so its wording depends on the provider):
  ```text
  [error] LLM call gpt-4o-mini (125 ms) failed: openai 401: Incorrect API key provided: invalid. [invalid_api_key] {"completion_tokens": null, "latency_ms": 125.0, "model": "gpt-4o-mini", "prompt_tokens": null, "total_tokens": null}
  [warn] security: batch 1/1 failed (files: src/app.py): openai 401: Incorrect API key provided: invalid. [invalid_api_key]
  ```
- stdout: a `Report status: incomplete — ...` line before the `Report:` line, counting the failed batch calls per review type and naming the reason, for example:
  ```text
  Report status: incomplete — 4 LLM batch call(s) failed (code_quality: 1, performance: 1, security: 1, test_coverage: 1): openai 401: Incorrect API key provided: invalid. [invalid_api_key] (see Failed batches in the report); requirements_node: openai 401: Incorrect API key provided: invalid. [invalid_api_key]
  ```
- Report file written; its header shows ``- **Report status**: `incomplete` `` and its `### Failed batches (FR-027)` section lists every failed batch with its reason
- Exit code 2 (partial review)
- Not mistaken for a complete review

**Validates**: FR-027, edge case

---

## Scenario 9: Verification failure (FR-013)

Create a finding that cites a non-existent file/line (e.g. by a hallucinating LLM). The verification node should:
1. Re-read the cited file/line
2. Fail to confirm → exclude from report
3. Record as a verification-failure note in report summary

**Expected**:
- The report's `### Verification failures (FR-013)` section lists the failure in a table (Finding ID, File, Lines, Code, Reason); the stdout summary carries `Verification failures: <n> finding(s) excluded (FR-013), see full report`
- The unverified finding does NOT appear in the findings list
- Note is visible and attributable (includes finding ID, file, reason code and reason)

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
- One requirement finding `No requirements documentation found` with status `unclear`, explaining that no requirements documentation was found in the reviewed scope
- No invented requirements
- All other review types proceed normally

**Validates**: FR-006, FR-007, FR-008

---

## Scenario 12: Unsupported language files (FR-022)

Run against a project containing `.cob`, `.lua`, and `.py` files.

**Expected**:
- `.cob` and `.lua` files are skipped and explicitly noted in the report
- `.py` files are fully reviewed
- Coverage shows "| Files skipped (unsupported language) | 2 |", lists the `.cob` and `.lua` paths, and the compact line includes "2 unsupported"
- Running the review again from the project directory still shows 2: the earlier `veritas-report-*.md` and the `.veritas/` directory are always skipped (a report written with a custom `--output` path inside the project would be listed)

**Validates**: FR-022
