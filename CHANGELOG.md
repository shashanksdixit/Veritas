# Changelog

All notable Veritas changes are recorded here. Every entry that touches the
review output schema (or the `Report` / `CodeFinding` / `RequirementFinding`
models) records the affected `Report.schema_version` and a one-line description
of what changed at that version — the migration-path record required by
constitution Principle VIII / FR-016.

## [Unreleased]

### Added
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
