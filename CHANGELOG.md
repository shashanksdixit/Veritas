# Changelog

All notable Veritas changes are recorded here. Every entry that touches the
review output schema (or the `Report` / `CodeFinding` / `RequirementFinding`
models) records the affected `Report.schema_version` and a one-line description
of what changed at that version — the migration-path record required by
constitution Principle VIII / FR-016.

## [Unreleased]

### Added
- Initial Veritas implementation for the `001-code-review` feature: CLI review
  (`project` / `module` / `file` / `pr` scopes), five review types (code
  quality, security/OWASP, requirements, test coverage, performance), grounded
  citation verification, suppression allowlist, project-aware review, Markdown
  report plus compact stdout summary, and optional PR-comment posting.
- Report schema **1.0.0**: initial schema. `Report` carries
  `schema_version = "1.0.0"`, `run`, `code_findings`, `requirement_findings`,
  `summary`, and `markdown_content`.