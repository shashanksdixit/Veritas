# Changelog

All notable Veritas changes are recorded here. Every entry that touches the
review output schema (or the `Report` / `CodeFinding` / `RequirementFinding`
models) records the affected `Report.schema_version` and a one-line description
of what changed at that version — the migration-path record required by
constitution Principle VIII / FR-016.

## [Unreleased]

### Security
- `--config` can no longer bypass the committed-config secret check (FR-020,
  T108): when the file passed with `--config` is `.veritas/config.toml` itself,
  under any spelling (`./.veritas/config.toml`, an absolute path, `..`
  segments, a different letter case on Windows, or a link to it), a key in it
  is refused with the same `[error] invalid configuration: ...` line and exit 1
  as without `--config`. Paths are compared by file identity, not by text. A
  `--config` file anywhere else may still hold keys. Migration: move a key out
  of `.veritas/config.toml` into `.veritas/config.local.toml` or a `VERITAS_*`
  variable.
- A key or token in the committed `.veritas/config.toml` is now refused
  (FR-020, T107): if it sets `api_key`, `github_token` or `gitlab_token`,
  `veritas review` exits 1 with `[error] invalid configuration: ...` naming the
  field and pointing to `.veritas/config.local.toml` or the `VERITAS_*`
  variable. Keys in `.veritas/config.local.toml`, an explicit `--config` file
  and environment variables work as before. Migration: move any key from
  `.veritas/config.toml` to `.veritas/config.local.toml` (and rotate it if the
  file was ever committed).
- Every finished output is redacted where it is written or sent (FR-013,
  T105): the Markdown report file, the compact stdout summary, every string
  value in `.veritas/last-report.json`, and the `--post` comment body. The
  run's recorded target is redacted too, so a credentialed PR/MR URL such as
  `https://oauth2:<token>@gitlab.com/...` is shown as
  `[REDACTED]gitlab.com/...`. Suppression is unaffected: fingerprints are
  computed from snippets that were already redacted, and existing
  `.veritas/suppressions.json` entries keep matching. No report schema change.
- Redaction masks unquoted secrets (constitution 6.0.0/6.0.1, FR-013, T104):
  a literal value assigned to a credential-named key is now masked when
  unquoted (`password=hunter2`, `api_key: abcd1234efgh5678`), not only when
  quoted, and in a .env-style line at the start of a line
  (`DB_PASSWORD=correcthorse`) every unquoted value is masked except a
  `$VAR` / `${VAR}` reference. Elsewhere a plain word, a number, a dotted name
  or an environment-variable name is treated as code and left alone, so
  `password = correcthorse` outside a .env-style line is still not masked.
  Migration: some text that appeared in reports and logs before is now shown as
  `[REDACTED]`; no report schema change.
- Every log line, including the `--verbose` structured payload, and every CLI
  error message on stderr now passes through `redact_secrets()` in one place
  (`Log`). Uncaught-exception tracebacks no longer print local variables;
  their text is not redacted, and neither are Click's own usage errors.

### Added
- Report schema **1.9.0**: adds `ReviewRun.sast_status` (`"ran"` or
  `"not_run"`), `ReviewRun.sast_result_count` (results mapped to reviewed
  files, set only when the scan ran) and `ReviewRun.sast_reason` (one line, set
  when the scan did not run or ran degraded, e.g. `1 result(s) unmapped`)
  (FR-012, T109). `sast_rules` is unchanged: the configured rules source. The
  report header's `- **SAST rules**: ...` line is replaced by one line,
  `- **SAST**: ran with `<rules>` — <n> result(s)` (plus `; <reason>` when
  degraded) or `- **SAST**: not run — <reason> (rules configured: `<rules>`)`,
  and the compact stdout summary gains `SAST: ran (<n> result(s))` or
  `SAST: not run — <reason>`. Before this, a missing OpenGrep was only logged
  and the report did not say SAST had not run. Migration: reports written
  before 1.9.0 still validate, with the three fields null; readers or scripts
  that matched the `SAST rules` header line should match `- **SAST**:` instead.
- Report schema **1.8.0**: adds `Report.failed_batches`, one `FailedBatch`
  record (review type, batch, total, files, reason) per failed LLM batch call,
  and a collapsible "Failed batches" table in the Markdown report after
  Verification failures (FR-027). Migration: earlier reports remain valid (the
  field defaults to an empty list); readers that parsed per-batch details out
  of `run.error` should read `failed_batches` instead.
- Provider error summaries drop URLs
- README "Purging stored data" section and FR-030: every place Veritas stores
  review artifacts (the report file, `.veritas/last-report.json`,
  `.veritas/suppressions.json`), the OpenGrep binary's own log and leftover
  rules files, and Git Bash / PowerShell commands to delete each. No behaviour
  change; a new test pins that a review writes nothing else and removes its
  SAST temporary directory.
- [llm] max_output_tokens bounds every LLM response and truncation is logged

### Changed
- The verification-failure note in the Markdown report says the reviewer's
  citation could not be confirmed (the quoted code was not found at the cited
  lines), instead of the claim.
- Spec and plan aligned with constitution 6.0.0: verification confirms the
  citation, not the claim (FR-013); SAST reproducibility is stated against the
  rules content, and the rules source is recorded (FR-025).
- Project, module and file reviews record an input revision (FR-024):
  `sha256:` + the hex SHA-256 of the reviewed files after exclusion (sorted by
  path; path, NUL, content, NUL per file), shown in the report header in place
  of `n/a`. PR reviews still record the head commit SHA. `run.input_revision`
  keeps its type (`str | None`), so the report schema version is unchanged.
- Run failure output is concise: `run.error` and the compact
  `Report status: incomplete — ...` line (at most 500 characters in total) give
  the failed batch count, the count per review type and each distinct reason
  once (at most 3, then "and N more"), plus any non-batch errors, each once.
  Previously every per-batch error was concatenated (about 10,000 characters
  for 40 identical failures).
- Operational polish: [llm] max_concurrency, a single retry for OpenRouter's in-flight 402, atomic log lines, UTF-8 console output on Windows, friendly configuration errors, LF line endings, README setup notes.

### Fixed
- An `unclear` requirement's evidence is now verified like every other
  status (FR-013, T106). Previously a reference it cited was published
  unchecked; now a reference to a file outside the review or a line past its
  end is removed and recorded as an `evidence_not_confirmed` verification
  failure. The requirement stays `unclear`. No report schema change.
- A failed `--post` warning names only the HTTP status (e.g. `403 Forbidden`) and the report path; it no longer includes the hosting API's raw response body. The exit code still ignores the posting outcome (0 complete, 2 incomplete) (FR-028).
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
