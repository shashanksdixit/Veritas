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
- `[security] opengrep_jobs` (`VERITAS_OPENGREP_JOBS`, default 4) sets how many
  parallel jobs OpenGrep runs with, passed as `-j <n>` (FR-012, T122). A value
  below 1 is refused with `[error] invalid configuration: opengrep_jobs: Input
  should be greater than or equal to 1` and exit 1. When the OpenGrep process
  exits, stderr logs one line such as `[info] OpenGrep scan finished in 31.2 s
  (-j 4)`. No schema change.
- PR reviews now use project context (FR-009, FR-010, FR-011, T120). For
  `--scope pr` the manifests are read from the repository root at the PR head
  commit, and AGENTS.md / CONVENTIONS.md / STYLE.md and `.veritas/rules.md`
  from the repository root at the PR base commit, so a PR cannot change the
  rules it is reviewed by. Everything goes through the read-only hosting
  fetcher (GET only, never local git state): at most 16 more requests per
  GitHub run, 114 per GitLab run. The report says, for example,
  `- **Project context**: detected — pyproject.toml (from repository root at abcd1234)`.
  A PR that changes a conventions or rules file is still reviewed with the
  base version, and the report says so (partial). A read failure other than
  404 (for example a 502) is not fatal here: it becomes a stated problem, the
  context is partial or unavailable, and the review finishes with its usual
  exit code. The reason `not read for PR reviews yet` and the note
  `Project context unavailable for a remote PR review` are gone; a PR whose
  root holds nothing now says `no manifest, conventions file or rules file found`.
  pyproject.toml fetched for a PR is decoded as the local reader decodes it (a
  leading byte-order mark ignored, invalid UTF-8 stated). `read_optional` on
  both fetchers gains a keyword `strict_utf8` (default unchanged), and each
  fetcher class names its error as `api_error`. No schema change:
  `project_context_origin` now holds `repository root at <8-character head SHA>`
  for a PR review instead of null.
- A local review finds project context above its target (FR-009, FR-010,
  FR-011, T119). From the start directory (the target, or a file target's
  directory) Veritas walks up to the project root: the first directory holding
  `.git` (a directory, or a file as in a git worktree or submodule) or a
  `.veritas/config.toml` file. A bare `.veritas/` directory is not a marker.
  Nothing above the root is read. The nearest directory holding any manifest
  supplies all manifests, never mixing levels, so a package's own
  pyproject.toml is not combined with the root's package.json; a manifest
  there that cannot be used still decides the directory and is stated. The
  conventions file and `.veritas/rules.md` are each the nearest whose text is
  not blank, so a module review now uses the repository root's AGENTS.md and
  rules. With no marker up to the filesystem root, or within 32 levels, only
  the start directory is read, as before. `--scope module src` in a git
  repository whose root holds pyproject.toml now reports that file instead of
  "no manifest, conventions file or rules file found". No schema change.
- Report schema **1.10.0**: the report states the project context the review
  used (FR-009, T117). `ReviewRun` gains `project_context_status`
  (`"detected"`, `"partial"` or `"unavailable"`), `project_context_sources`
  (the files that contributed; a manifest that parsed to nothing is not
  listed), `project_context_reason` (one line, for partial and unavailable)
  and `project_context_origin` (where the files were read from, relative to the
  working directory, or only the directory name when outside it; never an
  absolute path), all set by the context node. The report header gains, after
  the SAST line, `- **Project context**: detected — <sources> (from <origin>)`,
  `- **Project context**: partial — <sources> (from <origin>); <reason>` or
  `- **Project context**: unavailable — <reason>`, and the compact stdout
  summary gains `Context: detected (<sources>)`,
  `Context: partial (<sources>) — <reason>` or `Context: unavailable — <reason>`.
  A local review with nothing detected says
  `no manifest, conventions file or rules file found`; a `--scope pr` review
  says `not read for PR reviews yet`. What the model receives is unchanged.
  Migration: reports written before 1.10.0 still validate, with the status,
  reason and origin null and the sources empty; consumers should treat null as
  "not recorded", not as "unavailable".
