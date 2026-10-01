---

description: "Task list for Veritas code review tool implementation"

---

# Tasks: Code Review Tool (Veritas)

**Input**: Design documents from `specs/001-code-review/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: Test tasks are included because constitution Principle VII mandates integration tests (LLM provider contracts, prompt/output-schema changes, CLI behavior and output formats, configuration handling), which the spec inherits as binding.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- Single project (per plan.md structure): `veritas/` package and `tests/` at repository root
- Package entry point: `veritas.main:app` (replaces root `main.py`)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [X] T001 Update `pyproject.toml` runtime dependencies (langgraph, langchain-core, langchain-openai, httpx, typer, pydantic>=2) and dev dependencies (pytest); langchain-anthropic is an optional/extra dependency installed only when a user configures a native Claude key (FR-020, T007); openai SDK is a transitive dependency of langchain-openai and is not listed directly; resolve and record actual versions in the lockfile at install time — do not hardcode unverified versions (per plan.md dependency-version policy)
- [X] T002 Create `veritas/` package skeleton: `veritas/__init__.py`, `veritas/main.py` wiring the Typer app (entry point `veritas = veritas.main:app` in `pyproject.toml`); remove boilerplate root `main.py`
- [X] T003 Create structured logging utility in `veritas/utils/logging.py` (stderr for diagnostics/errors, structured LLM-call logs with latency, model, token usage per constitution Principle VIII)
- [X] T004 Update `.gitignore` to exclude report output files, coverage artifacts, and `.veritas/config.local.toml` (credentials MUST never be committed per constitution Privacy & Data Handling)
- [X] T004a Create `CHANGELOG.md` at the repository root: versioning-discipline file recording, per entry, the `Report.schema_version` value and a one-line description of what changed in the report schema at that version — the migration-path record required by constitution Principle VIII / FR-016

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T005 [P] Create all entity models in `veritas/models/entities.py` per `data-model.md`: enums `ReviewScope` (project/module/file/pr), `Severity` (error/warning/info), `Category` (code_quality/security/requirement/test_coverage/performance), `FindingSource` (sast/llm-verified), `RequirementStatus` (satisfied/partial/gap/unclear), `ReportStatus` (complete/incomplete), `Verdict` (RequiresModification/RequiresReview/Clean); models `LineRange`, `ReviewRun` (id=uuid4, scope, target, input_revision None, config_hash, timestamps, model_name, prompt_version, report_status default INCOMPLETE, error None), `CodeFinding` (id, file, line_range, severity, category, `source: FindingSource | None` — set for security-category findings per FR-012/SC-003, None for all other categories, owasp_id None, cwe_id None, title, description, recommendation, confidence float 0.0-1.0, cited_snippet None, is_suppressed default False, suppression_entry None), `RequirementFinding` (id, requirement_ref, requirement_text, status, evidence list default [], explanation), `SuppressionEntry` (fingerprint, file, category, title, added_at, reason None), `VerificationFailure` (finding_id, file, line_range, reason), `Summary` (totals, severity_counts, category_counts, requirement_status_counts, verification_failure_count, verification_failures default [], `verdict: Verdict` per FR-015), `Report` (schema_version default "1.0.0", run, code_findings, requirement_findings, summary, markdown_content None)
- [X] T006 [P] Create configuration layer in `veritas/config/settings.py` and `veritas/config/constants.py`: Pydantic Settings reading env vars (`VERITAS_API_KEY`, `VERITAS_BASE_URL` default `https://openrouter.ai/api/v1`, `VERITAS_MODEL`, `VERITAS_ZDR` default false, `VERITAS_GITHUB_TOKEN`, `VERITAS_GITLAB_TOKEN`, `VERITAS_GITLAB_URL` default `https://gitlab.com`), config file `.veritas/config.toml` with `.veritas/config.local.toml` (gitignored) override; config file must NOT contain API keys/tokens (constitution Privacy & Data Handling)
- [X] T007 [P] Implement LLM client in `veritas/llm/client.py` using LangChain's `init_chat_model(model_string, **kwargs)` universal factory (research.md §2) — never the openai SDK directly; model_string resolved from config as `"<provider>:<model>"` (e.g. `"openai:gpt-4o-mini"` for the default OpenRouter path via the openai-compatible base_url, or `"anthropic:claude-sonnet-4-6"` when a user configures a native Claude key per FR-020); when the resolved provider is anthropic, langchain-anthropic MUST already be present — it is a predeclared `pyproject.toml` optional extra (`pip install veritas[anthropic]`), never installed at runtime from within the running process; if the resolved provider is anthropic and the package is missing, fail fast with a clear error naming the exact install command (FR-020's promise of native Claude support with no custom adapter code, but installation itself is the user's explicit step, not an automatic one); wrap calls with structured logging (latency, model, token usage)
- [X] T008 [P] Implement ZDR toggle in `veritas/llm/zdr.py`: when `VERITAS_ZDR` is on, inject BOTH `provider.zdr: true` AND `provider.data_collection: "deny"` into the request body (research.md §2 — two distinct documented fields; both set together per OpenRouter guidance); when `VERITAS_ZDR` is off (default), print a stderr warning once per run (FR-021, constitution Privacy & Data Handling): some free-tier models reserve the right to train on inputs/outputs, recommend `VERITAS_ZDR=true` for proprietary/sensitive code
- [X] T009 [P] Implement free-model discovery in `veritas/llm/models.py`: authenticated `GET /api/v1/models` (OpenRouter), select `:free`-suffixed models with `pricing.prompt == "0"` (research.md §2)
- [X] T010 [P] Create read-only review tools in `veritas/review/tools/read_only.py`: `read_file`, `list_dir`, `grep_code`, `get_ast` (constitution Read-Only Safety Boundary — these are the ONLY tools bound to review nodes)
- [X] T011 [P] Create write tools in `veritas/review/tools/write_tools.py`: `write_report`, `update_suppressions` (isolated for post-review nodes; MUST NOT be bound to any review node)
- [X] T012 [P] Create LangGraph state schema in `veritas/review/state.py`: `ReviewState` TypedDict with `messages` (Annotated add_messages), `scope`, `target`, `code_findings`, `requirement_findings`, `verification_failures`, `run`, `phase` per `data-model.md`
- [X] T013 [P] Implement language detection in `veritas/utils/languages.py`: supported languages Python, Java, JavaScript, C#/.NET, Go, Rust (FR-022); files outside the set are marked skip-with-note
- [X] T014 [P] Implement fingerprint function in `veritas/suppression/fingerprint.py`: `compute_fingerprint(file, category, flagged_snippet)` = SHA-256 of `"file|category|normalized"` where normalized collapses whitespace per line and drops blank lines (FR-017; must be stable across unrelated edits, must change when flagged code changes)
- [X] T015 [P] Implement suppression store in `veritas/suppression/store.py`: read/write `.veritas/suppressions.json` (git-tracked allowlist; add, remove, list entries per FR-017/FR-018)
- [X] T016 [P] Implement suppression resolver in `veritas/suppression/resolver.py`: match a suppression command to finding(s) by stable finding ID, or alternatively filename + line reference (edge case: unambiguous single match suppresses; no match → clear diagnostic; several matches → do not suppress; emit a stderr diagnostic listing the ambiguous candidate matches and exit code 1 (no interactive prompt), matching contracts/cli.md and the spec edge case)
- [X] T017 Build LangGraph graph shell in `veritas/review/graph.py`: structural shape `scope_resolve → review loop (read_tool_node only) → verify → write (terminal, END)` with NO edge from write back into review (constitution Read-Only Safety Boundary); compile `StateGraph(ReviewState)` with recursion_limit; leave node bodies for their story phases
- [X] T018 [P] Implement Markdown report renderer in `veritas/output/markdown.py`: sections for summary (`total_code_findings`, severity/category counts, requirement-status counts, verdict per FR-015), code findings with per-finding `source` label (SAST vs LLM-identified per FR-012), requirement findings with status + evidence, verification-failure notes; output MUST be valid Markdown postable directly as a PR comment (FR-016); MUST emit `Report.schema_version` as an HTML comment at the top of the file (e.g. `<!-- veritas-report-schema: 1.0.0 -->`) so the version is inspectable without parsing the report body. The Summary section MUST show the applicable verdict with a 'Triggered by' line (counts of error-severity findings, requirement gaps, warning-severity findings, and partial/unclear requirements) and a verdict legend; the Verification failures section MUST begin with the explanatory note defined in FR-015.
- [X] T019 [P] Implement compact stdout renderer in `veritas/output/compact.py`: counts + headline findings only — every error-severity CodeFinding, capped at 10, with a "+N more, see full report" note when the cap is hit (FR-016); full report always in file

