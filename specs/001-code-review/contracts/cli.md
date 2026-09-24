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
| `VERITAS_ZDR` | Enable ZDR routing ("true"/"false") | --config value |
| `VERITAS_GITHUB_TOKEN` | GitHub API token (PR mode) | --config value |
| `VERITAS_GITLAB_TOKEN` | GitLab API token (PR mode) | --config value |
| `VERITAS_GITLAB_URL` | GitLab instance URL | --config value (default: https://gitlab.com) |

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Review completed (findings or not) |
| 1 | Fatal error (missing args, bad config, target not found, provider unreachable, or --post used with a non-pr scope) |
| 2 | Partial review (LLM failed mid-run; report written with completed types, marked incomplete per FR-027) |

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
[warn] OpenGrep not found on PATH; security findings will be LLM-identified only
[error] LLM provider rate-limited after 3/5 review types; report marked incomplete
[warn] Failed to post report as PR comment: 403 Forbidden; report saved to ./veritas-report-20260916-143022.md
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
zdr = false                                  # default: off

[hosting]
provider = "github"                          # "github" or "gitlab"
# GitHub: set VERITAS_GITHUB_TOKEN env var (never in file)
# GitLab:
# gitlab_url = "https://gitlab.com"          # default
# Set VERITAS_GITLAB_TOKEN env var (never in file)

[report]
output_dir = "."                             # default: current directory
```

**Privacy rule (constitution):** API keys and tokens MUST be supplied via environment variables or a non-committed config file. The default config file `.veritas/config.toml` MUST NOT contain keys/tokens. A `.veritas/config.local.toml` (gitignored) is allowed for developer-local overrides.
