# Veritas Constitution

## Core Principles

### I. Grounded Verification (Anti-Hallucination)
Every finding MUST be grounded in real project context and verified against a
cited source before it reaches the user. Verification MUST be a grounded re-check
that re-reads each finding's cited file and lines and confirms that the cited code
exists there: the quoted text MUST match the file at the cited lines after
whitespace normalisation and secret redaction are applied to both. Verification
confirms the citation, not the claim: it does not and cannot confirm that what the
finding says about that code is correct, and the report MUST state this limit.
Verification MUST NOT be a second LLM judging the first LLM's prose for
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
judgment (without executing the test suite), and performance reasoning — with no
per-run toggles that skip review types. Suggested changes are not a separate review
type: every finding produced by any of the five types MUST carry its own
recommendation text (FR-005), so a recommendation is never optional or skippable
even though there is no dedicated 'suggested changes' node or category.

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
(file/line references) — MUST be reproducible: the same input, configuration, and
SAST rules content MUST yield the same deterministic findings and citations. A
registry-hosted SAST ruleset can change between runs with no change on the user's
side, so every report MUST record the SAST rules source used; a local rules source
(file or directory) makes SAST output fully reproducible. LLM-narrated content
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
A zero-data-retention (ZDR) toggle MUST gate OpenRouter routing, restricting it
to zero-data-retention endpoints only when enabled. ZDR is enforced per request and
exists only on OpenRouter; on any other backend (a direct provider API or a
self-hosted endpoint), zero data retention depends on the user's account agreement
with that provider and cannot be enforced by Veritas. ZDR is off by default. Veritas
has no way to detect whether a given run is significant/proprietary versus
disposable, so this constitution does not mandate an environment-based default.
Instead, whenever ZDR is off, the system MUST print a data-retention warning exactly
once on every run, worded for the configured backend: on OpenRouter, stating that
some free-tier models explicitly reserve the right to train on inputs/outputs and
recommending ZDR be enabled for any review of proprietary or sensitive code; on any
other backend, stating that data retention is governed by the user's account
agreement with that provider. If ZDR is enabled and the configured backend is not
OpenRouter, the run MUST stop before any code is fetched or sent, with an error
stating that ZDR is only supported with OpenRouter; Veritas MUST NOT proceed as if
ZDR were in force.
Enabling ZDR is the user's informed choice; Veritas's constitutional obligation is
to make that choice informed, not to guess at or enforce an environment. Source code and diffs MAY be sent to LLM
providers only in accordance with the configured policy and never without explicit
intent. Provider API keys MUST NOT be committed or logged; they MUST be read from
environment variables or secure configuration. Every review node MUST call a
shared redaction utility on any user-facing finding text (title, description,
cited snippet, requirement explanation) before adding it to state, and the same
redaction MUST be applied to all review output, logs, and error messages —
including secrets discovered within the reviewed code itself, so a hardcoded
secret the review is reporting on is not echoed back verbatim. Redaction MUST mask
known secret shapes (such as private keys, cloud and provider API keys, access
tokens, and credentials embedded in URLs or connection strings) and any literal
value, quoted or unquoted, assigned to a key-, token-, password-, secret- or
credential-named key. Redaction is pattern-based and cannot recognise every
possible secret: a secret of no known shape that is not assigned to a
credential-named key can pass through unmasked. Review artifacts are stored only as configured and
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

**Version**: 6.0.0 | **Ratified**: 2026-09-15 | **Last Amended**: 2026-10-10