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
- An incomplete report's verdict now says it covers only what was reviewed
  (FR-015, FR-027, T140; no schema change). The verdict is derived only from
  the findings, so a run whose every LLM call failed used to print a bare
  `Verdict: Clean`. Stdout now reads `Verdict: Clean (incomplete review: covers
  only what was reviewed)`, the Markdown verdict line reads ``**Verdict**:
  `Clean` (incomplete review: covers only what was reviewed)``, and the Markdown
  report follows the verdict's reason with a note that code a failed call never
  reviewed may still have problems, so a `Clean` or `RequiresReview` verdict is
  not a clean bill of health and a `RequiresModification` verdict may not list
  every change needed. A `RequiresModification` found in the parts that were
  reviewed is kept, with the same qualification. The `--post` comment body is
  the Markdown report, so it carries both. `summary.verdict` in
  `.veritas/last-report.json` is unchanged (still the derived value); read it
  together with `run.report_status`. Complete reports are unchanged, including
  those whose Coverage lists files not reviewed.
- The verification-failure summary now states the two outcomes separately
  (FR-013, FR-015, FR-016, T139; no schema change). A code finding that fails
  verification is excluded from the report, but a requirement finding that
  fails stays in it with its unconfirmed evidence removed or its status set to
  unclear; stdout used to call both "excluded" and the Markdown note called
  both "dropped". Stdout now reads, naming only the kinds present,
  `Verification failures: 1 code finding(s) excluded; 1 requirement finding(s)
  kept, with unconfirmed evidence removed or marked unclear (FR-013), see full
  report`, and counts findings rather than failure records (one requirement
  finding can record both `evidence_not_confirmed` and `explanation_missing`,
  and was counted twice). The Markdown note describes both outcomes and is
  followed by an `**Outcome**:` line with the same counts. The kind is read
  from each failure's existing `reason_code`, so report JSON is unchanged.
- Documentation: the specs no longer contradict each other or the code (T138;
  no behaviour, schema or test change). plan.md's summary says only code
  findings carry a recommendation and a requirement finding carries an
  explanation of its status (constitution 8.0.0 Principle IV, FR-005); its
  Storage line names `.veritas/last-report.json` (FR-030); and its source tree
  lists every tracked test file, adding `test_llm_call_logging.py` and
  `test_unsupported_language_coverage.py`. data-model.md's ReviewState is the
  class in `veritas/review/state.py`, all 26 channels with their reducers.
  `docs/veritas-requirements.md`, the original requirements input, opens with
  a note that the constitution and spec.md supersede it where they differ,
  listing each difference with its governing source. tasks.md adds superseded
  notes to the Phase 3 goal, T036, T042, T094 and T105, and tags T043 with
  FR-023.
