# Changelog

All notable Veritas changes are recorded here. Every entry that touches the
review output schema (or the `Report` / `CodeFinding` / `RequirementFinding`
models) records the affected `Report.schema_version` and a one-line description
of what changed at that version — the migration-path record required by
constitution Principle VIII / FR-016.

## [Unreleased]

### Added
- Report schema **1.8.0**: adds `Report.failed_batches`, one `FailedBatch`
  record (review type, batch, total, files, reason) per failed LLM batch call,
  and a collapsible "Failed batches" table in the Markdown report after
  Verification failures (FR-027). Migration: earlier reports remain valid (the
  field defaults to an empty list); readers that parsed per-batch details out
  of `run.error` should read `failed_batches` instead.
- Provider error summaries drop URLs
- [llm] max_output_tokens bounds every LLM response and truncation is logged

### Changed
- Run failure output is concise: `run.error` and the compact
  `Report status: incomplete — ...` line (at most 500 characters in total) give
  the failed batch count, the count per review type and each distinct reason
  once (at most 3, then "and N more"), plus any non-batch errors, each once.
  Previously every per-batch error was concatenated (about 10,000 characters
  for 40 identical failures).
- Operational polish: [llm] max_concurrency, a single retry for OpenRouter's in-flight 402, atomic log lines, UTF-8 console output on Windows, friendly configuration errors, LF line endings, README setup notes.

### Fixed
- A response truncated by [llm] max_output_tokens no longer crashes the LLM call with `AttributeError`; it logs a warning naming the configured limit.
- SAST findings are no longer discarded on Windows: OpenGrep result paths (backslashes, 8.3 short names) are mapped to reviewed files; unmappable results are reported, not dropped; OpenGrep output is decoded as UTF-8.
- A requirement with an unconfirmable evidence reference is no longer dropped from
  the report: the bad reference is removed (and recorded), and the requirement
  becomes unclear only if no evidence remains. A failed requirements batch is named
  in the explanation.
- Python source modules whose names start with test_ (e.g. test_index.py) are no
  longer treated as test files, so they are reviewed as application code and kept
  out of the test index.
- Findings that quote a secret now verify (both sides are redacted before
  comparison); prompts require verbatim contiguous snippets. Prompt version
  **1.7.0**.

### Added
- Report schema **1.6.0**: adds `Summary.duplicates_merged`. Migration:
  earlier reports remain valid (the field defaults to 0).
- Precise do-not-report filters, fingerprint de-duplication, and a clearer LLM
  source label: a recommendation that says no change is needed and the
  `Optional[` / `| None` idiom opinion are discarded and logged instead of
  reported (FR-014); kept code findings sharing a suppression fingerprint are
  merged after verification with the count logged and shown in the summary
  (FR-013); the report labels an LLM-identified finding "LLM-identified,
  citation verified" and says what that does and does not mean (FR-012).
- Report schema **1.5.0**: adds the `not_addressed` requirement status. Migration:
  earlier reports remain valid.
- In PR scope, requirements with no evidence in the PR are reported as
  not_addressed and do not affect the verdict; gap is reported only for project
  and module reviews. Prompt version 1.9.0.
- Structured requirements are evaluated against every batch and merged
  deterministically; a gap is reported only when every batch found no
  implementation and coverage was complete. Prompt version **1.8.0**.
- Requirement sources are discovered by pattern (spec-kit specs/*/spec.md first)
  and spec-kit FR lines are extracted deterministically.
- Report schema **1.4.0**: adds optional `CodeFinding.severity_adjusted_from`.
  Migration: earlier reports remain valid; the field defaults to null.
- Prompt version **1.6.0**: code review prompts carry a severity rubric with
  category limits and a do-not-report list; test-coverage findings are capped at
  warning.
- Batching reviews application code before test files, so when the batch limit is
  reached it is tests, not application code, that go unreviewed.
- The test index is ordered by relevance per batch (name match, then Python
  import match, then the rest), with a 16000-character per-batch limit, so
  truncation drops the least relevant tests instead of an alphabetical tail.
  Prompt version **1.5.0**.
- Prompt version **1.4.0**: the test coverage review is shown an index of every
  test file in scope, with the Python test names inside each, so a batch no longer
  looks untested when its tests are in another batch; no report schema change.
- Prompt version **1.3.0**: code review prompts explain partial-file chunk headers
  and forbid findings that exist only because code outside the shown range is not
  visible; no report schema change.
- Prompt version **1.2.0**: review prompts require a finding's line range to
  start and end on the first and last lines of its cited snippet, with a worked
  example, and to quote the exact lines a finding is about; no report schema
  change.
- Prompt version **1.1.0**: review prompts present line-numbered code and
  instruct the model to cite shown line numbers; no report schema change.
- Initial Veritas implementation for the `001-code-review` feature: CLI review
  (`project` / `module` / `file` / `pr` scopes), five review types (code
  quality, security/OWASP, requirements, test coverage, performance), grounded
  citation verification, suppression allowlist, project-aware review, Markdown
  report plus compact stdout summary, and optional PR-comment posting.
- Report schema **1.3.0**: additive change (FR-029). Adds `Report.coverage` (a
  `Coverage` with batch settings, batches used, and reviewed, split, excluded
  and not-reviewed file lists) and the `ExcludedFile` model. Migration: earlier
  reports remain valid; `coverage` defaults to null.
- Report schema **1.2.0**: additive change (FR-013). Adds the optional
  `CodeFinding.citation_adjusted_from` field (a `LineRange`), recording the
  reviewer's original cited range when verification corrected a near-miss
  citation. Migration: 1.0.0 and 1.1.0 reports remain valid; the field defaults
  to null.
- Report schema **1.1.0**: additive change (FR-013). Adds the
  `VerificationReasonCode` enum (`file_not_in_scope`, `line_out_of_range`,
  `snippet_found_elsewhere`, `snippet_not_found`, `evidence_not_confirmed`) and
  four optional `VerificationFailure` fields — `reason_code`, `claimed_snippet`,
  `actual_snippet`, `found_at_lines`. Migration: 1.0.0 reports remain valid and
  the new fields default to null, so no action is required for existing
  consumers.
- Report schema **1.0.0**: initial schema. `Report` carries
  `schema_version = "1.0.0"`, `run`, `code_findings`, `requirement_findings`,
  `summary`, and `markdown_content`.

### Changed
- ZDR now works on OpenRouter (sent in the request body; previously it crashed every LLM call), fails closed on non-OpenRouter backends, and its warning prints once per run with backend-specific wording.
- LLM requests are bounded by `[llm] timeout_seconds` (default 120) and
  `[llm] max_retries` (default 2); previously the client defaults allowed a
  stalled request to wait up to 30 minutes.

### Fixed
- Cited snippets copied with the prompt's line-number prefixes (e.g. `   21| code`) are cleaned before verification, so real findings are no longer rejected for the prefix alone.

### Security
- Secret redaction now covers every shape it claims to: all GitHub token forms
  (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), provider `sk-` keys
  (OpenAI, OpenRouter, Anthropic), Google (`AIza...`), Slack (`xox...`) and GitLab
  (`glpat-`) tokens, AWS secret access keys, and a secret assigned to a key-,
  token-, password-, secret- or credential-named variable in quoted or JSON-style
  form - while environment-variable names, code that reads a secret and prose
  such as `scikit-learn` pass through unchanged.
- Partial-review and LLM-call errors are a one-line summary of at most 200
  characters (provider, HTTP status, the provider's own message, a reason code)
  instead of the raw response body, so account identifiers in a provider error
  never reach the report, the log or stdout.
