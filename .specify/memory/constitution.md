<!--
SYNC IMPACT REPORT (temporary scratch, remove before commit)
- Version change: 2.0.1 → 3.0.0 (MAJOR: redefinition of Principle II — the prior
  MUST-NOT (LLM MUST NOT add security findings) becomes a conditional allowance
  for verified LLM-identified security findings; backward-incompatible per policy)
- Modified principles:
  - II. SAST-Grounded Security → II. SAST-Grounded and Verified Security
    (redefinition: LLM-identified security findings allowed only when they pass
    the Principle I grounded citation re-check; every security finding labeled
    by source; SAST tool licensing constraint retained verbatim)
  - I. Grounded Verification (Anti-Hallucination): per-finding tool-source
    exemption replaces the former per-category exemption; LLM-originated
    findings within a tool-sourced category are NOT exempt from the re-read step
- Added sections: none
- Removed sections: none
- Deferred TODOs: none
-->

# Veritas Constitution

## Core Principles

### I. Grounded Verification (Anti-Hallucination)
Every finding MUST be grounded in real project context and verified against a
cited source before it reaches the user. Verification MUST be a grounded re-check
that re-reads each finding's cited file/line to confirm it is real and matches the
claim. Verification MUST NOT be a second LLM judging the first LLM's prose for
plausibility — that approach inherits the same hallucination failure mode, since a
hallucinated finding is usually stated in the same confident tone as a true one.
Where a specific finding is sourced directly from a real tool's output (e.g. a
SAST scanner), that tool's output is the ground truth for that finding and MAY
replace the re-read step. This exemption applies per finding by actual source,
not by category as a whole — an LLM-originated finding within a category that
also contains tool-sourced findings (for example, security) is NOT exempt and
MUST go through the re-read step. Each finding MUST be attributable: model,
prompt version, and input commit or revision MUST be recorded.

### II. SAST-Grounded and Verified Security
Security findings MAY come from two sources: the integrated SAST tool (OpenGrep)
and LLM-identified findings for security issues outside a SAST tool's
pattern-matching reach (e.g. business-logic issues such as broken access
control, where understanding intent matters more than syntactic
pattern-matching). SAST-sourced findings are ground truth as reported by the
tool. LLM-originated security findings are NOT exempt from verification: they
MUST pass the same grounded citation re-check required of other findings under
Principle I before appearing in a report. Every security finding MUST be labeled
with its source (the SAST tool, or LLM-identified-and-verified) so a reader can
calibrate trust per finding rather than assuming uniform provenance across the
security category. The SAST tool itself MUST remain fully free with unambiguous,
long-term-stable licensing — a paid tier or a history of ambiguous rule licensing
is disqualifying. Chosen tool: OpenGrep, a fully free LGPL-2.1 fork of Semgrep
with no paid tier and restored cross-file taint analysis, rule-syntax-compatible
with Semgrep.

### III. Requirement Traceability
Every review MUST check the reviewed code against the project's actual
requirements and report gaps, not just diff-level comments. Requirements sources:
spec-kit-style structured docs when available, with a freeform PRD/markdown
fallback otherwise. Requirement findings MUST distinguish satisfied / partial /
gap / unclear and MUST cite evidence (file/line references) where applicable.

