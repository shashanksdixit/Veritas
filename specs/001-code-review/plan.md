# Implementation Plan: Code Review Tool (Veritas)

**Branch**: `001-code-review` | **Date**: 2026-09-16 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-code-review/spec.md`

**Note**: This plan is filled by `/speckit.plan`; its definition describes the execution workflow.

## Summary

Build Veritas: a CLI-based, multi-agent code review tool that fetches PRs remotely via hosting-provider APIs (GitHub/GitLab), performs grounded reviews (code quality, security/OWASP with SAST + LLM-verified findings, requirement traceability, test-coverage judgment, performance reasoning; each finding with its recommendation), verifies every finding against real source before emission, and outputs a Markdown report + compact stdout summary. Orchestrated via LangGraph with architectural read-only safety boundaries; LLM backend configurable (fixed default openai:gpt-4o-mini at https://api.openai.com/v1; OpenRouter supported when configured, with the ZDR toggle); suppressions managed via a git-tracked allowlist keyed by code fingerprint.

## Technical Context

**Language/Version**: Python 3.12 (matches `.python-version`; `pyproject.toml` requires `>=3.12`)

**Primary Dependencies**:
| Dependency | Version | License | Purpose |
|-----------|---------|---------|---------|
| LangGraph | latest stable (resolve and record actual version in lockfile at install time — do not hardcode a version here that hasn't been verified against the live package index) | MIT | Review orchestration (StateGraph) |
| langchain-core | latest | MIT | LLM provider abstraction |
| langchain-openai | latest | MIT | OpenAI-compatible provider integration for init_chat_model (covers the OpenAI default + OpenRouter) |
| langchain-anthropic | latest | MIT | Anthropic native provider integration, installed when a user configures a Claude key directly (FR-020) |
| openai SDK | latest | Apache-2.0 | Transitive dependency of `langchain-openai`; not called directly by Veritas code — all LLM calls go through `init_chat_model` |
| httpx | 0.28.x | BSD-3-Clause | GitHub/GitLab REST API calls |
| Typer | latest stable (resolve and record actual version in lockfile at install time — do not hardcode a version here that hasn't been verified against the live package index) | MIT | CLI parsing (vendored Click, BSD-3-Clause) |
| pytest | latest | MIT | Integration testing (Principle VII) |
| Pydantic | v2 | MIT | Data models, state schema |

**Storage**: File-system only — `.veritas/suppressions.json` (git-tracked), report Markdown file, `.veritas/config.toml` (optional). No database.

**Testing**: pytest (integration tests for LLM contracts, CLI behavior, config handling per Principle VII)

**Target Platform**: Cross-platform CLI (Windows/Linux/macOS); run locally by a developer or CI

**Project Type**: CLI tool

**Performance Goals**: No quantitative targets in this phase (spec SC-001 is qualitative). Functional goal: review completes within a session for typical PRs (<~2k LOC changed).

**Constraints**:
- OpenGrep LGPL-2.1, invoked via subprocess (no Python API)
- ZDR toggle off by default; when on, both `provider.zdr: true` and `provider.data_collection: "deny"` in the OpenRouter request body
- Each node receives only its own frozen context; no node is bound to tools (constitution Read-Only Safety Boundary)
- Output schema versioned per constitution
- Deterministic components (SAST, manifest parsing, citations) reproducible for the same input, configuration and SAST rules content; the SAST rules source is recorded in every report, and a local rules source makes SAST fully reproducible; LLM-narrated content may vary

**Scale/Scope**: Single-user CLI tool; typical input is one PR or project directory per invocation. 6 languages (Python, Java primary; JS, C#, Go, Rust supported).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Principle | Status | Design Mapping |
|------|-----------|--------|----------------|
| **Read-Only Safety Boundary** | Additional Constraints § Read-Only | PASS | No node is bound to tools. Each analysis node (context, the five review nodes, verify) receives only a frozen, slotted `ReviewContext(llm, log)` (`veritas/review/contexts.py:28`). The scope node receives a `ScopeContext` whose hosting client is a GET-only fetcher; it may execute OpenGrep (FR-012), and the boundary test pins those two routes (`shutil` and `subprocess`, through `collect_sast`) as the only ones it reaches. Only the terminal render node receives a `RenderContext` holding the suppression store and the poster, and its only edge is to `END` (`veritas/review/graph.py:92`). Tests: `tests/integration/test_read_only_boundary.py::test_analysis_nodes_reach_no_write_or_execute_capability`, `::test_scope_node_reaches_no_write_capability_and_only_the_pinned_opengrep_routes`, `::test_render_only_edge_is_to_end`, `::test_verify_node_reaches_only_the_log`; `tests/integration/test_node_contexts.py::test_only_the_render_context_holds_the_store_and_the_poster`. The LLM endpoint is the only outbound call a review node makes. |
| **Grounded Verification** | Principle I | PASS | The verification node (`veritas/review/nodes/verification.py:591`) re-checks every non-SAST code finding with `verify_code_finding` (:191), in this order: the file is in the reviewed set (`file_not_in_scope`); `1 <= start_line <= end_line <= len(lines)` (`if start < 1:` :245 through `if end > len(lines):` :260, `line_out_of_range`); the finding quotes code (`if finding.cited_snippet is None or not finding.cited_snippet.strip():` :267, `snippet_missing`; `build_code_finding` stores a blank quote as `None`, `veritas/review/nodes/common.py:132`); the quoted code matches the cited lines after whitespace normalisation and redaction of both sides (:273-282, T086). A quoted near-miss within two lines, including one cited past the end of the file, is corrected to its real lines and `citation_adjusted_from` keeps the original; an unquoted finding is never corrected (:632-637, T132). A code finding that fails is excluded and recorded. Requirement evidence: each reference (`path`, `path:N`, `path:A-B`) is confirmed only when the file is in the reviewed set and every cited line lies between 1 and the file's length (`_evidence_problem`, :460; `if first < 1 or last > len(content.splitlines()):` :488); a failed reference is removed and recorded, and a satisfied or partial requirement left with no evidence becomes unclear. The report states both limits: "Citation verified means the quoted code was found at the cited lines; it does not mean the finding's claim was confirmed." (`veritas/output/markdown.py:456`) and `REQUIREMENT_EVIDENCE_NOTE` (:119). Only SAST findings are exempt, by actual source (`if finding.source == FindingSource.SAST:` :617); LLM-identified security findings are re-checked. No second LLM judges the findings: the verify node reaches only the log. Attribution: `ReviewRun` records `input_revision`, `model_name` and `prompt_version` (`veritas/models/entities.py:118-123`). Probe (2026-10-22, at 8cc0488): unquoted LLM-identified findings citing lines 0-0, -3 to -1 and 2-99 of a 2-line file were all rejected and recorded as `line_out_of_range`, and the same unquoted finding citing lines 1-2 was rejected as `snippet_missing`; none was kept. Tests: `tests/integration/test_verification.py::test_unquoted_llm_finding_fails_as_snippet_missing`, `::test_cited_lines_must_lie_within_the_file`, `::test_verify_node_drops_unquoted_llm_finding_but_keeps_unquoted_sast`, `::test_verify_node_never_corrects_an_unquoted_finding`, `::test_quoted_citation_outside_the_file_is_corrected_and_kept`, `::test_mismatched_snippet_fails`, `::test_verify_node_keeps_sast_and_confirmed`, `::test_each_evidence_reference_form_is_checked`, `::test_a_line_zero_reference_demotes_and_a_valid_range_stays_satisfied_through_run_review`; `tests/integration/test_read_only_boundary.py::test_verify_node_reaches_only_the_log`; `tests/integration/test_determinism.py::test_provenance_recorded_in_full_run`; `tests/unit/test_markdown_renderer.py::test_citation_verified_explanatory_sentence_under_code_findings`, `::test_the_evidence_note_is_stated_once_when_requirement_evidence_is_listed`, `::test_snippet_missing_renders_its_legend_row`. |
| **SAST-Grounded & Verified Security** | Principle II | PASS | OpenGrep runs as a subprocess (`veritas/security/opengrep.py:135`) and its results become `source=FindingSource.SAST` findings (`veritas/review/nodes/security.py:32`); LLM-identified security findings get `source=FindingSource.LLM_IDENTIFIED` (:56) and go through the full Grounded Verification re-check (scope, line bounds, quote present, quote match; see that row), because only `FindingSource.SAST` is exempt (`veritas/review/nodes/verification.py:617`). Every security finding is labelled with its source: `return "sast" if finding.source.value == "sast" else "LLM-identified (citation verified)"` (`veritas/output/markdown.py:68`), and a per-source count on stdout. The "citation verified" label holds for every LLM-identified finding that is reported, because an unquoted or out-of-range one is not reported. Engine and rules are licensed separately: OpenGrep is LGPL-2.1 with no paid tier; a registry ruleset such as `p/owasp-top-ten` (the default) carries the Semgrep Rules License, and a local rules file or directory is the alternative. README, contracts/cli.md, spec.md, the `opengrep_rules` setting description and the `veritas.security.opengrep` docstring all say so (T128). Probe (2026-10-22): the unquoted out-of-range findings in the Grounded Verification probe were LLM-identified security findings, and all were rejected. Tests: `tests/integration/test_verification.py::test_verify_node_keeps_sast_and_confirmed`, `::test_verify_node_drops_unquoted_llm_finding_but_keeps_unquoted_sast`, `::test_cited_lines_must_lie_within_the_file`; `tests/unit/test_markdown_renderer.py::test_llm_identified_finding_labeled_in_heading_and_source_row`; `tests/unit/test_compact_summary.py::test_security_source_breakdown`; `tests/unit/test_rules_licence_statement.py::test_every_ruleset_mention_states_the_rules_licence`. |
| **Requirement Traceability** | Principle III | PASS | Requirements come from spec-kit feature specs (`specs/<feature>/spec.md`, FR lines extracted with file and line) or, failing that, freeform documentation in FR-008's priority order (`veritas/review/requirements_source.py`). Findings carry satisfied / partial / gap / unclear, plus not_addressed for PR scope only (FR-007), with evidence references verified as in Grounded Verification. Tests: `tests/unit/test_requirements_source.py::test_requirements_are_extracted_with_id_text_file_and_line`, `::test_priority_follows_the_documented_order`; `tests/integration/test_requirements_discovery.py::test_free_text_sources_are_reported_as_such`; `tests/integration/test_cli_review.py::test_a_pr_that_implements_nothing_reports_not_addressed_and_stays_clean`. |
| **Fixed Review Scope** | Principle IV | PASS with deferral (T131) | Scope is user-directed per run (`--scope project`, `module`, `file` or `pr`, a PR fetched through the hosting API) and no state is kept between runs. The graph always runs all five review nodes (code quality, security, requirements, test-coverage, performance; `veritas/review/graph.py:68-92`) and has no suggested-changes node; no setting or CLI option skips one (`veritas/config/settings.py:55-97`, `veritas/cli/app.py:74-80`; a grep of `veritas/` for `skip_review`, `skip_node`, `disable_security`, `--skip` and `--only` finds none). Code findings: the four code prompts require a `recommendation` (`veritas/review/prompts/code_quality.md:24`, `security.md:30`, `test_coverage.md:33`, `performance.md:27`), and the verify node enforces it after the citation check: `recommendation_failure` (`veritas/review/nodes/verification.py:312`; `if finding.recommendation.strip():` :328) excludes a finding whose recommendation is absent, empty or whitespace and records it as `recommendation_missing` (`_keep_if_recommended`, :604; T133). SAST findings carry a fixed recommendation (`veritas/security/sast_results.py:46`). Requirement findings: `RequirementFinding` has no recommendation field (`veritas/models/entities.py:204-212`), and `explanation_failure` (`veritas/review/nodes/verification.py:434`; `if finding.explanation.strip():` :449) marks a requirement whose explanation is absent, empty or whitespace unclear with the fixed explanation "The reviewer gave no explanation for this requirement's status." and records it as `explanation_missing` (`_keep_if_explained`, :661; T134). Probe (2026-10-22, at 8cc0488): a code finding with no `recommendation` and one with `"   "` were both rejected and recorded as `recommendation_missing`; a satisfied requirement with explanation `"   "` (through `build_requirement_finding`) and a partial one with `"  \n"` (built directly) were both kept as unclear with the fixed explanation and recorded as `explanation_missing`. Tests: `tests/integration/test_verification.py::test_verify_node_records_a_finding_without_a_recommendation`, `::test_recommendation_failure_treats_blank_text_as_missing`, `::test_a_citation_failure_is_recorded_before_a_missing_recommendation`, `::test_verify_node_keeps_a_sast_finding_whatever_its_recommendation`, `::test_a_blank_explanation_is_marked_unclear_and_recorded`, `::test_a_structured_satisfied_or_partial_answer_with_no_explanation_is_marked_unclear`, `::test_a_requirement_with_no_explanation_is_reported_unclear_through_run_review`. **Deferred (T131):** a user-directed scope is not always reviewed, or reported, in full: the local walk stops at `MAX_SCOPE_FILES` (500) without reporting the files beyond it (`if len(files) >= MAX_SCOPE_FILES:` `veritas/review/nodes/scope.py:423`), a file whose read fails with an operating-system error is skipped without being reported in Coverage (`except OSError:` :427), the local walk does not respect `.gitignore`, and `_SKIP_FILES` (:84) is not applied in the PR path (`_fetch_pr`, :434). |
| **Project-Aware Review** | Principle V | PASS | Language and framework versions are parsed from manifests (pyproject.toml, pom.xml, build.gradle, package.json, go.mod, Cargo.toml, .csproj; `veritas/utils/project_context.py:102-235`, FR-009). Conventions come from the first non-blank AGENTS.md, CONVENTIONS.md or STYLE.md (:260, FR-010) and custom rules from a non-blank `.veritas/rules.md` (:276, FR-011), both redacted and capped at 4,000 characters. Custom rules are natural language only; there is no rule-file tier. Tests: `tests/integration/test_project_aware_review.py::test_project_context_flows_into_prompts`, `::test_conventions_and_rules_reach_the_model_redacted`, `::test_a_blank_agents_md_does_not_hide_style_md`; `tests/integration/test_project_context_golden.py`. |
| **CLI-First & Markdown Report** | Principle VI | PASS | Every capability is a Typer command (`review`, `suppress`, `unsuppress`; `veritas/cli/app.py`). The compact summary goes to stdout; diagnostics and errors go to stderr through `Log` (`veritas/utils/logging.py:3`). The full report is a Markdown file (`--output`, default `./veritas-report-<timestamp>.md`). The schema is versioned: `SCHEMA_VERSION = "1.15.0"` (`veritas/config/constants.py:10`). Tests: `tests/integration/test_cli_review.py::test_project_review_succeeds_and_writes_report`, `::test_cli_invalid_scope_exits_1`; `tests/unit/test_compact_summary.py::test_error_headline_capped_at_ten`; `tests/integration/test_schema_version.py::test_schema_version_in_report_marker`. |
| **Integration Testing** | Principle VII | PASS | Integration tests cover each area the principle names: LLM provider contracts (`tests/integration/test_llm_provider.py`, `test_llm_output_limit.py`, `test_zdr.py::test_zdr_true_reaches_the_wire_as_a_top_level_provider_block`), prompt and output-schema changes (`test_schema_version.py::test_schema_version_matches_data_model`, `::test_schema_version_recorded_in_changelog`, `test_project_context_golden.py`), CLI behaviour and output formats (`test_cli_review.py`, `test_cli_suppress.py`) and configuration handling (`test_config.py`). |
| **Observability & Versioning** | Principle VIII | PASS | Every LLM call is logged in every run, with or without `--verbose`, as one stderr line ending in a sorted JSON object with model, latency_ms, prompt_tokens, completion_tokens and total_tokens (null when the provider reports no usage; read from `usage_metadata` first) (`Log.llm_call`, `veritas/utils/logging.py:63`, called from `veritas/llm/client.py:401` and :419; FR-019, T135). Every log line and CLI error passes through `redact_secrets()` (`veritas/utils/logging.py:49`, `veritas/cli/app.py:41`). The schema (`SCHEMA_VERSION = "1.15.0"`, `veritas/config/constants.py:10`) and prompts (`PROMPT_VERSION = "1.9.0"`, :15) follow semver, and every schema change is recorded in CHANGELOG.md with a migration note (1.13.0 `snippet_missing`, 1.14.0 `recommendation_missing` and 1.15.0 `explanation_missing` are each additive, with a note that reports written at the previous version still validate). YAGNI: a grep of `veritas/*.py` for Ollama, `difflib.unified_diff`, `.patch`, cProfile, pyinstrument, LSP and VS Code finds only `.vscode` in the scope walk's skipped-directory list (`veritas/review/nodes/scope.py:74`), which is not an integration. Tests: `tests/unit/test_logging.py::test_verbose_structured_line_is_single_line_too`, `::test_llm_call_line_is_structured_without_verbose`, `::test_llm_call_line_without_usage_carries_null_token_fields`, `::test_llm_call_line_is_the_same_with_and_without_verbose`, `::test_failed_llm_call_line_is_structured_without_verbose`, `::test_other_lines_stay_plain_without_verbose`; `tests/unit/test_log_redaction.py::test_llm_call_error_is_masked`, `::test_llm_call_fields_are_masked_without_verbose`; `tests/integration/test_llm_call_logging.py::test_openai_usage_is_logged_without_verbose`, `::test_anthropic_style_usage_metadata_is_logged`, `::test_anthropic_style_response_metadata_alone_is_logged_without_a_computed_total`, `::test_a_response_without_usage_logs_null_token_counts`; `tests/integration/test_schema_version.py::test_schema_version_recorded_in_changelog`; `tests/unit/test_code_package.py::test_prompt_version_is_current`. |
| **Determinism** | Principle VIII | PASS | SAST output, manifest-parsed versions and requirement citations are deterministic (FR-025); the SAST rules source is recorded in every report (`Report.run.sast_rules`, rendered at `veritas/output/markdown.py:336` and :338), and a local rules source makes SAST reproducible. LLM-narrated content is exempt. Tests: `tests/integration/test_determinism.py::test_two_runs_identical_report`, `::test_sast_finding_grounded_and_deterministic`, `::test_fingerprint_identical_across_runs`; `tests/integration/test_opengrep.py::test_report_records_sast_rules_and_renders_the_line`; `tests/unit/test_requirements_source.py::test_extraction_is_deterministic`. (T131's unsorted unsupported-language list is outside the components this principle names.) |
| **Privacy & Data Handling** | Additional Constraints § Privacy | PASS with deferral (T103) | Keys are read from `.veritas/config.local.toml` (gitignored), an explicit `--config` file, or `VERITAS_*` environment variables; a key or token in the committed `.veritas/config.toml` is refused with exit 1, under any spelling of its path (`_reject_committed_secrets`, `veritas/config/settings.py:203`; FR-020, T107, T108), and secrets are excluded from the config hash. Every review node redacts finding text before it enters state: title, description, recommendation and cited snippet in `build_code_finding` (`veritas/review/nodes/common.py:156-160`), requirement text and explanation in `build_requirement_finding` (:169, :172); the same redaction is applied to the report, stdout, logs and error messages, including secrets in reviewed code. Redaction masks known secret shapes and any literal assigned to a credential-named key (FR-013); it is pattern-based, so a secret of no known shape not assigned to such a key can pass unmasked. ZDR adds `provider.zdr: true` and `provider.data_collection: "deny"` to the OpenRouter request body (`veritas/llm/zdr.py:64`); with ZDR off a backend-specific warning is printed once per run, and ZDR on a non-OpenRouter backend stops the run before anything is fetched or sent (`_gate_zdr`, `veritas/review/graph.py:167`; FR-021). A review leaves only the report and its JSON copy. Tests: `tests/integration/test_config.py::test_cli_rejects_a_secret_in_the_committed_config`, `::test_cli_config_option_naming_the_committed_file_rejects_its_secret`, `::test_cli_accepts_a_secret_in_the_local_config`; `tests/integration/test_output_redaction.py::test_credentialed_mr_url_token_appears_in_no_output`; `tests/integration/test_zdr.py::test_cli_fails_closed_when_zdr_on_a_non_openrouter_backend`, `::test_data_retention_warning_printed_exactly_once_per_run`; `tests/integration/test_purgeable_artifacts.py::test_review_leaves_only_the_report_and_its_json_copy`. **Deferred (T103):** OpenGrep's own log and temporary rules files are written outside a Veritas-managed directory, so purging them is not under Veritas's control; README documents them. |
| **Suppression** | Dev Workflow § Suppression | PASS | Suppressions live in the git-tracked `.veritas/suppressions.json` (`veritas/suppression/store.py:24`; `git check-ignore` does not match it); there is no inline-comment mechanism. Fingerprint: SHA-256 of file + category + whitespace-normalised snippet (`compute_fingerprint`, `veritas/models/entities.py:328`). `suppress` and `unsuppress` both exist. Tests: `tests/unit/test_fingerprint.py::test_unrelated_whitespace_edits_same_hash`, `::test_changed_snippet_different_hash`; `tests/integration/test_cli_suppress.py::test_suppress_then_rerun_excludes_finding`, `::test_unsuppress_removes_entry`; `tests/integration/test_purgeable_artifacts.py::test_last_report_json_is_gitignored_but_committable_veritas_files_are_not`. |
| **Licensing Discipline** | Principle II (SAST tool) | PASS | The engine is OpenGrep, LGPL-2.1 with no paid tier, invoked only as a subprocess (no Python API). Its licence does not cover rules: registry rulesets such as the default `p/owasp-top-ten` carry the Semgrep Rules License, using one is the user's choice, and a local rules file or directory is the alternative; every place that names a registry ruleset says so (T128; `tests/unit/test_rules_licence_statement.py::test_every_ruleset_mention_states_the_rules_licence`). httpx is used instead of PyGithub/python-gitlab to avoid LGPL. Runtime licences were checked 2026-10-22 from installed metadata for all 54 installed packages that `uv export --no-dev` lists (55 listed; httpx2-jsfetch is emscripten-only and not installed, and the optional `anthropic` extra is not included). The 8 direct runtime dependencies are MIT or BSD-3-Clause. Exactly two packages, both transitive, are copyleft: certifi 2026.7.22 is `MPL-2.0` (pulled in through httpx, httpcore, langchain-openai and requests) and orjson 3.12.0 is `MPL-2.0 AND (Apache-2.0 OR MIT)` (through langgraph-sdk and langsmith). MPL-2.0 is a file-level copyleft licence, and Veritas uses both packages unmodified. No installed runtime package is GPL, LGPL or AGPL. See `research.md` § 6. |

**Checked 2026-10-22 at commit 8cc0488 against constitution v8.0.0 (T099; schema 1.15.0, prompts 1.9.0): 11 gates PASS, 2 PASS with deferral (Fixed Review Scope, T131; Privacy & Data Handling, T103), 0 FAIL.**

## Project Structure

### Documentation (this feature)

```text
specs/001-code-review/
├── plan.md              # This file (/speckit.plan output)
├── research.md          # Phase 0 output — all unknowns resolved
├── data-model.md        # Phase 1 output — Pydantic models
├── quickstart.md        # Phase 1 output — validation scenarios
├── contracts/           # Phase 1 output — interface contracts
│   ├── cli.md           #   CLI argument contract (Typer)
│   └── hosting-api.md   #   GitHub/GitLab REST API contracts
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
veritas/
├── __init__.py
├── main.py                          # Entry point (replaces root main.py)
├── cli/
│   ├── __init__.py
│   └── app.py                       # Typer app: review, suppress, unsuppress
├── config/
│   ├── __init__.py
│   ├── settings.py                  # Pydantic Settings: env vars, config file
│   └── constants.py                 # Defaults, version string
├── models/
│   ├── __init__.py
│   └── entities.py                  # ReviewRun, CodeFinding, RequirementFinding,
│                                    #   SuppressionEntry, VerificationFailure, Coverage,
│                                    #   FailedBatch, Report, Summary (from data-model.md)
├── hosting/
│   ├── __init__.py
│   ├── github.py                    # GitHub REST via httpx (PR files, contents, comments)
│   ├── gitlab.py                    # GitLab REST via httpx
│   ├── factory.py                   # HostingClients(fetcher, poster), build_hosting_client (T111)
│   ├── resolver.py                  # Parse --target PR ref → provider + owner/repo/id
│   └── types.py                     # Provider-neutral values returned by the hosting fetchers (T115)
├── llm/
│   ├── __init__.py
│   ├── client.py                    # LLM client via LangChain's init_chat_model (provider:model string resolved from config)
│   └── zdr.py                       # ZDR toggle logic (provider.zdr and provider.data_collection params)
├── review/
│   ├── __init__.py
│   ├── graph.py                     # LangGraph StateGraph definition
│   ├── contexts.py                  # Frozen node contexts: ReviewContext, ScopeContext, RenderContext (T111)
│   ├── state.py                     # ReviewState TypedDict
│   ├── batching.py                  # Deterministic batch planner for code-review inputs (FR-029, T076/T083)
│   ├── test_index.py                # Test-file identification + relevance-ordered per-batch test index (FR-004, T081/T082/T084)
│   ├── requirements_source.py       # Requirement source discovery + FR-NNN extraction (FR-008, T087)
│   ├── nodes/
│   │   ├── __init__.py
│   │   ├── scope.py                 # Scope resolution (parse target, fetch files)
│   │   ├── context.py               # Project-context node: manifests, conventions, NL rules (FR-009–011, T057)
│   │   ├── common.py                # Shared node helpers: prompt loading, line-numbered code package, per-batch LLM calls (T068/T080/T092)
│   │   ├── code_quality.py          # Code quality review node
│   │   ├── security.py              # Security/OWASP review node
│   │   ├── requirements.py          # Requirement traceability node
│   │   ├── test_coverage.py         # Test coverage judgment node
│   │   ├── performance.py           # Performance reasoning node
│   │   ├── verification.py          # FR-013 citation re-verification node
│   │   └── render.py                # Report file + stdout summary (WRITE — post-review)
│   ├── tools/
│   │   ├── __init__.py
│   │   └── write_tools.py           # write_report, persist_last_report_json (WRITE; render node only)
│   └── prompts/
│       ├── __init__.py
│       ├── code_quality.md
│       ├── security.md
│       ├── requirements.md
│       ├── requirements_structured.md   # Per-batch evaluation of extracted requirements (FR-007, T088)
│       ├── test_coverage.md
│       └── performance.md
├── security/
│   ├── __init__.py
│   ├── opengrep.py                  # OpenGrep subprocess invocation + JSON parsing
│   └── sast_results.py              # Pure mapping of OpenGrep results to finding dicts (T111)
├── suppression/
│   ├── __init__.py
│   ├── fingerprint.py               # compute_fingerprint() per data-model.md
│   ├── store.py                     # Read/write .veritas/suppressions.json
│   └── resolver.py                  # Find suppression by ID or filename+line
├── output/
│   ├── __init__.py
│   ├── markdown.py                  # Report → Markdown renderer
│   ├── compact.py                   # Report → stdout compact summary
│   └── summary.py                   # compute_summary() — verdict + counts aggregation (T041a)
└── utils/
    ├── __init__.py
    ├── logging.py                    # Structured logging (Principle VIII); redacts every line it writes
    ├── redaction.py                  # Shared redact_secrets() utility (constitution Privacy & Data Handling) — called by every review node on finding text before it enters state, and by Log on every log and error line
    ├── languages.py                  # Supported-language detection + skip logic
    ├── paths.py                      # Path matching helpers for exclusion patterns (FR-029, T075)
    └── project_context.py            # Manifest/convention/NL-rule parsing (FR-009–011, T056)

tests/
├── conftest.py                      # Shared fixtures: deterministic fake LLM, sample project, settings, fake_secret()
├── boundary_walker.py               # Reachability walker for the read-only boundary test (T112, FR-026)
├── contract/
│   ├── test_github_api.py           # GitHub API contract (mocked httpx)
│   └── test_gitlab_api.py           # GitLab API contract (mocked httpx)
├── integration/
│   ├── conftest.py                  # No real SAST binary, sandboxed CWD
│   ├── data/
│   │   └── project_context_golden.json # Project-context golden cases a-g (T116)
│   ├── project_context_cases.py     # Builders for the project-context golden cases (T116, T118, T119)
│   ├── test_project_context_golden.py # Project-context golden tests (T116, T118)
│   ├── test_pr_project_context.py   # PR project context read through the hosting API (T120, FR-009–011)
│   ├── test_hosting_clients.py      # Hosting fetcher/poster split, token errors (T110, T111)
│   ├── test_node_contexts.py        # Frozen per-node contexts, hosting built before the graph (T111, FR-026)
│   ├── test_read_only_boundary.py   # Read-only boundary verified by reachability (T112, FR-026)
│   ├── test_pr_fetch_errors.py      # PR fetch API error → one [error] line, exit 1 (T121)
│   ├── test_pr_withheld_content.py  # PR files with withheld content reported as not reviewed (T124, FR-029)
│   ├── test_post_failure.py         # A failed --post never changes the exit code (T100, FR-028)
│   ├── test_output_redaction.py     # Every finished output redacted where written or sent (T105, FR-013)
│   ├── test_input_revision.py       # Local-scope input revision (T101, FR-024)
│   ├── test_purgeable_artifacts.py  # Review artifacts are purgeable (T102, FR-030)
│   ├── test_llm_provider.py         # LLM provider request/response contracts
│   ├── test_llm_output_limit.py     # Provider error URL removal, [llm] max_output_tokens (FR-019, FR-029)
│   ├── test_zdr.py                  # ZDR toggle (FR-021, T079)
│   ├── test_cli_review.py           # CLI review command behavior
│   ├── test_cli_suppress.py         # CLI suppress/unsuppress behavior
│   ├── test_config.py               # Config loading (env, file, CLI flags)
│   ├── test_opengrep.py             # OpenGrep invocation + JSON parsing
│   ├── test_verification.py         # Verification node re-check logic
│   ├── test_prefixed_citations.py   # Cited snippets copied with the prompts' line numbers (FR-014, T080)
│   ├── test_batch_review.py         # Batched code review consumption (FR-029, T076)
│   ├── test_review_exclusion.py     # Scope exclusion (FR-029, T075)
│   ├── test_test_coverage_index.py  # Relevance-ordered test index for the test-coverage review (FR-004)
│   ├── test_requirements_discovery.py # Requirement source discovery + extraction in the scope node (FR-008)
│   ├── test_requirements_batches.py # Per-batch requirement evaluation (FR-007)
│   ├── test_determinism.py          # Run-level determinism (SC-004)
│   ├── test_schema_version.py       # Output-schema versioning tripwire (FR-016)
│   └── test_project_aware_review.py # Project-context influence on reports (US4)
└── unit/
    ├── test_fingerprint.py          # Suppression fingerprint determinism
    ├── test_suppression_resolver.py # Suppression resolution rules (FR-017)
    ├── test_markdown_renderer.py    # Report → Markdown output
    ├── test_compact_summary.py      # Report → stdout compact output
    ├── test_failure_summary.py      # Concise run failure output (FR-027, T098)
    ├── test_scope_resolver.py       # PR ref parsing, scope validation
    ├── test_models.py               # Pydantic model validation
    ├── test_redaction.py            # redact_secrets() masking
    ├── test_log_redaction.py        # Every log line and CLI error message redacted (T104, FR-013)
    ├── test_llm_retry_after.py      # Retry-After parsing for the in-flight-budget retry (T114, FR-019)
    ├── test_review_tools.py         # review/tools holds only the render node's write tools (T113)
    ├── test_rules_licence_statement.py # SAST rules licence stated wherever a registry ruleset is named (T128)
    ├── test_llm_error_summary.py    # summarize_llm_error() one-line summaries (FR-029)
    ├── test_logging.py              # Atomic structured logging
    ├── test_languages.py            # Unsupported-language skip/note logic
    ├── test_project_context.py      # Manifest/convention/NL-rule parsing
    ├── test_exclusion_patterns.py   # Exclusion-pattern matching (FR-029)
    ├── test_batching.py             # Deterministic batch planner (FR-029)
    ├── test_code_package.py         # Line-numbered, line-boundary-truncated code package (FR-014)
    ├── test_line_number_prefixes.py # Cited-snippet line-number prefix stripping (FR-014)
    ├── test_findings_filters.py     # Do-not-report finding filters (FR-014)
    ├── test_test_index.py           # Relevance-ordered test index (FR-004)
    ├── test_requirements_source.py  # Requirement source discovery + extraction (FR-008)
    ├── test_requirements_merge.py   # Merging per-batch requirement answers (FR-007)
    └── test_summary.py              # Verdict derivation (T041b)
```

**Structure Decision**: Single project layout. All source under `veritas/` package (importable). CLI entry point via `veritas.main:app` (Typer). Tests mirror source structure. No frontend — CLI-only (constitution Principle VI). The `review/nodes/` directory separates each review-type node into its own module for clarity; `review/tools/` holds only the render node's write tools.

## Complexity Tracking

> No constitution gates were violated. No complexity justifications needed.
