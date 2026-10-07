# CLI Contract: Veritas

**Date**: 2026-09-16

## Command Structure

```text
veritas <command> [options]

Commands:
  review      Run a code review (primary command)
  suppress    Suppress a finding by ID or filename+line
  unsuppress Remove a suppression entry by ID or fingerprint
```

## `veritas review`

```text
veritas review [OPTIONS]

Options:
  --scope [project|module|file|pr]  (required) Review scope (module = Module/Package)
  --target TEXT                      (required) Target: directory path, file path, or PR ref (e.g. "owner/repo#123")
  --config PATH                     Config file path (default: .veritas/config.toml)
  --output PATH                     Report file path (default: ./veritas-report-<timestamp>.md)
  --verbose / --no-verbose          Enable verbose logging to stderr
  --post                            Post the report as a PR comment / MR note (opt-in, --scope pr only; rejected with other scopes)
  --help                            Show help message
```

### Environment Variables

| Variable | Description | Overrides |
|----------|-------------|-----------|
| `VERITAS_API_KEY` | LLM provider API key | --config value |
| `VERITAS_BASE_URL` | LLM endpoint base URL | --config value |
| `VERITAS_MODEL` | Model override | --config value |
| `VERITAS_ZDR` | Enable ZDR routing ("true"/"false"); OpenRouter only — "true" on any other backend is a fatal error | --config value |
| `VERITAS_TIMEOUT_SECONDS` | Per-request LLM timeout in seconds (default: 120, min 1) | --config value |
| `VERITAS_MAX_RETRIES` | Retry limit per LLM request (default: 2, range 0-10) | --config value |
| `VERITAS_MAX_CONCURRENCY` | Maximum concurrent LLM requests (default: 4, range 1-16) | --config value |
| `VERITAS_GITHUB_TOKEN` | GitHub API token (PR mode) | --config value |
| `VERITAS_GITLAB_TOKEN` | GitLab API token (PR mode) | --config value |
| `VERITAS_GITLAB_URL` | GitLab instance URL | --config value (default: https://gitlab.com) |

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Review completed (findings or not) |
| 1 | Fatal error (missing args, bad config, target not found, provider unreachable, `VERITAS_ZDR=true` on a non-OpenRouter backend, or --post used with a non-pr scope) |
| 2 | Partial review (LLM failed mid-run; report written with completed types, marked incomplete per FR-027) |

On exit code 1 for bad configuration (a pydantic validation error or a
settings-loading `ValueError`), the CLI prints one
`[error] invalid configuration: {field}: {message}` line per problem to stderr
(for a `ValueError`, `[error] invalid configuration: {message}`) — never a
traceback — and exits before any review work starts.

### stdout

Compact summary only (FR-016):
```text
Veritas Review: 42 findings (3 error, 15 warning, 24 info)
Security: 2 (1 sast, 1 llm-verified) | Requirements: 3 gaps, 2 partial, 5 satisfied
Report: ./veritas-report-20260916-143022.md
```

### stderr

Diagnostics and errors only:
```text
[info] Scanning project in scope: ./myproject
[warn] ZDR is OFF: some free-tier models reserve the right to train on inputs/outputs. Set VERITAS_ZDR=true for reviews of proprietary or sensitive code.
[warn] OpenGrep not found on PATH; security findings will be LLM-identified only
[error] LLM provider rate-limited after 3/5 review types; report marked incomplete
[warn] Failed to post report as PR comment: 403 Forbidden; report saved to ./veritas-report-20260916-143022.md
```

Exactly one data-retention warning is printed per run, before any review work
starts, and is worded for the configured backend: on OpenRouter the free-tier
training warning shown above; on any other backend,
`[warn] ZDR is OFF: data retention for this backend is governed by your account
agreement with {provider}...`.

With `VERITAS_ZDR=true` on a non-OpenRouter backend the run stops immediately with
exit code 1 and no report — nothing is fetched, read, or sent:
```text
[error] ZDR is only supported with OpenRouter. The configured backend is openai at https://api.openai.com/v1; zero data retention there depends on your account agreement with that provider and cannot be enforced per request. Nothing was sent. Set zdr = false (VERITAS_ZDR=false) if your account already has zero data retention, or point base_url at https://openrouter.ai/api/v1.
```

## `veritas suppress`

```text
veritas suppress [OPTIONS]

Options:
  --finding-id TEXT    Stable finding ID from report
  --file TEXT          Filename (alternative to finding-id)
  --line INTEGER       Line number (used with --file to resolve finding(s))
  --reason TEXT        Reason for suppression
  --help               Show help
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Suppression added successfully |
| 1 | Finding not matched, or multiple matches (ambiguous) — diagnostic on stderr |

On success (exit 0), stdout prints the suppression's fingerprint, e.g.:
```text
Suppressed. Fingerprint: a3f9c2...  (use with 'veritas unsuppress --fingerprint' to reverse)
```
This is the mechanism for obtaining a fingerprint to pass to `veritas unsuppress` later — the fingerprint is also stored, and remains readable, in `.veritas/suppressions.json` itself.

## `veritas unsuppress`

```text
veritas unsuppress [OPTIONS]

Options:
  --fingerprint TEXT   SHA-256 fingerprint of suppression entry (from .veritas/suppressions.json)
  --help               Show help
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Suppression removed |
| 1 | No matching suppression entry found |

## Config File (`.veritas/config.toml`)

```toml
[llm]
base_url = "https://openrouter.ai/api/v1"   # default
model = "openai/gpt-4o-mini"                 # default (or free model discovered at runtime)
zdr = false                                  # default: off; true = OpenRouter only (FR-021)
timeout_seconds = 120                        # default: per-request timeout, >= 1 (FR-019)
max_retries = 2                              # default: retry limit per request, 0-10 (FR-019)
max_concurrency = 4                          # default: max concurrent LLM requests, 1-16 (FR-019)

[hosting]
provider = "github"                          # "github" or "gitlab"
# GitHub: set VERITAS_GITHUB_TOKEN env var (never in file)
# GitLab:
# gitlab_url = "https://gitlab.com"          # default
# Set VERITAS_GITLAB_TOKEN env var (never in file)

[review]
exclude = [".specify/"]                       # default; path exclusion patterns (FR-029)
                                             #  a pattern ending in "/" matches every
                                             #  path under that directory prefix; any
                                             #  other pattern is fnmatch-ed against the
                                             #  full relative path. A user-supplied
                                             #  list replaces this default; an empty
                                             #  list disables exclusion.
batch_chars = 48000                          # default: max line-numbered chars per batch (FR-029)
max_batches = 8                              # default: max batches per code review type (FR-029)
```

**Privacy rule (constitution):** API keys and tokens MUST be supplied via environment variables or a non-committed config file. The default config file `.veritas/config.toml` MUST NOT contain keys/tokens. A `.veritas/config.local.toml` (gitignored) is allowed for developer-local overrides.