### IV. Fixed Review Scope & User-Directed Selection
Review scope is always user-directed; Veritas MUST NOT track state between runs or
infer "what changed since last time" (no diff-aware auto-scoping). Supported
scopes: Project, Module/Package, File, and PR (remote, fetched via the hosting
provider's API — not a local git diff). Every run MUST perform all mandatory
review types — code quality, security/OWASP, requirement fulfillment, test-coverage
judgment (without executing the test suite), performance reasoning, and
suggested-change recommendations — with no per-run toggles that skip review types.

### V. Project-Aware Review
Reviews MUST be grounded in real project context, not inferred or generic.
Language/framework versions MUST be parsed from manifest files and review
guidance MUST adapt accordingly (e.g. MUST NOT flag Java-8-appropriate code
against Java 21 idioms). Project conventions (an AGENTS.md-equivalent / style
guide) MUST be honored. Custom project-specific review rules are natural-language
only; a separate deterministic rule-authoring tier MUST NOT be introduced.

### VI. CLI-First Interface & Markdown Report Output
Every user-facing capability MUST be exposed through the CLI. The tool SHALL use a
plain-text input/output protocol: arguments and stdin for input, stdout for
results, stderr for diagnostics and errors. Delivery MUST include a compact stdout
summary (counts and headline findings) and a full report written to a file. Report
files MUST be Markdown — chosen specifically so a report can be posted directly as
a PR comment with no conversion. The review output schema MUST be versioned.

### VII. Integration Testing
Integration tests are required for: LLM provider contracts (request and response
handling), prompt and output-schema changes, CLI behavior and output formats, and
configuration handling. Any contract change MUST be accompanied by an integration
test before merge.

### VIII. Observability, Versioning & Simplicity
Structured logging is required for LLM calls, including latency, model, and token
usage. The review output schema and CLI output follow semantic versioning;
breaking changes require a MAJOR version bump and a documented migration path.
Start simple and apply YAGNI: the following are explicitly deferred and MUST NOT
be introduced speculatively — an IDE plugin, diff-aware automatic re-scoping, a
deterministic rule-file tier, a dedicated performance-profiling tool integration,
Ollama testing/integration, and generated diff/patch output (vs. recommendation
text).

Deterministic components of a review — SAST tool output, manifest-parsed
language/framework version detection, and requirement-status evidence citations
(file/line references) — MUST be reproducible: the same input and configuration
MUST yield the same deterministic findings and citations. LLM-narrated content
(finding descriptions, recommendation wording, prioritization language) MAY vary
between runs and is explicitly exempt from this reproducibility requirement; this
variability MUST NOT be treated as a defect.

## Additional Constraints: Security, Privacy & Safety Boundaries

### LLM Backend
The LLM backend MUST be fully configurable and fully standalone — no runtime
dependency on OpenCode, even though OpenCode is used to build Veritas. Default:
free models via OpenRouter, queried at runtime for its `:free`-suffixed models
(OpenAI-compatible API) rather than a hardcoded list. If the user configures their
own API key (Claude, ChatGPT, or a self-hosted Ollama endpoint), that provider
MUST be used instead. Ollama support/testing is deferred until the rest of the
tool is complete.

### Privacy & Data Handling
A zero-data-retention (ZDR) toggle MUST gate OpenRouter routing: off during early
development/testing, and MUST be on for production-level use, restricting routing
to zero-data-retention endpoints only — some free-tier models explicitly reserve
the right to train on inputs/outputs. Source code and diffs MAY be sent to LLM
providers only in accordance with the configured policy and never without explicit
intent. Provider API keys MUST NOT be committed or logged; they MUST be read from
environment variables or secure configuration. Secrets MUST NEVER appear in review
output, logs, or error messages. Review artifacts are stored only as configured and
MUST be purgeable on request.

### Read-Only Safety Boundary
Any node responsible for read-only review MUST NOT be able to reach any write or
execute capability — no tool, subagent, or sub-graph with write/execute reachable
from it. This boundary MUST be architectural (nothing reachable can write), not a
permission flag: observed tooling behavior is that a read-only permission denial
does NOT propagate through task-tool delegation to a less-restricted subagent.

## Development Workflow & Quality Gates

Changes to the review prompt, LLM provider, or review output schema require a
Spec update and integration coverage. Models and configuration used to produce a
review MUST be recorded in the review provenance for reproducibility and audit.

### Review Suppression
Suppressions MUST live in a local, git-tracked allowlist file (e.g.
`.veritas/suppressions.json`), never in inline code comments. Entries MUST be keyed
by a fingerprint of (file, category, flagged-code snippet) — not raw line numbers —
so a suppression survives unrelated reformatting/edits yet automatically
un-suppresses if the actually-flagged code changes. Both marking and unmarking MUST
be supported.

### Orientation to the Requirements Reference
The requirements reference at `docs/veritas-requirements.md` defines the locked
product scope and constraints, including the explicit out-of-scope list. Where this
constitution and the requirements reference conflict, this constitution wins until
amended.

## Governance

This constitution supersedes all other development practices. Amendments require
documented rationale, explicit approval, and a migration plan. Versioning policy:
MAJOR for backward-incompatible principle changes or removals, MINOR for added or
materially expanded principles, PATCH for clarifications and wording fixes. Every
PR and completed review MUST verify compliance with this constitution.

**Version**: 3.0.0 | **Ratified**: 2026-09-15 | **Last Amended**: 2026-09-16