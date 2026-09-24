# Veritas

CLI-based, multi-agent code review tool. Veritas reviews a pull request, project,
module, or single file for code quality, security/OWASP, requirement fulfillment,
test-coverage judgment, and performance reasoning — grounding every finding in a
real, verified source location and the project's own context before emitting a
GitHub-Flavored Markdown report that can be posted directly as a PR comment.

## Features

- **Four scopes, per run** — `project`, `module`, `file`, or `pr` (fetched
  remotely via the GitHub/GitLab API; no local git state read).
- **Five mandatory review types** — code quality, security/OWASP, requirements,
  test-coverage judgment (tests are never executed), and performance reasoning.
  Every finding carries its own concrete recommendation text.
- **Grounded and verified** — SAST (OpenGrep) output is ground truth for security
  findings; every LLM-identified finding is re-verified against its cited
  file/line before it can appear. Unverifiable findings are excluded and recorded
  as verification-failure notes (never dropped silently).
- **Project-aware** — language/framework versions parsed from real manifests,
  project conventions honored (AGENTS.md-equivalent), and natural-language custom
  rules supported.
- **Suppression allowlist** — a git-tracked `.veritas/suppressions.json` keyed by
  a fingerprint of the flagged code, so unrelated edits keep a finding suppressed
  while a real change in the flagged snippet surfaces it again.
- **Privacy control** — opt-in ZDR (zero-data-retention) routing for OpenRouter,
  and a shared redaction utility that keeps secrets out of reports, logs, and
  error messages.

## Installation

Requires Python 3.12+.

```bash
pip install -e .
```

Optional native-Anthropic (Claude) support is an extra that installs
`langchain-anthropic`; it is never installed automatically at runtime:

```bash
pip install -e ".[anthropic]"
```

Development/test extras:

```bash
pip install -e ".[test]"
```

**SAST (optional but recommended):** Veritas integrates OpenGrep (LGPL-2.1, no
paid tier) for grounded security findings. Install the standalone `opengrep`
binary on `PATH` (see github.com/opengrep/opengrep/releases). Without it, security
coverage degrades gracefully with a specific reason printed to stderr.

## Quick start

```bash
# One-off review of a whole project
veritas review --scope project --target ./myproject

# A single module directory or file
veritas review --scope module --target ./myproject/src/auth
veritas review --scope file --target ./myproject/src/auth.py

# Review a remote PR (GitHub), then optionally post the report as a comment
export VERITAS_GITHUB_TOKEN=ghp_...
veritas review --scope pr --target "myorg/myrepo#42"
veritas review --scope pr --target "myorg/myrepo#42" --post

# Review a remote MR (GitLab)
export VERITAS_GITLAB_TOKEN=glpat-...
veritas review --scope pr --target "group/project!7"
```

## CLI reference

```text
veritas <command> [options]

Commands:
  review      Run a code review (primary command)
  suppress    Suppress a finding by ID or filename+line
  unsuppress  Remove a suppression entry by fingerprint
```

### `veritas review`

```text
veritas review [OPTIONS]

Options:
  --scope [project|module|file|pr]  (required) Review scope
  --target TEXT                      (required) Directory path, file path, or PR ref
                                    e.g. "owner/repo#123" (GitHub) or "group/project!9" (GitLab)
  --config PATH                     Config file path (default: .veritas/config.toml)
  --output PATH                     Report file path (default: ./veritas-report-<timestamp>.md)
  --verbose / --no-verbose          Enable verbose logging to stderr
  --post                            Post the report as a PR comment / MR note (--scope pr only)
  --help                            Show help message
```

PR `--target` references:

| Pattern | Parsed as |
|---------|-----------|
| `owner/repo#123` | GitHub PR #123 |
| `https://github.com/owner/repo/pull/123` | GitHub PR #123 |
| `group/project!123` | GitLab MR #123 |
| `https://gitlab.com/owner/repo/-/merge_requests/123` | GitLab MR #123 |

A bare number (e.g. `123`) is rejected with exit code 1: without a config-level
default project or local git state, it is always ambiguous. The full reference is
required.

### `veritas suppress`

```text
veritas suppress [OPTIONS]

Options:
  --finding-id TEXT   Stable finding ID from the report
  --file TEXT         Filename (alternative to --finding-id)
  --line INTEGER      Line number (used with --file)
  --reason TEXT       Reason for suppression
```

On success the computed fingerprint is printed to stdout:

```text
Suppressed. Fingerprint: a3f9c2...  (use with 'veritas unsuppress --fingerprint' to reverse)
```

### `veritas unsuppress`