### Tests for Foundational (T022a is NOT optional — it is the sole owner of mandatory SC-004; the remainder of this section is an optional contribution to the checklist) ✓

- [X] T020 [P] Unit tests for renderers in `tests/unit/test_markdown_renderer.py` and `tests/unit/test_compact_summary.py` (source labels render; verification-failure notes render; compact reflects report counts)
- [X] T021 [P] Unit tests for fingerprint determinism in `tests/unit/test_fingerprint.py` (same input → same hash; unrelated-whitespace edits → same hash; changed snippet → different hash)
- [X] T022 [P] Integration test for config handling in `tests/integration/test_config.py` (env/file/CLI precedence; `VERITAS_ZDR` toggling; secrets never surfaced in logs/errors per constitution Principle VII)
- [X] T022a [P] Integration test for run-level determinism in `tests/integration/test_determinism.py` (SC-004, constitution Principle VIII): run the full review pipeline twice against a fixed test fixture (same target, same config) with the LLM client mocked to return a fixed canned response, so no real LLM call is made; assert the deterministic components — SAST findings, manifest-parsed language/framework versions, requirement evidence citations, and suppression fingerprints — are byte-identical across both runs; explicitly do NOT assert equality on LLM-narrated text (`title`, `description`, `recommendation`, `explanation`) per FR-025, since that variability is not a defect
- [X] T022b [P] Integration test for output-schema versioning in `tests/integration/test_schema_version.py` (constitution Principle VII, FR-016): a full pipeline run's `Report.schema_version` matches the version declared in `data-model.md`; `CHANGELOG.md` (T004a) contains a corresponding entry for that version — this test is a tripwire that fails loudly if a schema field changes without a matching version bump and changelog entry