- Tests: a reachability test for the read-only boundary (FR-026, T112).
  `tests/boundary_walker.py` walks everything a graph node's function can
  reach (closures, defaults, partials, bound methods, Veritas objects and their
  methods, containers, the globals a function's code names, including inside
  lambdas, nested functions and function-body imports, and modules only through
  the attributes that code uses) and reports every write or execute capability
  with the path that reached it. `tests/integration/test_read_only_boundary.py`
  proves the walker on one small setup per path kind (each gap rule is also
  shown to miss its case when switched off), then walks the real graph built for
  `--scope pr --post`: the analysis nodes reach nothing that can write or
  execute, the scope node's only execute routes are the two pinned OpenGrep
  paths, render is the only output node and its only edge is to the end. The
  module overrides the integration conftest's `collect_sast` stub so it walks
  the production scanner. No behaviour change.
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
- Project-context sources are named by their path relative to the project
  root, with `/` separators (FR-009, T119), and `project_context_origin` is
  that root (the start directory when no root was found). A file at the root
  keeps its bare name (`pyproject.toml`, `AGENTS.md`, `.veritas/rules.md`);
  one below it reads `src/pkg/pyproject.toml`, and so do the problems about
  it (`src/pkg/pyproject.toml is not valid TOML`). Blank conventions and rules
  files that were passed over are no longer kept in the scope node's
  `ProjectSources`; they never contributed, so nothing in the report changes.
  No schema change: the values change, not the fields.
- Project-context problems are stated, not silent (FR-009, T118). A file
  over 200,000 bytes (pyproject.toml now included; it had no limit), a file
  that cannot be read, a pyproject.toml that is not valid UTF-8 or not valid
  TOML, and conventions or rules text longer than the 4,000 characters the
  model receives each produce one short line naming the file, for example
  `package.json over 200000 bytes, skipped` or
  `AGENTS.md truncated to 4000 characters`. The context node logs each as
  one `[warn] Project context: ...` line, and the report's project context
  becomes partial when something was still detected or unavailable when not,
  with the lines joined by "; " as the reason. What the model receives for a
  truncated file is unchanged. A manifest that declares no version is still
  not a problem. No schema change (1.10.0 already has the fields).
- Conventions (`AGENTS.md`, `CONVENTIONS.md`, `STYLE.md`) and
  `.veritas/rules.md` text now passes through `redact_secrets()` before it
  reaches the model (FR-010, FR-011, FR-013, T118); before, a token written
  in AGENTS.md was sent to the model as is. Redaction runs before the
  4,000-character cut, so a cut can never leave an unmasked fragment.
- Internal: project-context files are now read by the scope node instead of
  the context node (T116; no user-visible change). The scope node reads the
  same manifests, conventions file and `.veritas/rules.md` from the same
  directory as before into a new `project_sources` state channel, and the
  context node renders them without touching the filesystem. For a PR review
  the channel is empty and the note is unchanged. The prompt text and the
  `[info] Detected: ...` lines are byte-for-byte the same, including today's
  silent skip of files over 200,000 bytes and the 4,000-character cut of
  conventions and rules.
- Internal: the GitHub and GitLab fetchers can now read project context
  (T115; nothing calls these yet, so reviews are unchanged): `pull_refs`
  returns a PR/MR's head and base SHAs, `list_root` lists the repository
  root (name, file or directory, size; GitLab reports no size, so it is
  None there), and `read_optional` reads a file, returning None only when
  the provider answers 404 and raising on any other failure. All are
  read-only GET requests through the existing guarded client.
- Each graph node gets its own frozen context instead of the shared, mutable
  `Runtime` object, which is removed (T111, FR-026): the analysis nodes get a
  `ReviewContext` (LLM client, log), the scope node a `ScopeContext` (exclusion
  patterns, batch budgets, SAST rules source, read-only hosting fetcher and
  parsed PR) and the render node a `RenderContext` (report path, batch
  budgets, suppression store, parsed PR, and the hosting poster only with
  `--post`). The hosting clients are now built in `run_review`
  (`veritas/hosting/factory.py`) after the LLM client and before the graph,
  not inside the scope node. Visible change: on a PR review with a missing
  `VERITAS_GITHUB_TOKEN` / `VERITAS_GITLAB_TOKEN`, the `[info] Scanning target
  in scope: ...` line no longer appears before the error. The ZDR warning, the
  `[error] PR review requires a ... token` line and exit code 1 are unchanged,
  and a missing API key still wins over a missing token. `LLMClient` no longer
  keeps the `Settings` object, only the output-token limit. The OpenGrep
  result mapping (`result_to_finding`) moved to
  `veritas/security/sast_results.py`. No report schema change.
