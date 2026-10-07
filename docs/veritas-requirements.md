# Veritas — Requirements Specification

**Working name:** ReviewBuddy → **Locked name:** Veritas
**Status:** Requirements workshop complete. To be refined further once build starts.

## 1. Purpose

Veritas is a multi-agent code review tool (Python / LangChain / LangGraph) that reviews code for quality, security, and requirement compliance — grounded wherever possible in real tools and real project context rather than LLM opinion alone, and with an explicit verification step to catch hallucinated findings before they reach the user.

It is not intended to compete with generic AI PR-review tools on being "the best possible reviewer with the least effort." Its value is in: requirement-traceability against a project's actual spec/PRD, a grounded verification layer, SAST-backed security findings, explicit control over data privacy, and full ownership of the stack.

## 2. Review Modes

Veritas supports four scopes, selected per run:

| Mode | Description |
|---|---|
| Project | Whole project, but not auto-run against the full repo every time — user points Veritas at it |
| Module/Package | A specific directory/module |
| File | A single file |
| PR | A remote pull request, fetched via the hosting provider's API (not a local git diff) |

**Workflow assumption for PR mode:** a developer opens a PR; a lead runs Veritas against it.

Scope is always user-directed. Veritas does not track state between runs to infer "what changed since last time" (no diff-aware auto-scoping).

## 3. Review Types (all mandatory, no toggles)

Every run performs all of the following — there is no per-run selection of which checks to run:

1. **Code quality** — maintainability, code smells, dead code, etc.
2. **Security / OWASP** — see §5.
3. **Requirement fulfillment** — checks code against the project's requirements and reports gaps, not just diff-level comments. See §4.
4. **Test coverage (judgment, not execution)** — Veritas does not run the test suite (language-specific tools already do that well). Instead it judges whether existing tests actually exercise the business logic of the method under review, and suggests changes if they don't.
5. **Performance** — LLM reasoning only, not paired with a separate profiler (kept out to avoid extra third-party tool licensing).
6. **Suggested changes** — delivered as a recommendation string in the report, not a generated diff/patch. (Diff-apply is an IDE-shaped feature; Veritas is CLI-first.)

## 4. Requirement Traceability

- Requirements source: **spec-kit style docs** when available (structured: `spec.md`, `contracts/*.md`, `data-model.md`, etc.), with a **freeform PRD/markdown fallback** when they aren't.
- Output distinguishes `satisfied` / `partial` / `gap` / `unclear` per requirement, with evidence (file/line references) where applicable.

## 5. Security / OWASP

