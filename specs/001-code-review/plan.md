# Implementation Plan: Code Review Tool (Veritas)

**Branch**: `001-code-review` | **Date**: 2026-09-16 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-code-review/spec.md`

**Note**: This plan is filled by `/speckit.plan`; its definition describes the execution workflow.

## Summary

Build Veritas: a CLI-based, multi-agent code review tool that fetches PRs remotely via hosting-provider APIs (GitHub/GitLab), performs grounded reviews (code quality, security/OWASP with SAST + LLM-verified findings, requirement traceability, test-coverage judgment, performance reasoning; each finding with its recommendation), verifies every finding against real source before emission, and outputs a Markdown report + compact stdout summary. Orchestrated via LangGraph with architectural read-only safety boundaries; LLM backend configurable via OpenRouter (free models default, ZDR toggle); suppressions managed via a git-tracked allowlist keyed by code fingerprint.

## Technical Context

**Language/Version**: Python 3.12 (matches `.python-version`; `pyproject.toml` requires `>=3.12`)

**Primary Dependencies**:
| Dependency | Version | License | Purpose |
|-----------|---------|---------|---------|
| LangGraph | latest stable (resolve and record actual version in lockfile at install time — do not hardcode a version here that hasn't been verified against the live package index) | MIT | Review orchestration (StateGraph, ToolNode) |
| langchain-core | latest | MIT | LLM provider abstraction |
| langchain-openai | latest | MIT | OpenAI-compatible provider integration for init_chat_model (covers OpenRouter default + OpenAI) |
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
- All review nodes structurally bound to read-only tools only (constitution Read-Only Safety Boundary)
- Output schema versioned per constitution
- Deterministic components (SAST, manifest parsing, citations) reproducible; LLM-narrated content may vary

**Scale/Scope**: Single-user CLI tool; typical input is one PR or project directory per invocation. 6 languages (Python, Java primary; JS, C#, Go, Rust supported).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Principle | Status | Design Mapping |
|------|-----------|--------|----------------|
| **Read-Only Safety Boundary** | Additional Constraints § Read-Only | PASS | Review nodes use `ToolNode(read_only_tools)` only; write nodes (`ToolNode(write_tools)`) are terminal, no back-edges. Structural — tools not in the dict. |
| **Grounded Verification** | Principle I | PASS | Dedicated verification node re-reads file/line for every non-SAST finding (FR-013). Per-finding exemption by actual source — LLM-identified security findings go through re-check. |
| **SAST-Grounded & Verified Security** | Principle II | PASS | OpenGrep subprocess (LGPL-2.1, no paid tier) produces ground-truth findings (`source="sast"`). LLM-identified security findings (`source="llm-verified"`) pass verification. Every security finding labeled by source. |
| **Requirement Traceability** | Principle III | PASS | Review checks code against project requirements (spec-kit or freeform). Findings carry status (satisfied/partial/gap/unclear/not_addressed, per FR-007; not_addressed is PR scope only) + evidence citations. |
| **Fixed Review Scope** | Principle IV | PASS | Scope is user-directed per run. No state between runs. Five review-type nodes (code quality, security, requirements, test-coverage, performance) each produce findings that include recommendation text; all mandatory, no per-run toggles. |
| **Project-Aware Review** | Principle V | PASS | Language/framework versions parsed from manifest files (FR-009). Project conventions honored (FR-010). Custom NL rules supported (FR-011). |
| **CLI-First & Markdown Report** | Principle VI | PASS | All capability via CLI (Typer). Compact stdout + full Markdown file. Output schema versioned. |
| **Integration Testing** | Principle VII | PASS | pytest integration tests planned for: LLM provider contracts, prompt/schema changes, CLI behavior, config handling. |
| **Observability & Versioning** | Principle VIII | PASS | Structured logging for LLM calls (latency, model, tokens). CLI output follows semver. YAGNI: no IDE plugin, no diff-aware rescoping, no deterministic rule tier, no profiler, no Ollama, no diff/patch output. |
| **Determinism** | Principle VIII | PASS | SAST output, manifest detection, requirement citations are deterministic (FR-025). LLM-narrated content exempt. |
| **Privacy & Data Handling** | Additional Constraints § Privacy | PASS | API keys from env vars only; never committed/logged. Secrets never in output/logs/errors, including secrets found within reviewed code — every review node calls the shared `redact_secrets()` utility on finding text before it enters state. ZDR toggle via both `provider.zdr: true` and `provider.data_collection: "deny"` in the OpenRouter request body when enabled; a stderr warning is printed on every run where ZDR is off, per FR-021. Config file `.veritas/config.toml` has no keys; `.veritas/config.local.toml` (gitignored) for local overrides. |
| **Suppression** | Dev Workflow § Suppression | PASS | `.veritas/suppressions.json` git-tracked. Fingerprint: SHA-256 of (file + category + normalized snippet). Add + remove supported. |
| **Licensing Discipline** | Principle II (SAST tool) | PASS | OpenGrep LGPL-2.1, no paid tier. All Python deps permissive (MIT/BSD-3-Clause/Apache-2.0). httpx used instead of PyGithub/python-gitlab to avoid LGPL. See `research.md` § 6 for full license table. |

**All gates PASS. No violations to justify.**

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
│   └── resolver.py                  # Parse --target PR ref → provider + owner/repo/id
├── llm/
│   ├── __init__.py
│   ├── client.py                    # LLM client via LangChain's init_chat_model (provider:model string resolved from config)
│   ├── models.py                    # Free-model discovery, model selection
│   └── zdr.py                       # ZDR toggle logic (provider.zdr and provider.data_collection params)
├── review/
│   ├── __init__.py
│   ├── graph.py                     # LangGraph StateGraph definition
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
│   │   ├── read_only.py             # read_file, list_dir, grep_code, get_ast
│   │   └── write_tools.py           # write_report, update_suppressions (WRITE)
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
│   └── opengrep.py                  # OpenGrep subprocess invocation + JSON parsing
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
    ├── logging.py                    # Structured logging (Principle VIII)
    ├── redaction.py                  # Shared redact_secrets() utility (constitution Privacy & Data Handling) — called by every review node on finding text before it enters state
    ├── languages.py                  # Supported-language detection + skip logic
    ├── paths.py                      # Path matching helpers for exclusion patterns (FR-029, T075)
    └── project_context.py            # Manifest/convention/NL-rule parsing (FR-009–011, T056)

tests/
├── conftest.py                      # Shared fixtures: deterministic fake LLM, sample project, settings, fake_secret()
├── contract/
│   ├── test_github_api.py           # GitHub API contract (mocked httpx)
│   └── test_gitlab_api.py           # GitLab API contract (mocked httpx)
├── integration/
│   ├── conftest.py                  # No real SAST binary, sandboxed CWD
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

**Structure Decision**: Single project layout. All source under `veritas/` package (importable). CLI entry point via `veritas.main:app` (Typer). Tests mirror source structure. No frontend — CLI-only (constitution Principle VI). The `review/nodes/` directory separates each review-type node into its own module for clarity; the `review/tools/` directory separates read-only tools from write tools to enforce the constitutional boundary at the import level.

## Complexity Tracking

> No constitution gates were violated. No complexity justifications needed.