- Internal, no user-visible change (T110): each hosting provider's client is
  split into a read-only fetcher (`GitHubFetcher`, `GitLabFetcher`) and a
  poster (`GitHubPoster`, `GitLabPoster`); `GitHubClient` and `GitLabClient`
  are removed. The fetcher's HTTP client refuses every method other than GET
  and HEAD before any network I/O (`github|gitlab API error: 0 — blocked:
  <METHOD> on read-only fetcher`). TLS verification, certificates and
  environment proxies are unchanged: the guard is an httpx request event
  hook (`_reject_writes`) on an otherwise unchanged `httpx.Client`, so httpx
  builds its transports and proxy mounts exactly as before. Token checks,
  error messages, endpoints and request bodies are unchanged; no report schema
  change.
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

### Removed
- Unused internal tools (T113; internal, no user-visible change): the module
  `veritas/review/tools/read_only.py` (`read_file`, `list_dir`, `grep_code`,
  `get_ast`, `scoped_files`), which no node ever used, and `update_suppressions`
  from `veritas/review/tools/write_tools.py`, which nothing called. The plan's
  read-only boundary description now matches the real design: frozen per-node
  contexts, checked by the reachability test.

### Fixed
- OpenGrep no longer times out on larger reviews (FR-012, T122). Without `-j`,
  OpenGrep 1.30.1 ran one job per detected core (14 on the measured machine)
  and the same 116-file scan took 39 to 208 s, past the 120 s limit; with 4
  jobs it took 24 to 41 s on every run. The limit stays 120 s. A timeout's
  reason now states it, `OpenGrep scan timed out after 120 s`, in the report's
  SAST line, the `SAST:` stdout line and the log, replacing `OpenGrep scan
  timed out`.
- A hosting API error while fetching a PR no longer ends `veritas review
  --scope pr` with a Python traceback (FR-002, T121). An error reading the PR
  refs, its file list or its files (for example a 404, a 502 or a network
  failure) now prints one line, `[error] github API error: 502 — Bad Gateway`
  (with ` (path: ...)` when a file was being read), redacted like every
  other error, and exits 1. No report file, `.veritas/last-report.json` or
  PR comment is produced. Errors reading project context still only degrade
  the context (T120).
- A pyproject.toml that is not valid UTF-8 no longer stops a local review
  with a `UnicodeDecodeError` (FR-009, T118): it is skipped and the report
  says `pyproject.toml is not valid UTF-8, skipped`.
- A pyproject.toml that is valid TOML but shaped unexpectedly no longer
  crashes or misleads (FR-009, T118): `project = "x"` raised `AttributeError`
  and stopped the review, `requires-python = 3.12` (a number) was shown as
  "Python 3.12", and `dependencies = "pydantic"` (a string) was walked one
  character at a time. Each now gives `pyproject.toml has an unexpected
  [project] layout`; the parts of the right type are still used. A
  non-string item inside a valid `dependencies` list is skipped, as before.
- A pyproject.toml saved with a UTF-8 byte-order mark (common from Windows
  editors) is read normally (FR-009, T118). `tomllib` rejects the mark, so
  such a file was taken for invalid TOML and its versions were lost; it is
  now decoded as `utf-8-sig`. Invalid UTF-8 is still reported as before.
- A blank (whitespace-only) `AGENTS.md` or `CONVENTIONS.md` no longer hides
  the next conventions file (FR-010, T118): before, a blank AGENTS.md was
  picked, stripped to nothing, and a real STYLE.md next to it was never read.
  The search now passes over blank files.
- A `Retry-After` header given as an HTTP date no longer crashes the
  OpenRouter in-flight-budget retry (FR-019, T114). Two causes: the date parser
  `parsedate_to_datetime` was never imported (`NameError`, which an
  unparseable value also hit), and the parsed date carried a timezone while
  "now" did not, so subtracting them failed and the wait silently became 0
  seconds. The wait is now the time until that date, capped at 120 seconds;
  a past date or an unparseable value still means retry immediately. A date
  written with `-0000` is read as UTC, the same as `GMT`.
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