- Combines a real **SAST tool** with LLM reasoning — not LLM alone. For security findings, the SAST tool's output is the source of truth; the LLM narrates and prioritizes rather than inventing findings.
- **Chosen SAST tool: OpenGrep** — a fully free, LGPL-2.1 fork of Semgrep with no paid tier, chosen over Semgrep OSS specifically for long-term licensing safety (Semgrep's rule licensing has a history of ambiguity, and its free tier lacks cross-file taint analysis, which OpenGrep restores). OpenGrep is rule-syntax-compatible with Semgrep.

## 6. Project-Aware Review

Veritas grounds its review in real project context rather than inferring it:

- **Language/framework version detection** — parsed from manifest files (`pom.xml`/`build.gradle`, `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, `.csproj`), including things like Java 8 vs. 21, or Spring Boot version. Review guidance adapts accordingly (e.g. don't flag Java-8-appropriate code against Java 21 idioms).
- **Project conventions** — an `AGENTS.md`-equivalent/style guide should be honored, not just spec/PRD compliance, so the review reflects the project's own standards rather than being generic.
- **Custom project-specific review rules** — natural-language only (no separate deterministic rule-authoring tier). Kept simple deliberately, since natural language is a natural fit for an LLM-based tool and lowers the barrier to actually writing rules.

## 7. Verification / Anti-Hallucination Design

This is a first-class design requirement, not an afterthought — motivated by directly-observed failures during the OpenCode tutorial (a confidently-stated false finding, and a hallucinated file path that silently prevented a file from being read).

- **Verification step**: a grounded re-check that re-reads each finding's cited file/line to confirm it is real and matches the claim — **not** a second LLM judging the first LLM's prose for plausibility (that approach was explicitly rejected — it inherits the same failure mode, since a hallucinated finding is often stated in the same confident tone as a true one).
- Security findings are grounded via the SAST tool output directly (see §5), sidestepping LLM-invented vulnerability claims entirely.

## 8. Output

- **Format**: structured findings internally, with two shapes:
  - `code_findings[]` — location-based (file, line range, severity, category, OWASP/CWE id where applicable, title, description, recommendation, confidence)
  - `requirement_findings[]` — requirement-based (requirement reference, status, evidence, explanation)
  - plus a `summary` (counts by severity/category, requirement status counts, overall verdict)
- **Delivery**: both **stdout** (compact — counts and headline findings) and a **file** (full report), since stdout alone is unwieldy for large finding counts.
- **File format: Markdown**, not HTML or JSON — chosen specifically so the report can be posted directly as a PR comment on GitHub/GitLab with no conversion, matching the PR-review workflow.

## 9. Suppression

- A local, **git-tracked allowlist file** (e.g. `.veritas/suppressions.json`) — not inline code comments — keeps Veritas's opinions out of the reviewed source.
- Entries are keyed by a **fingerprint** of `(file, category, flagged-code snippet)`, not raw line numbers, so a suppression survives unrelated reformatting/edits but automatically "un-suppresses" itself if the actually-flagged code changes.
- Both marking and unmarking are supported (add/remove an entry).

## 10. LLM Backend

- **Fully configurable**, and **fully standalone** — no runtime dependency on OpenCode itself, even though OpenCode is the tool used to build Veritas.
- **Default**: free models via **OpenRouter**, queried at runtime for its `:free`-suffixed models (OpenAI-compatible API) rather than a hardcoded list.
- **Override**: if the user configures their own API key (Claude, ChatGPT, etc.), that provider is used instead.
- The same configured-key path also covers a **self-hosted Ollama** endpoint, if one is configured. Ollama support/testing is explicitly deferred until the rest of the tool is complete — not an early-stage priority.

## 11. Privacy

- A **ZDR (zero data retention) toggle** for OpenRouter routing:
  - **Off** during early development/testing — free models, privacy not a concern at that stage.
  - **On** for production-level use — restricts routing to zero-data-retention (typically paid) endpoints only.
- Motivation: some free-tier models on OpenRouter explicitly reserve the right to train on inputs/outputs; ZDR routing is the mechanism to guarantee that doesn't happen when it matters.

## 12. Language Support

- Primary: **Python, Java**
- Also supported: **JavaScript, .NET (C#), Go, Rust**

## 13. Interface

- **CLI first.** An IDE plugin was considered and explicitly deprioritized for now.

## 14. Architectural Constraint Carried Forward from Tooling Research

During the OpenCode-based tutorial work that preceded this spec, empirical testing found that an agent's own "read-only" permission denial (`bash: deny`, `edit: deny`) does **not** propagate through task-tool delegation to a less-restricted subagent — the denial only holds for that agent's own direct tool calls. This is a direct design constraint for Veritas's LangGraph graph: any node responsible for read-only review must not have access to any tool, subagent, or sub-graph with write/execute capability at all — the boundary needs to be architectural (nothing reachable can write), not a permission flag trusted to hold across a delegation boundary.

## 15. Explicitly Out of Scope / Deferred

- IDE plugin
- Diff-aware automatic re-scoping between runs
- A separate deterministic (rule-file-based) tier for custom project rules
- A dedicated performance-profiling tool integration
- Ollama testing/integration (deferred to post-completion)
- Generated diff/patch output (vs. recommendation text)