```text
veritas unsuppress [OPTIONS]

Options:
  --fingerprint TEXT   SHA-256 fingerprint of the suppression entry
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Review completed (findings or not) |
| 1 | Fatal error (missing args, bad config, target not found, provider unreachable, PR fetch failed, or `--post` used with a non-pr scope) |
| 2 | Partial review (LLM failed mid-run; report written with completed types and marked incomplete) |

### stdout / stderr

stdout carries results only (compact summary). stderr carries diagnostics and
errors:

```text
[info] Scanning target in scope: ./myproject
[warn] OpenGrep not found on PATH; security findings will be LLM-identified only
[error] Failed to post report as PR comment: 403 Forbidden; report saved to ./veritas-report-20260916-143022.md
```

## Configuration

Configuration layers, lowest to highest precedence:

1. Built-in defaults
2. `.veritas/config.toml` (committed, must not contain secrets)
3. `.veritas/config.local.toml` (gitignored, local overrides)
4. Environment variables `VERITAS_*`

```toml
# .veritas/config.toml
[llm]
base_url = "https://openrouter.ai/api/v1"   # default
model = "openai/gpt-4o-mini"                 # default
zdr = false                                  # default: off

[hosting]
provider = "github"                          # "github" or "gitlab"

[report]
output_dir = "."                             # default
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `VERITAS_API_KEY` | LLM provider API key |
| `VERITAS_BASE_URL` | LLM endpoint base URL (default: OpenRouter) |
| `VERITAS_MODEL` | Model override (e.g. `openai:vendor/model` or `anthropic:claude-model`) |
| `VERITAS_ZDR` | Zero-data-retention routing (`"true"`/`"false"`) |
| `VERITAS_GITHUB_TOKEN` | GitHub API token (PR mode) |
| `VERITAS_GITLAB_TOKEN` | GitLab API token (PR mode) |
| `VERITAS_GITLAB_URL` | GitLab instance URL (default: https://gitlab.com) |

**Secrets rule:** API keys and tokens are provided only via environment variables
or the gitignored local config file. The committed config file must never contain
credentials. No secret is logged or echoed into a report — the shared redaction
utility masks common secret shapes (API keys, bearer tokens, connection strings,
PEM blocks) in all user-facing finding text.

### LLM model strings

The model string is `<provider>:<model>`. For the default OpenRouter route the
OpenAI-compatible provider is used and the part after the colon is OpenRouter's
own catalog id (e.g. `openai:openai/gpt-4o-mini`). To use a native Anthropic
(Claude) key, configure `VERITAS_MODEL=anthropic:claude-...` and install the
`[anthropic]` extra. Without the extra, startup fails fast with the exact
install command.

## ZDR and privacy

Zero-data-retention routing is off by default. When enabled
(`VERITAS_ZDR=true`), requests carry both `provider.zdr: true` and
`provider.data_collection: "deny"` for OpenRouter routing.

Whenever ZDR is off (the default), Veritas prints a warning on every run: some
free-tier models explicitly reserve the right to train on inputs/outputs, and ZDR
is recommended for reviews of proprietary or sensitive code. Enabling ZDR is the
user's informed choice.

## Suppression workflow

Suppressions live in `.veritas/suppressions.json` (git-tracked, committed) and
are keyed by a fingerprint of (file, category, normalized flagged snippet), not
by line numbers.

```bash
# Review, note a finding ID from the report
veritas review --scope file --target ./myproject/src/auth.py

# Suppress it
veritas suppress --finding-id <finding-id> --reason "Accepted risk"
# Also possible: veritas suppress --file src/auth.py --line 42

# Re-run: the finding no longer appears. Move an unrelated line: still suppressed.
# Edit the flagged snippet itself: the finding reappears.

# Reverse it
veritas unsuppress --fingerprint <sha256>
```

## Supported languages

Primary: Python, Java. Also supported: JavaScript/TypeScript, C#/.NET, Go, Rust.
Files of any other language are skipped and explicitly noted in the report; the
rest of the scope is reviewed normally.

## Report output

Each run writes a full GitHub-Flavored Markdown report to
`./veritas-report-<timestamp>.md` (or the `--output` path). The report includes:

- provenance (model, prompt version, input revision),
- a summary with severity/category counts, requirement-status counts, and an
  overall verdict (`RequiresModification` / `RequiresReview` / `Clean`),
- verification-failure notes,
- code findings (each labeled with its security source: `(SAST)` or
  `(LLM-verified)` where applicable),
- requirement findings with status and evidence citations.

The report schema is versioned: an HTML comment
`<!-- veritas-report-schema: 1.0.0 -->` is emitted at the top, and breaking
changes are tracked in `CHANGELOG.md`. The machine-readable
`.veritas/last-report.json` sidecar powers `veritas suppress`.

## Development

```bash
pip install -e ".[test]"
pytest
```

Veritas is built on Python 3.12, Typer, Pydantic v2, LangChain/LangGraph (via
`init_chat_model`), and httpx. All runtime Python dependencies are permissive-licensed
(MIT / BSD-3-Clause / Apache-2.0); the OpenGrep SAST engine is the sole
LGPL-2.1 component (no paid tier). Constitution governance details live in
`.specify/memory/constitution.md`.