- Documentation: the user-facing docs now match the code (T137; no behaviour,
  schema or test change). README and quickstart.md say that only code findings
  carry a recommendation and that a requirement finding carries a non-empty
  explanation of its status instead (constitution 8.0.0 Principle IV, FR-005).
  contracts/cli.md and README drop the `--no-verbose` option and removal of a
  suppression by ID, neither of which exists. The stdout and stderr examples
  show the lines Veritas really writes: the real compact summary format, the
  ZDR warning first, `Scanning target in scope`, `Security coverage degraded:
  ...` after `OpenGrep not found on PATH` (also in quickstart.md Scenario 1),
  a failed LLM batch's `[error] LLM call ... failed: ...` and `[warn] <review
  type>: batch <i>/<n> failed ...` lines in place of an invented rate-limit
  line, and a failed `--post` as a `[warn]` line (FR-028). The exit code rows
  in README and contracts/cli.md are now identical: exit 1 names the ZDR
  refusal, a missing hosting token, a PR fetch failure and an LLM client that
  failed to initialize (for example, no API key) instead of "provider
  unreachable", and exit 2 says a failed LLM call, such as a rejected API key
  or an unreachable provider, gives a partial review. quickstart.md asks for
  OpenGrep v1.30.1 on PATH (there is no binary-path setting), and its
  Scenario 8 uses an API key that is invalid from the start, since settings
  are loaded once per run; its example reason is the provider's error message
  as Veritas summarizes it, not any provider's exact wording. CLAUDE.md's test
  count is about 1400.
- Documentation: research.md § 1 no longer claims what the Semgrep Rules
  License permits or recommends `--config auto`, which `[security]
  opengrep_rules` does not accept; it now says registry rulesets such as
  `p/owasp-top-ten` carry the Semgrep Rules License, that using one is the
  user's licensing choice, and that a local rules file or directory is the
  alternative (constitution 8.0.0 Principle II, T136). Its OpenGrep command
  points to FR-012 for the current command. plan.md's Constitution Check rows
  say "licensing choice". The rules-licence test now checks every paragraph,
  list item and table row of every tracked Markdown file that names a ruleset
  (CHANGELOG.md and completed task lines are historical records and exempt),
  plus the `run_opengrep` docstring. No behaviour or schema change.
- Every LLM call is now logged with its structured fields in every run, not
  only with `--verbose` (constitution Principle VIII, FR-019, T135): the
  `[info] LLM call <model> (<ms> ms)` line, and the `[error] ... failed:` line,
  end with a sorted JSON object holding `model`, `latency_ms`, `prompt_tokens`,
  `completion_tokens` and `total_tokens`; a count the provider does not report
  is `null` (previously left out). Token counts are now read from LangChain's
  `usage_metadata` first, so Anthropic-style counts (`input_tokens`/
  `output_tokens`), previously dropped even with `--verbose`, are logged; the
  Anthropic shape is verified with constructed responses only, as
  langchain-anthropic is an optional extra not installed in the test
  environment. Other log lines are unchanged. Logs are not part of the report,
  so `schema_version` stays 1.15.0.
- Constitution amended to v8.0.0 (Principle IV): a recommendation is required
  for code findings only; a requirement finding carries no recommendation but
  must carry a non-empty explanation of its status. CLAUDE.md now names
  v8.0.0, and spec.md FR-003 and FR-005 say code findings carry recommendation
  text and requirement findings carry an explanation (T134).
- Documentation: plan.md's Constitution Check re-run against constitution
  v8.0.0 and the code at 8cc0488 (schema 1.15.0, prompts 1.9.0), and T099
  closed. Each of the 13 gate rows describes the code as it is, with file and
  line references, the tests that prove it and, for the three gates that
  failed at the 2026-10-21 re-run (2ea72ad, v7.0.0), the probes that now pass:
  an unquoted code finding citing lines 0-0, -3 to -1 or 2-99 of a 2-line file
  is rejected as `line_out_of_range` (T132), a code finding with a missing or
  whitespace recommendation is rejected as `recommendation_missing` (T133),
  and a requirement finding with a blank explanation is marked unclear and
  recorded as `explanation_missing` (T134). Result: 11 PASS, 2 PASS with
  deferral (Fixed Review Scope, T131: files beyond `MAX_SCOPE_FILES` or
  unreadable are not reported, `.gitignore` is not respected in the local
  walk, `_SKIP_FILES` is not applied in the PR path; Privacy & Data Handling,
  T103) and 0 FAIL. Stale line references and the schema version (1.12.0 →
  1.15.0) in the rows are corrected; all 75 test references in the rows
  resolve to collected tests or test files. research.md § 6 (licensing)
  corrected: it listed certifi only as an httpx dependency and left out
  orjson. The licence metadata of all 54 installed runtime packages listed by
  `uv export --no-dev` was read: the 8 direct runtime dependencies are MIT or
  BSD-3-Clause, and exactly two transitive packages carry MPL-2.0 (certifi
  `MPL-2.0`, through httpx, httpcore, langchain-openai and requests; orjson
  `MPL-2.0 AND (Apache-2.0 OR MIT)`, through langgraph-sdk and langsmith);
  none is GPL, LGPL or AGPL. httpx2-jsfetch (emscripten only) and the optional
  `anthropic` extra are not installed and were not checked. MPL-2.0 is
  file-level copyleft and Veritas uses both packages unmodified. README's
  "All runtime Python dependencies are permissive-licensed" is replaced to
  match. No behaviour, schema or test change.
- Documentation: stale text brought in line with the current behaviour (T129).
  `quickstart.md` no longer assumes OpenRouter is the default: the API-key
  prerequisite names the configured backend (OpenAI direct by default),
  Scenario 5 configures OpenRouter explicitly for ZDR and shows that
  `VERITAS_ZDR=true` on the default backend is refused with exit 1, and
  Scenarios 1, 3, 4, 6, 9 and 11 now show the current stderr, stdout and report
  wording (the `SAST:` and `Context:` lines, source labels, the missing-token
  error, the `--post` warning and rejection, the suppress output, the
  verification-failure table, the no-requirements finding). research.md §2 is
  marked superseded by T125; tasks T006-T009 carry superseded notes; T103 is
  marked deferred past the v1 merge; plan.md's source tree gains
  `veritas/hosting/types.py` and the missing test files, and its tool-binding
  wording now says no node is bound to tools; README's schema marker shows a
  placeholder instead of `1.0.0`; data-model.md's `model_name` example is
  `gpt-4o-mini`; spec.md FR-026 lists the OpenGrep job count in `ScopeContext`
  and FR-013 states the order of the evidence checks. No behaviour or schema
  change.
- Documentation: every place that names a registry SAST ruleset now states its
  licence (FR-012, T128, constitution 7.0.0 Principle II). README.md,
  `contracts/cli.md`, spec.md FR-012, the `opengrep_rules` setting description
  and the OpenGrep module docstrings say that registry rulesets such as
  `p/owasp-top-ten` are not covered by OpenGrep's LGPL-2.1 licence, carry the
  Semgrep Rules License, and are the user's licensing choice, with a local
  rules file or directory as the alternative. No behaviour or schema change.
- The ZDR-off warning on a non-OpenRouter backend no longer advises a setting
  Veritas refuses (FR-021, T126). It read `... Set VERITAS_ZDR=true only if that
  agreement already provides zero data retention ...`, but `VERITAS_ZDR=true` on
  such a backend stops the run. It now reads `[warn] ZDR is OFF: zero data
  retention cannot be enforced per request on this backend; data retention is
  governed by your account agreement with <provider>. To enforce ZDR per
  request, use OpenRouter with VERITAS_ZDR=true.` This is the line every
  default (OpenAI direct) run prints. The anthropic route with an OpenRouter
  `base_url` now gets this wording too, instead of the OpenRouter warning. The
  OpenRouter warning, the fail-closed refusal and its message are unchanged. No
  schema change.
- The default LLM backend is now a fixed model and endpoint, OpenAI direct:
  `openai:gpt-4o-mini` at `https://api.openai.com/v1` (FR-019, T125,
  constitution 7.0.0). It was OpenRouter (`https://openrouter.ai/api/v1`,
  `openai:openai/gpt-4o-mini`). Veritas never chooses a model at runtime; the
  unused free-model discovery module `veritas/llm/models.py` is removed. With
  the default backend the ZDR-off warning is the account-agreement wording
  naming `openai`; the report's `- **Model**:` line reads `gpt-4o-mini`.
  OpenRouter is still supported when configured, and its 402 retry and
  per-request ZDR are unchanged. No schema change. Migration: if you relied on
  the old OpenRouter default, set `VERITAS_BASE_URL=https://openrouter.ai/api/v1`
  and `VERITAS_MODEL=openai:openai/gpt-4o-mini` (or `base_url` and `model` under
  `[llm]` in `.veritas/config.toml`); an OpenRouter key in `VERITAS_API_KEY`
  sent to the new default endpoint will be rejected by OpenAI.
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
- A requirement finding with no explanation is no longer reported as if its
  status were explained (FR-005, T134, constitution v8.0.0 Principle IV). A
  requirement finding carries no recommendation, but it must explain its
  status. Before, a free-text finding whose `explanation` was absent, empty or
  only whitespace was kept with an empty explanation, and a structured
  satisfied or partial requirement whose deciding answers explained nothing
  was given "The code shown could not be judged against this requirement.",
  which contradicts its status. The structured merge now leaves that
  explanation empty, and after verification any requirement finding whose
  explanation is absent, empty or only whitespace stays in the report,
  marked unclear with the explanation "The reviewer gave no explanation for
  this requirement's status.", and is recorded as a verification failure with
  `reason_code` `explanation_missing` and the reason "the requirement finding
  gives no explanation". It is checked after the evidence: a requirement
  already demoted for unconfirmed evidence keeps that fixed explanation and is
  not recorded twice. Statuses the structured path concludes itself (gap,
  unclear, not_addressed), a free-text gap restated as not_addressed in PR
  scope, and the no-documentation finding carry fixed explanations and are
  unaffected. The report's reason-code legend gains `explanation_missing` ("A
  requirement finding gave no explanation for its status, so it was marked
  unclear."). Report schema **1.15.0**: `VerificationReasonCode` gains
  `explanation_missing`. Migration: reports written at 1.14.0 still validate
  unchanged; a consumer that matches on `reason_code` must accept the new
  value. Runs where the model leaves `explanation` out may report more
  unclear requirements (verdict RequiresReview rather than Clean, or rather
  than RequiresModification for a free-text gap) and more verification
  failures; no prompt changed.
