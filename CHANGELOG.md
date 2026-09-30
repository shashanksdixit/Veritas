# Changelog

All notable Veritas changes are recorded here. Every entry that touches the
review output schema (or the `Report` / `CodeFinding` / `RequirementFinding`
models) records the affected `Report.schema_version` and a one-line description
of what changed at that version — the migration-path record required by
constitution Principle VIII / FR-016.

## [Unreleased]

### Added
- Prompt version **1.1.0**: review prompts present line-numbered code and
  instruct the model to cite shown line numbers; no report schema change.
- Initial Veritas implementation for the `001-code-review` feature: CLI review
  (`project` / `module` / `file` / `pr` scopes), five review types (code
  quality, security/OWASP, requirements, test coverage, performance), grounded
  citation verification, suppression allowlist, project-aware review, Markdown
  report plus compact stdout summary, and optional PR-comment posting.
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