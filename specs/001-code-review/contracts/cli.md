# CLI Contract: Veritas

**Date**: 2026-09-16

## Command Structure

```text
veritas <command> [options]

Commands:
  review      Run a code review (primary command)
  suppress    Suppress a finding by ID or filename+line
  unsuppress  Remove a suppression entry
```

## `veritas review`

```text
veritas review [OPTIONS]

Options:
  --scope [project|module|file|pr]  (required) Review scope (module = Module/Package)
  --target TEXT                      (required) Target: directory path, file path, or PR ref (e.g. "owner/repo#123")
  --config PATH                     Config file path (default: .veritas/config.toml)
  --output PATH                     Report file path (default: ./veritas-report-<timestamp>.md)
  --verbose                         Enable verbose logging to stderr
  --post                            Post the report as a PR comment / MR note (opt-in, --scope pr only; rejected with other scopes)
  --help                            Show help message
```

### Environment Variables

| Variable | Description | Overrides |
|----------|-------------|-----------|
| `VERITAS_API_KEY` | LLM provider API key | --config value |
| `VERITAS_BASE_URL` | LLM endpoint base URL (default: `https://api.openai.com/v1`) | --config value |
| `VERITAS_MODEL` | Model override (default: `openai:gpt-4o-mini`) | --config value |
| `VERITAS_ZDR` | Enable ZDR routing ("true"/"false"); OpenRouter only — "true" on any other backend is a fatal error | --config value |
| `VERITAS_TIMEOUT_SECONDS` | Per-request LLM timeout in seconds (default: 120, min 1) | --config value |
| `VERITAS_MAX_RETRIES` | Retry limit per LLM request (default: 2, range 0-10) | --config value |
| `VERITAS_MAX_CONCURRENCY` | Maximum concurrent LLM requests (default: 4, range 1-16) | --config value |
| `VERITAS_MAX_OUTPUT_TOKENS` | Maximum output tokens per LLM request (default: 8192, min 256, max 65536) | --config value |
| `VERITAS_OPENGREP_RULES` | SAST rules source: a registry ruleset name (`p/...` or `r/...`) or an existing local rules file/directory (default: `p/owasp-top-ten`). Registry rulesets carry the Semgrep Rules License, not OpenGrep's LGPL-2.1; using one is the user's licensing choice, and local rules are the alternative | --config value |
| `VERITAS_OPENGREP_JOBS` | OpenGrep parallel job count, passed as `-j` (default: 4, min 1) | --config value |
| `VERITAS_GITHUB_TOKEN` | GitHub API token (PR mode) | --config value |
| `VERITAS_GITLAB_TOKEN` | GitLab API token (PR mode) | --config value |
| `VERITAS_GITLAB_URL` | GitLab instance URL | --config value (default: https://gitlab.com) |

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Review completed (findings or not) |
| 1 | Fatal error (missing args, bad config, target not found, the LLM client failed to initialize (for example, no API key), PR fetch failed, `VERITAS_ZDR=true` on a non-OpenRouter backend, `--post` used with a non-pr scope, or a missing hosting token for `--scope pr`: `VERITAS_GITHUB_TOKEN` / `VERITAS_GITLAB_TOKEN`, checked after the LLM client is built and before any review work starts) |
| 2 | Partial review (an LLM call failed, for example a rejected API key or an unreachable provider; the report is still written and marked incomplete per FR-027) |

On exit code 1 for bad configuration (a pydantic validation error or a
settings-loading `ValueError`), the CLI prints one
`[error] invalid configuration: {field}: {message}` line per problem to stderr
(for a `ValueError`, `[error] invalid configuration: {message}`) — never a
traceback — and exits before any review work starts.

### stdout

Compact summary only (FR-016):
```text
Veritas Review: 42 findings (1 error, 17 warning, 24 info)
Code Quality: 30 | Security: 2 | Test Coverage: 6 | Performance: 4
Coverage: 18 reviewed (1 split), 2 excluded, 0 unsupported, 0 not reviewed; 3/8 batches
SAST: ran (1 result(s))
Context: detected (pyproject.toml, AGENTS.md)
Security sources: 1 llm-verified | 1 sast
Requirements: 5 satisfied, 2 partial, 3 gap, 0 unclear, 0 not addressed
Verdict: RequiresModification

Headline findings (errors):
  - src/app/db.py:42: SQL query built from request input
Report: ./veritas-report-20260916-143022.md
```

A line appears only when it applies: the category line names only categories
with findings, `Security sources` only when there are security findings,
`Headline findings` only for unsuppressed error findings (at most 10, then
`+N more, see full report`), and `Verification failures: ...` and
`Report status: incomplete — ...` only when they occur.
On an incomplete report the verdict line reads `Verdict: <verdict> (incomplete review: covers only what was reviewed)`.

### stderr

Diagnostics and errors only:
```text
[warn] ZDR is OFF: zero data retention cannot be enforced per request on this backend; data retention is governed by your account agreement with openai. To enforce ZDR per request, use OpenRouter with VERITAS_ZDR=true.
[info] Scanning target in scope: acme/widget#3
[info] Fetching PR acme/widget#3 from github
[warn] OpenGrep not found on PATH
[info] LLM call gpt-4o-mini (1234 ms) {"completion_tokens": 30, "latency_ms": 1234.4, "model": "gpt-4o-mini", "prompt_tokens": 120, "total_tokens": 150}
[warn] Security coverage degraded: OpenGrep not found on PATH; LLM-identified findings remain subject to re-verification.
[error] LLM call gpt-4o-mini (812 ms) failed: openai 429: Rate limit reached for gpt-4o-mini [rate_limit_exceeded] {"completion_tokens": null, "latency_ms": 812.3, "model": "gpt-4o-mini", "prompt_tokens": null, "total_tokens": null}
[warn] performance: batch 2/3 failed (files: src/b.py): openai 429: Rate limit reached for gpt-4o-mini [rate_limit_exceeded]
[warn] Failed to post report as github comment: 403 Forbidden; report saved to ./veritas-report-20260916-143022.md
```

An excerpt from a `veritas review --scope pr --target acme/widget#3 --post` run
with OpenGrep missing, one failed LLM batch and a refused post; a real run writes
more `[info]` lines. A failed LLM call writes an `[error] LLM call ... failed: ...`
line, and a failed batch then writes a `[warn] <review type>: batch <i>/<n> failed
(files: ...): <reason>` line; the run continues and the report is marked
incomplete (exit code 2, FR-027). A failed `--post` is a warning and leaves the
exit code unchanged (FR-028).

Every LLM call is logged as one such line in every run; a token count the provider does not report is null. --verbose adds the same kind of JSON object to the other lines that carry fields.

Exactly one data-retention warning is printed per run, before any review work
starts, and is worded for the configured backend: on the default backend (OpenAI
direct) and any other non-OpenRouter backend (including the Anthropic route,
whatever its base_url), the account-agreement warning shown above, naming the
provider (`...your account agreement with {provider}...`); it never advises
`VERITAS_ZDR=true` on that backend, which the run refuses (below), only using
OpenRouter with it. On OpenRouter, `[warn] ZDR is OFF: some free-tier models
reserve the right to train on inputs/outputs. Set VERITAS_ZDR=true for reviews
of proprietary or sensitive code.`

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
base_url = "https://api.openai.com/v1"      # default (OpenAI direct)
model = "openai:gpt-4o-mini"                 # default; fixed, never chosen at runtime
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

[security]
opengrep_rules = "p/owasp-top-ten"           # default: SAST rules source (FR-012).
                                             #  A registry ruleset name starting with
                                             #  "p/" or "r/", or an existing local rules
                                             #  file or directory - a local source makes
                                             #  SAST reproducible. Registry rulesets are
                                             #  not covered by OpenGrep's LGPL-2.1
                                             #  licence; they carry the Semgrep Rules
                                             #  License, and using one is the user's
                                             #  licensing choice. A local rules file or
                                             #  directory is the alternative. The value
                                             #  is recorded in the report as
                                             #  ReviewRun.sast_rules.
                                             #  Anything else is rejected as an invalid
                                             #  configuration error before the run starts.
opengrep_jobs = 4                            # default: OpenGrep parallel jobs ("-j", FR-012),
                                             #  at least 1; a lower value is rejected as an
                                             #  invalid configuration error. The scan's
                                             #  time limit stays 120 s.
```

**Privacy rule (constitution, FR-020):** API keys and tokens (`api_key`, `github_token`, `gitlab_token`) MUST be supplied via `VERITAS_*` environment variables, `.veritas/config.local.toml` (gitignored), or a file passed with `--config` other than `.veritas/config.toml`. The committed default config file `.veritas/config.toml` MUST NOT contain them; if it does, whether it is read by default or named with `--config` under any spelling (compared by resolved path), `veritas review` exits 1 with `[error] invalid configuration: {fields} must not be set in .veritas/config.toml, which is committed; put it in .veritas/config.local.toml or the environment variable {VERITAS_FIELDS} instead`.