**Checkpoint**: Foundation ready — user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - Review a pull request before merge (Priority: P1) 🎯 MVP

**Goal**: `veritas review --scope pr --target "owner/repo#N"` fetches the PR remotely via the hosting provider's API and produces a full, grounded Markdown report covering all five review types (with per-finding suggested-change recommendations per FR-005).

**Independent Test**: Run against a live remote PR on a hosted repo containing a known bug, security issue, and requirement gap; report contains all mandatory review types, no local git state used, and the Markdown can be posted as a PR comment (spec US1).

**⚠️ NOTE: Write the tests below FIRST, ensure they FAIL before implementation**

### Tests for User Story 1 ✓

- [X] T023 [P] [US1] Contract tests for GitHub REST in `tests/contract/test_github_api.py` per `contracts/hosting-api.md` (list PR files + `Link` pagination, get file contents at ref, post issue comment; mocked httpx)
- [X] T024 [P] [US1] Contract tests for GitLab REST in `tests/contract/test_gitlab_api.py` per `contracts/hosting-api.md` (MR changes, repository files at ref, post MR note; `PRIVATE-TOKEN` auth)
- [X] T025 [P] [US1] Integration test for LLM provider contract in `tests/integration/test_llm_provider.py` (request construction incl. `provider.zdr` + `provider.data_collection: "deny"` when enabled, response handling, rate-limit/error handling — constitution Principle VII)
- [X] T026 [P] [US1] Integration test for OpenGrep invocation/parsing in `tests/integration/test_opengrep.py` (subprocess `opengrep scan --json`, result parsing into `CodeFinding` with `source="sast"`, exit-code handling, binary-not-found degraded path)
- [X] T027 [P] [US1] Integration test for verification node in `tests/integration/test_verification.py` (FR-013: confirmed citation passes; fake file/line excluded + `VerificationFailure` recorded)
- [X] T028 [US1] Integration test for CLI review of a PR flow in `tests/integration/test_cli_review.py` (full success path + errors: PR not found, access denied, API unreachable → exit code 1, no partial report — constitution Principle VII)
- [X] T028a [US1] Integration test for `--post` in `tests/integration/test_cli_review.py` (FR-028): `--post` with `--scope pr` calls the mocked hosting client's post-comment/post-note method with the rendered Markdown and exits 0; `--post` with any non-pr scope is rejected with exit code 1 and a clear stderr diagnostic before any review work starts; a mocked posting failure after a successful report write prints a stderr warning naming the reason and report path, and exits 0 (report file still written and valid)