- A code finding without a recommendation is no longer reported as if it had
  one (FR-005, T133, constitution v7.0.0 Principle IV). A finding whose
  `recommendation` was absent, empty or only whitespace was kept and shown
  with an empty recommendation; the verify node now checks it after the
  citation (on the finding as confirmed or corrected), leaves it out of the
  findings and records it as a verification failure with `reason_code`
  `recommendation_missing` and the reason "the finding gives no
  recommendation", with the quoted code and the text at its cited lines. A
  finding whose citation fails keeps that cause. SAST findings are
  unaffected: they carry a fixed recommendation. The report's reason-code
  legend gains `recommendation_missing` ("The finding gave no recommendation,
  so it could not be reported as actionable."), and the failure note's last
  sentence now reads "...it means the reviewer's citation could not be
  confirmed (the quoted code was not found at the cited lines, or no code was
  quoted), or that the finding gave no recommendation." Requirement findings
  are unchanged. Report schema **1.14.0**: `VerificationReasonCode` gains
  `recommendation_missing`. Migration: reports written at 1.13.0 still
  validate unchanged; a consumer that matches on `reason_code` must accept
  the new value. Runs may report fewer findings and more verification
  failures where the model left `recommendation` out; no prompt changed.
- Every LLM code finding must now quote the code it cites, and every code
  finding's cited lines must lie within the file (FR-013, T132, constitution
  v7.0.0 Principle I). A finding with no `cited_snippet`, or one that was
  empty or only whitespace, skipped the snippet check and was kept on its
  line numbers alone; it now fails verification as `snippet_missing` ("the
  finding quotes no code, so its citation cannot be checked") and is listed
  under Verification failures with the text at its cited lines. A blank
  quote is stored as none when the finding is built. Cited lines are checked
  before the quote: a start line below 1, an end line before the start line,
  or a start or end line past the end of the file fails as
  `line_out_of_range`, each with its own reason. A quoted finding cited at
  negative lines used to pass, because the check read lines from the end of
  the file. A quoted finding whose lines fall outside the file is corrected
  like a near-miss citation when its quote occurs exactly once within two
  lines (shown as "Citation adjusted"), so a real quote cited just past the
  end of the file is kept. SAST findings remain exempt. The report's
  reason-code legend gains `snippet_missing`, `line_out_of_range` is
  described as "below 1 or past the end of the file, or the range ends
  before it starts", and the failure note ends "or no code was quoted."
  Report schema **1.13.0**: `VerificationReasonCode` gains `snippet_missing`.
  Migration: reports written at 1.12.0 still validate unchanged; a consumer
  that matches on `reason_code` must accept the new value. Runs may report
  fewer findings and more verification failures where the model left
  `cited_snippet` out; no prompt changed (they already require a quote).
- Every file the scope lists or walks now appears in exactly one Coverage
  bucket (FR-022, FR-029, T130). Files in a language Veritas does not review
  were collected and never shown, and the requirement sources read
  (`specs/.../spec.md`, `README.md`) were counted among them. A local
  supported file over 1 MB appeared nowhere in Coverage, and an unsupported
  file under an excluded directory was never matched against the exclusion
  patterns, because the language check came first. Each path is now placed, in
  this order: excluded by a pattern (matched whatever the language); excluded
  with a reason (content withheld by the hosting API, T124, or a local file over
  1 MB, recorded as `file over 1 MB, not read` with its path relative to the
  scope root and never read); a requirement source that was read; a
  supported-language file, batched as before; otherwise an unsupported-language
  file. A `--scope file` target in an unsupported language is listed as
  unsupported and is still passed to SAST, as before. The Markdown Coverage
  table gains `| Files skipped (unsupported language) | <n> |` and
  `| Requirement sources read | <n> |` rows and matching `<details>` blocks,
  `Unsupported-language files (<n>)` and `Requirement sources (<n>)`; the
  Coverage sentence adds "Files in a language Veritas does not review are
  listed, not reviewed (FR-022)."; the compact line becomes
  `Coverage: <r> reviewed (<s> split), <e> excluded, <u> unsupported, <n> not
  reviewed; <b>/<m> batches`. The local walk now always skips Veritas's own
  files, the `.veritas/` directory and reports named `veritas-report-*.md`,
  so a review after an earlier run does not list that run's report or
  `.veritas/last-report.json`; project context still reads
  `.veritas/config.toml` and `.veritas/rules.md` directly. A report written
  with a custom `--output` path inside the project is still listed. Two
  known gaps remain and FR-029 states them: a local file whose size check or
  read fails with an operating-system error, and files beyond the walk's
  500-file `MAX_SCOPE_FILES` limit, are skipped without appearing in
  Coverage. The internal state channel `skipped_languages`
  is renamed `unsupported_files` and is written only by the scope node.
  Report schema **1.12.0**: `Coverage` gains `unsupported_files` and
  `requirement_sources` (FR-008 order), both lists defaulting to empty.
  Migration: reports written before 1.12.0 still validate, with both lists
  empty; a consumer parsing the compact coverage line must allow the new
  `<u> unsupported` count between `excluded` and `not reviewed`.
- A requirement evidence reference is confirmed only when every cited line
  exists (FR-013, T127, constitution v7.0.0 Principle I; no schema change).
  `path:0` was confirmed and is now `(line out of range)`. `path:A-B` was
  always reported as `(file not in scope)`; it is now confirmed when
  1 <= A <= B <= the file's line count, `(line out of range)` outside that and
  `(invalid line range)` when A > B. Any other text after the path's first
  `:`, such as `path:2:5`, is `(unrecognised reference)` instead of
  `(file not in scope)`, which now only means the path was not reviewed. An
  empty or whitespace-only reference was confirmed and is now
  `(empty reference)`. Unconfirmed references are removed, recorded as
  `evidence_not_confirmed` failures and leave a satisfied or partial
  requirement with no evidence unclear, as before. The Markdown report's
  Requirement Findings section now states once, whenever any requirement lists
  evidence: "Evidence verification confirms that each cited file is in the
  reviewed set and the cited lines exist; it does not confirm that they
  implement the requirement." The requirements prompt is unchanged.
- A PR file whose content the hosting API withholds is no longer reviewed as
  an empty file (FR-029, T124). GitHub's contents API answers a 1-100 MB file
  with `"encoding": "none"` and no content, and the fetcher returned `""`: the
  model saw nothing, SAST scanned nothing and the coverage line counted the
  file as reviewed. Both fetchers now raise `ContentWithheldError` for such a
  file (GitHub: encoding `"none"`, or any other non-base64 encoding with empty
  content; GitLab, whose documentation does not say what it returns for a
  large file: empty content for a blob of non-zero size, or encoding `"none"`
  with empty content). The scope node records it as an excluded file with the
  reason, for example `content not provided by the hosting API (file over 1
  MB)`, logs `[warn] scope: <path> not reviewed: <reason>`, and leaves it out
  of every prompt and the SAST scan; the report's Coverage section lists it as
  `` `<path>`: <reason> `` and the coverage line counts it as excluded. This
  covers requirement sources the PR changes too. A genuinely empty file is
  still reviewed, and every other fetch error is still fatal (T121).
  Report schema **1.11.0**: `ExcludedFile` gains `reason` and its `pattern`
  becomes optional; exactly one of the two is set. Migration: reports written
  before 1.11.0 still validate (their excluded files have a pattern and no
  reason); a consumer reading `coverage.excluded_files[].pattern` must allow
  it to be `null` when `reason` is set.
- The report now says which reviewed files OpenGrep did not scan (FR-012,
  T123). OpenGrep's built-in ignore list (`tests/`, `build/`, `vendor/`,
  `dist/`, `node_modules/`, `*.min.js` and others, at any depth) and its 1 MB
  size limit skip files silently; on one 116-file PR it skipped the 57 under
  `tests/` while the report said `SAST: ran (0 result(s))`. OpenGrep now runs
  with `--verbose` so its JSON lists what it skipped, and the SAST reason gains
  one clause per reason, for example `ran (0 result(s)) — 57 file(s) not
  scanned (OpenGrep's default ignore list: tests/)` or `1 file(s) not scanned
  (over OpenGrep's 1 MB size limit)`, after any `result(s) unmapped` note, in
  the report's SAST line, the `SAST:` stdout line and the `[warn]` log line.
  The status stays `ran`. Which files OpenGrep scans is unchanged. A file no
  rule's language covers is neither scanned nor skipped and is not reported.
  No schema change.
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