### Implementation for User Story 1

- [X] T029 [P] [US1] Implement GitHub client in `veritas/hosting/github.py` via httpx (BSD-3-Clause): list PR files, get file contents at ref, post issue comment; bearer-token auth; `Link`-header pagination; 429 exponential backoff (1s/2s/4s, max 3) per `contracts/hosting-api.md`
- [X] T030 [P] [US1] Implement GitLab client in `veritas/hosting/gitlab.py` via httpx: `GET /projects/{id}/merge_requests/{iid}/changes`, repository files at ref, post MR note; `PRIVATE-TOKEN` auth; 429 backoff per `contracts/hosting-api.md`
- [X] T031 [P] [US1] Implement PR ref resolver in `veritas/hosting/resolver.py`: accept `owner/repo#N` (GitHub), `group/project!N` (GitLab, matching GitLab's own MR-reference syntax), a full GitHub PR URL, or a full GitLab MR URL per `contracts/hosting-api.md`; a bare number with no owner/repo or group/project is REJECTED with a clear stderr diagnostic and exit code 1 (U1: resolving a bare number would require either a config-level default project or reading local git state, both rejected — a bare number is always ambiguous, so the full reference is always required); return (provider, owner, repo, number)
- [X] T032 [P] [US1] Implement OpenGrep integration in `veritas/security/opengrep.py`: `subprocess.run([opengrep, "scan", "--config", rules, "--json", "-o", out, target])` (research.md §1 — CLI-only, no Python API); parse `results[]` (`check_id`, `path`, `start/end`, `extra.lines`, `extra.severity`, `extra.metadata.cwe/owasp`) into `CodeFinding` with `source=FindingSource.SAST`; `FileNotFoundError` (binary missing) → degraded-coverage reason string `"OpenGrep not found on PATH"` per FR-003/FR-012, not a full failure
- [X] T033 [P] [US1] Implement PR scope-resolution node in `veritas/review/nodes/scope.py`: for `scope=pr`, fetch changed files + contents via hosting client (remote only, no local git state per FR-002); populate `ReviewRun` metadata incl. input_revision from PR head SHA
- [X] T034 [US1] Create the five review-type prompt files in `veritas/review/prompts/` (`code_quality.md`, `security.md`, `requirements.md`, `test_coverage.md`, `performance.md`) with a version header constant consumed for `ReviewRun.prompt_version` (FR-024 attribution)
- [X] T035 [P] [US1] Implement `redact_secrets(text: str) -> str` in `veritas/utils/redaction.py` using pattern matching for common secret shapes (API keys, bearer tokens, connection strings, PEM/private-key blocks) per constitution Privacy & Data Handling
- [X] T035a [P] Unit test for `redact_secrets()` in `tests/unit/test_redaction.py` (constitution Privacy & Data Handling): common secret shapes (API keys, bearer tokens, connection strings, PEM/private-key blocks) are masked; non-secret code text passes through unchanged
- [X] T036 [P] [US1] Implement code-quality review node in `veritas/review/nodes/code_quality.py` bound only to read tools (maintainability, code smells, dead code); leave CodeFinding.source as None (not applicable outside category=security) MUST populate CodeFinding.recommendation with concrete suggested-change text per FR-005 (not a generated diff/patch). MUST call redact_secrets() on all user-facing finding text before adding the finding to state.
- [X] T037 [P] [US1] Implement security review node in `veritas/review/nodes/security.py`: narrate/prioritize SAST output; LLM-identified security findings get `source=FindingSource.LLM_IDENTIFIED` at creation time and MUST pass verification (FR-012; per-finding labels, no exemption) MUST populate CodeFinding.recommendation with concrete suggested-change text per FR-005 (not a generated diff/patch). MUST call redact_secrets() on all user-facing finding text before adding the finding to state.
- [X] T038 [P] [US1] Implement requirements review node in `veritas/review/nodes/requirements.py`: read spec-kit structured docs when present else freeform PRD/markdown fallback (FR-008); produce `RequirementFinding` with status satisfied/partial/gap/unclear + evidence citations (FR-006/FR-007) MUST populate RequirementFinding.explanation with a clear rationale for the assigned status (satisfied/partial/gap/unclear). MUST call redact_secrets() on all user-facing finding text before adding the finding to state.
- [X] T039 [P] [US1] Implement test-coverage judgment node in `veritas/review/nodes/test_coverage.py`: judge whether tests exercise the business logic; MUST NOT execute the test suite (FR-004); leave CodeFinding.source as None (not applicable outside category=security) MUST populate CodeFinding.recommendation with concrete suggested-change text per FR-005 (not a generated diff/patch). MUST call redact_secrets() on all user-facing finding text before adding the finding to state.
- [X] T040 [P] [US1] Implement performance reasoning node in `veritas/review/nodes/performance.py` (LLM reasoning only — no profiler integration per YAGNI list); leave CodeFinding.source as None (not applicable outside category=security) MUST populate CodeFinding.recommendation with concrete suggested-change text per FR-005 (not a generated diff/patch). MUST call redact_secrets() on all user-facing finding text before adding the finding to state.
- [X] T041 [US1] Implement verification node in `veritas/review/nodes/verification.py`: re-read every non-SAST finding's cited file/line — this MUST include both CodeFinding citations AND RequirementFinding evidence citations (incl. LLM-identified security findings, not exempt) per FR-013; confirmed → keep; unconfirmed → exclude + append VerificationFailure (never dropped silently per Clarifications).
- [X] T041a [US1] Implement summary aggregation in `veritas/output/summary.py`: `compute_summary(code_findings, requirement_findings, verification_failures) -> Summary` — counts by severity/category/requirement-status; derives `verdict` per FR-015 (RequiresModification: any error-severity CodeFinding or any RequirementFinding.status==gap; RequiresReview: none of those, but any warning-severity CodeFinding or any RequirementFinding.status in {partial, unclear}; Clean: otherwise)
- [X] T041b [P] [US1] Unit test for verdict derivation in `tests/unit/test_summary.py`: each of the three verdict tiers is reachable and correctly derived from representative finding combinations, including the boundary cases (a single warning with otherwise-clean findings → RequiresReview; a single gap with zero code findings → RequiresModification)
- [X] T042 [US1] Implement render node in `veritas/review/nodes/render.py`: bind `write_report` (write tool); call `compute_summary()` (T041a) to build the `Report.summary`; render Markdown (T018) + compact stdout (T019) from the same `Report` model (single source of truth — cannot drift); mark report complete/incomplete per FR-027; write report file to `--output` path; when `--post` is set (scope=pr only), after the file write, call the hosting client's post-comment/post-note method (T029/T030) with `markdown_content` to post the report to the reviewed PR/MR; on failure, catch the error, print a stderr warning naming the reason and the report file's path, and continue — do NOT raise, and do NOT change the process exit code, since the report file write already succeeded (FR-028)
- [X] T043 [US1] Implement CLI review command in `veritas/cli/app.py`: Typer `review` with `--scope`, `--target`, `--config`, `--output`, `--verbose`, `--post` (opt-in, PR-only — reject with exit code 1 and clear diagnostic if used with any non-pr scope, per FR-028); env/config precedence per `contracts/cli.md`; exit codes 0 (complete), 1 (fatal), 2 (partial/incomplete per FR-027)
- [X] T044 [US1] Wire the full graph in `veritas/review/graph.py`: `scope → review loop (read_tool_node only) → verify → render (write) → END`; confirm NO write tool is reachable from any review node and NO edge leads back into the review path (constitution Read-Only Safety Boundary); attach provenance collection (model, prompt_version, input_revision)

**Checkpoint**: User Story 1 fully functional and independently testable — this is the MVP

---

## Phase 4: User Story 2 - Ad hoc review of a project, module, or file (Priority: P2)

**Goal**: `veritas review --scope project|module|file` reviews a local target with the same full pipeline, scoped exactly to the target, with no influence from prior runs.

**Independent Test**: Run against (a) a whole project, (b) a single module dir, (c) a single file; each report is scoped exactly to its target; a second run is unaffected by the first (spec US2).

### Tests for User Story 2 ✓

- [X] T045 [P] [US2] Unit tests for scope resolver/paths in `tests/unit/test_scope_resolver.py` (PR ref parsing from T031; local path validation for project/module/file)
- [X] T046 [US2] Extend integration test in `tests/integration/test_cli_review.py` for ad-hoc scopes: project/module/file flows; non-existent or non-readable target → clear diagnostic + no report (spec US2 edge)

### Implementation for User Story 2

- [X] T047 [US2] Extend `veritas/review/nodes/scope.py` for local scopes: `project` (directory tree), `module` (sub-directory), `file` (single file); validate existence/readability; per-run independence — MUST NOT track state between runs (FR-001, edge case "no influence from prior run"); MUST leave ReviewRun.input_revision as None for all ad-hoc scopes (project/module/file) — revision tracking is PR-only (data-model.md)
- [X] T048 [US2] Extend CLI validation in `veritas/cli/app.py`: reject invalid scope/target combinations with clear stderr diagnostics (contracts/cli.md exit code 1)

**Checkpoint**: User Stories 1 AND 2 both work independently

---

## Phase 5: User Story 3 - Suppress and un-suppress individual findings (Priority: P2)

**Goal**: `veritas suppress` / `veritas unsuppress` manage a git-tracked `.veritas/suppressions.json` keyed by code fingerprint, and suppressed findings are excluded from reports.

**Independent Test**: Suppress a finding; reformat an unrelated region of the file → stays suppressed; edit the flagged snippet → reappears; un-suppress → reported again (spec US3).

### Tests for User Story 3 ✓

- [X] T049 [P] [US3] Integration test for CLI suppress/unsuppress in `tests/integration/test_cli_suppress.py`: add entry (by finding ID and by file+line), re-run excludes finding, unrelated edit keeps suppressed, snippet change lapses suppression, unsuppress restores; ambiguous/no-match diagnostics (spec US3 + edge cases)
- [X] T050 [US3] Integration test for render-time suppression matching: suppressed finding absent from BOTH Markdown and compact stdout views

### Implementation for User Story 3

- [X] T051 [P] [US3] Implement `veritas suppress` command in `veritas/cli/app.py` (`--finding-id` or `--file`/`--line`, optional `--reason`) per `contracts/cli.md`; resolve via `store.py`/`resolver.py`; write `.veritas/suppressions.json`; print the computed fingerprint to stdout on success (e.g. `Suppressed. Fingerprint: <sha256>  (use with 'veritas unsuppress --fingerprint' to reverse)`) — this is the only place the fingerprint is surfaced at suppress-time, and it also remains readable directly in .veritas/suppressions.json afterward (U1)
- [X] T052 [P] [US3] Implement `veritas unsuppress` command in `veritas/cli/app.py` (`--fingerprint` only — a suppressed finding has no live finding ID to reference, since SuppressionEntry stores only the code fingerprint, not the finding ID or line number) per `contracts/cli.md`; remove entry
- [X] T053 [US3] Wire render-time suppression in `veritas/review/nodes/render.py` + `veritas/suppression/store.py`: before rendering, match each `CodeFinding` against suppressions via `compute_fingerprint`; set `is_suppressed`, exclude from report views (FR-017 review-run check happens before render)

**Checkpoint**: User Stories 1, 2, AND 3 all work independently

---

## Phase 6: User Story 4 - Review adapted to the project's own context (Priority: P3)

**Goal**: Reviews detect language/framework versions from real manifests, honor project conventions (AGENTS.md-equivalent), and apply natural-language-only custom rules — with generic fallback when none exist.

**Independent Test**: Point Veritas at a project with a detectable framework version, a conventions file, and one custom NL rule; report reflects the version, honors conventions, and surfaces the custom rule's concern (spec US4).

### Tests for User Story 4 ✓

- [X] T054 [P] [US4] Unit tests for project-context parsing in `tests/unit/test_project_context.py` (manifests: `pyproject.toml`, `pom.xml`, `build.gradle`, `package.json`, `go.mod`, `Cargo.toml`, `.csproj`; conventions file; NL rules; none-found → generic note)
- [X] T055 [US4] Integration test in `tests/integration/test_project_aware_review.py`: detected version, conventions, and NL rule influence appear in the report; absent context → report notes context unavailable (spec US4)

### Implementation for User Story 4

- [X] T056 [US4] Implement manifest/project-context parser in `veritas/utils/project_context.py`: parse manifest files per FR-009 (Java 8 vs 21 style awareness; framework versions); detect conventions file (AGENTS.md-equivalent) per FR-010; load natural-language-only custom rules per FR-011 (no deterministic rule-file tier per YAGNI list)
- [X] T057 [US4] Implement context node in `veritas/review/nodes/context.py`: gather manifest versions + conventions + NL rules before review nodes run; when nothing detectable, produce the "project context unavailable" note (spec US4 edge)
- [X] T058 [US4] Inject collected context into all review-type prompt assemblies (`veritas/review/prompts/`) so guidance adapts to the project (FR-009/FR-010/FR-011)

**Checkpoint**: All four user stories are independently functional

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [X] T059 Run the full `quickstart.md` validation suite (12 scenarios) against the built tool and fix any gaps found
- [X] T060 [P] Verify dependency license discipline against `research.md` §6 (all Python deps MIT/BSD-3-Clause/Apache-2.0 — no LGPL/GPL in the runtime tree; OpenGrep engine remains sole LGPL-2.1 component with no paid tier)
- [X] T061 [P] Write user documentation in `README.md` (CLI reference per `contracts/cli.md`, ZDR privacy notes, suppression workflow)
- [X] T061a [P] Verify this spec (spec.md) has not drifted from the locked scope and out-of-scope list in `docs/veritas-requirements.md`; note and resolve any discrepancy before release (constitution Orientation to the Requirements Reference)
- [X] T062 [P] Unit tests for language skip/note logic in `tests/unit/test_languages.py` (unsupported-language files skipped and explicitly noted per FR-022)
- [X] T063 [P] Finalize test configuration in `pyproject.toml` (pytest `testpaths`, correct `pythonpath` for the `veritas` package; ensure `tests/` run from repo root)
- [X] T064 Re-verify constitution compliance: re-run all 13 plan.md gates, confirm no write tool reachable from review nodes in the final graph, and confirm integration coverage exists for prompt and output-schema changes (constitution Principle VII + VIII)
- [X] T065 Add `VerificationReasonCode` enum and the four optional `VerificationFailure` fields (`reason_code`, `claimed_snippet`, `actual_snippet`, `found_at_lines`) in `veritas/models/entities.py`, bump `Report.schema_version` to `1.1.0`, add a `CHANGELOG.md` entry recording the additive change and its migration note, and keep the existing schema tripwire test passing (FR-013)
- [X] T066 [P] In `veritas/review/nodes/verification.py`, return a structured verification result carrying `reason_code`, a specific human-readable `reason` naming the actual cause, redacted-then-200-char-truncated `claimed_snippet`/`actual_snippet`, and `found_at_lines` (1-based, at most 5, ascending); unit tests covering all five reason codes, redaction applied before truncation, the 200-char truncation bound, and the 5-line cap (FR-013)
- [X] T067 [P] Render `reason_code`, `claimed_snippet`, `actual_snippet` and `found_at_lines` in the Verification failures section of `veritas/output/markdown.py`; extend the renderer tests in `tests/unit/test_markdown_renderer.py` with a case where all four new fields are None, to prove old-shape failures still render (FR-013)
- [X] T068 [P] Line-numbered, line-boundary-truncated code_package in veritas/review/nodes/common.py; prompt instruction to cite shown line numbers; PROMPT_VERSION 1.1.0; tests in tests/unit/ (FR-014)
- [X] T069 Add the optional `citation_adjusted_from: LineRange | None = None` field to `CodeFinding` in `veritas/models/entities.py`, bump `SCHEMA_VERSION` to `1.2.0`, add a `CHANGELOG.md` entry recording the additive change and its migration note, and keep the existing schema tripwire test passing (FR-013)
- [ ] T070 Implement citation correction in `veritas/review/nodes/verification.py` per FR-013: when the snippet does not verify within the cited range but occurs exactly once in the file and that occurrence (s through e) overlaps the cited range or lies within 2 lines of it (s <= ce + 2 and e >= cs - 2), keep the finding with `line_range` corrected to s through e and set `citation_adjusted_from` to the original range, emitting an info-level log line for each correction; more than one occurrence, or an occurrence farther than 2 lines away, still fails verification. Unit tests covering: overlap correction, within-2-lines correction, 3-lines-away still fails, two occurrences still fails, a too-short cited range extended to the full snippet span, and a corrected finding keeping its suppression fingerprint (FR-013)
- [ ] T071 [P] Render `Citation adjusted from {cs}-{ce} to {s}-{e}` for corrected findings in `veritas/output/markdown.py`, with renderer tests covering a corrected finding showing that line and an uncorrected finding showing no such line (FR-013)
- [ ] T072 [P] Add the line_range-must-match-cited_snippet instruction to every prompt that receives code; bump `PROMPT_VERSION` and all prompt-file headers to `1.2.0`; tests (FR-014)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion — BLOCKS all user stories
- **User Stories (Phase 3+)**: All depend on Foundational completion
  - User stories can then proceed in parallel (if staffed)
  - Or sequentially in priority order (P1 → P2 → P3)
- **Polish (Final Phase)**: Depends on all desired user stories being complete

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational (Phase 2); no dependencies on other stories. **This is the MVP.**
- **User Story 2 (P2)**: Reuses the US1 pipeline with extended scope resolution; no other story dependencies
- **User Story 3 (P2)**: Depends on US1 render path (render-time suppression check) but is independently testable via its CLI commands
- **User Story 4 (P3)**: Extends the US1 pipeline prompts; no other story dependencies

### Within Each User Story

- Tests are written FIRST and must FAIL before implementation
- Read-only core (tools, state) before nodes
- Nodes before graph wiring
- Story complete before moving to next priority

### Parallel Opportunities

- All Setup tasks marked [P] can run in parallel
- All Foundational tasks marked [P] can run in parallel (within Phase 2)
- Once Foundational completes, all user stories can start in parallel (if team capacity allows)
- All tests for a user story marked [P] can run in parallel
- Within US1, all review-type nodes marked [P] can be built in parallel after T029–T035 land (T035 is redact_secrets(), a dependency of every review node)
- Different user stories can be worked on in parallel by different team members

---

## Parallel Example: User Story 1

```bash
# Launch all hosting + SAST + scope tasks together:
Task: "Implement GitHub client in veritas/hosting/github.py"
Task: "Implement GitLab client in veritas/hosting/gitlab.py"
Task: "Implement PR ref resolver in veritas/hosting/resolver.py"
Task: "Implement OpenGrep integration in veritas/security/opengrep.py"

# Launch all review-type nodes together (after prompts land):
Task: "Implement code-quality review node in veritas/review/nodes/code_quality.py"
Task: "Implement security review node in veritas/review/nodes/security.py"
Task: "Implement requirements review node in veritas/review/nodes/requirements.py"
Task: "Implement test-coverage judgment node in veritas/review/nodes/test_coverage.py"
Task: "Implement performance reasoning node in veritas/review/nodes/performance.py"
```

Then sequentially (each depends on the nodes): T041 verification → T042 render → T043 CLI → T044 graph wiring.

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 (PR review end-to-end)
4. **STOP and VALIDATE**: Run `tests/integration/test_cli_review.py` + the US1 portions of `quickstart.md` independently
5. Deploy/demo if ready

### Incremental Delivery

1. Complete Setup + Foundational → Foundation ready
2. Add User Story 1 → PR review MVP → Deploy/Demo
3. Add User Story 2 → ad-hoc scopes → Deploy/Demo
4. Add User Story 3 → suppression → Deploy/Demo
5. Add User Story 4 → project-aware review → Deploy/Demo
6. Each story adds value without breaking previous stories

### Parallel Team Strategy

With multiple developers:

1. Team completes Setup + Foundational together
2. Once Foundational is done:
   - Developer A: User Story 1 (review nodes + PR flow)
   - Developer B: User Story 3 (suppression — depends on US1 render, start after render)

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story is independently completable and testable
- Verify tests fail before implementing
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- Avoid: vague tasks, same-file conflicts, cross-story dependencies that break independence
- Filing-path convention: `veritas/` package at repo root per plan.md (NOT `src/`)
