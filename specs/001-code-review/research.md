# Research: Veritas Implementation (001-code-review)

**Date**: 2026-09-16
**Branch**: 001-code-review

## 1. SAST Integration — OpenGrep

### Decision
Invoke OpenGrep via `subprocess.run()` calling the standalone binary CLI (`opengrep scan --config <rules> --json -o <file> <target>`). Parse the `--json` output directly (semgrep-compatible `cli_output` schema). Pin to v1.30.0 (latest stable as of 2026-09-07).

### Rationale
- OpenGrep provides **no Python API / importable library**. Maintainers explicitly state: *"opengrep will not be submitted to pypi because… it does not provide a python API and the tool is mostly not written in python. If you depend on such a tool, you list it as a script dependency and probe for it at runtime."* (opengrep/opengrep#20)
- `--json` output is the most convenient structured surface: `results[]` entries contain `check_id`, `path`, `start.line`/`end.line`, `extra.lines` (matched code snippet), `extra.severity`, `extra.metadata.cwe`, `extra.metadata.owasp`. This maps directly to `CodeFinding` fields with `source="sast"`.
- SARIF output carries CWE/OWASP only as tags on the rule descriptor, not per-result — less convenient than `--json` for populating per-finding taxonomy ids.
- The standalone binary (Nuitka-built, ~41 MB exe+DLLs on Windows) avoids the absent/typosquatted PyPI situation entirely and has native win32 support. Linux/macOS binaries also available for CI.

### Alternatives considered
| Option | Reason rejected |
|--------|-----------------|
| Semgrep CE (`pip install semgrep`) | OpenGrep exceeds feature set (intrafile taint, Windows maturity, no Pro-gating); same integration pattern |
| SARIF via `--sarif` | CWE/OWASP only on rule descriptor, not per-result — would require an extra rule-lookup join |
| `semgrep-rules` Python registry | Same rule-licensing concerns as Semgrep's own rules |

### OpenGrep output schema (relevant fields per result)

```jsonc
{
  "check_id": "python.langsecurity.audit.dangerous-subprocess-use",
  "path": "src/app.py",
  "start": { "line": 42, "col": 5, "offset": 0 },
  "end":   { "line": 42, "col": 50, "offset": 0 },
  "extra": {
    "lines": "subprocess.call(user_input, shell=True)",
    "message": "User input flows to a subprocess call without sanitization.",
    "severity": "ERROR",
    "metadata": {
      "cwe": ["CWE-78: Improper Neutralization of Special Elements used in an OS Command ('OS Command Injection')"],
      "owasp": ["A03:2021 - Injection"]
    }
  }
}
```

### Rule licensing note
The engine is unambiguously LGPL-2.1 with no paid tier. However, rules pulled from Semgrep's registry carry Semgrep's restrictive Rules License v1.0 (internal use only, no redistribution). The opengrep-rules fork is archived. For the initial release, use Semgrep's default rule packs (`--config auto` or specific packs like `p/owasp-top-ten`) — this is permissible for internal use and review against external code. If redistribution of rules becomes a concern, author custom rules (LGPL-2.1 compatible) under the Veritas project.

---

## 2. OpenRouter LLM Client

### Decision
Use LangChain's `init_chat_model(model_string, **kwargs)` universal factory as the single entry point for all LLM calls, rather than the openai SDK directly. Model selection is a string like `'openai:gpt-4o-mini'` or `'anthropic:claude-sonnet-4-6'`, resolved from config (FR-019/FR-020). Default backend (OpenRouter) uses the openai provider prefix with base_url set to OpenRouter's endpoint, since OpenRouter's API is OpenAI-schema-compatible. When the user configures their own key for a provider whose native API is not OpenAI-compatible (e.g. Anthropic), the corresponding LangChain integration package (langchain-anthropic, etc.) is installed and its provider prefix used instead — no custom per-provider request/response handling is written by Veritas itself. ZDR (provider.zdr / provider.data_collection) remains OpenRouter-specific and only applies when routing through the OpenRouter/openai-compatible path. Note the two distinct string layers: the LangChain provider prefix (`openai:` or `anthropic:`) selects which integration package handles the call; the portion after the colon is whatever model identifier that backend itself expects — for the OpenRouter/openai-compatible path this is OpenRouter's own slash-formatted catalog ID (e.g. `openai/gpt-4o-mini`, or a free variant like `meta-llama/llama-3.1-8b-instruct:free`), matching `contracts/cli.md`'s config and `data-model.md`'s `ReviewRun.model_name`; for the native Anthropic path it is Anthropic's own model name (e.g. `claude-sonnet-4-6`), no slash. These are not competing formats — they're two different layers of the same resolved string.

### Rationale
- OpenRouter's official docs show the OpenAI SDK as the primary integration path: `client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key="...")`.
- Models endpoint requires authentication; pass the same API key.
- Free models are `:free`-suffixed catalog variants with `pricing.prompt == "0"`; they remain active in 2026 with rate limits (50 req/day unauthenticated-equivalent, 1000 req/day with ≥$10 credits).

### Alternatives considered
| Option | Reason rejected |
|--------|-----------------|
| Raw openai SDK only (original plan) | Works for OpenRouter/OpenAI but cannot reach Anthropic's native (non-OpenAI-compatible) API without a hand-written adapter — contradicts FR-020's promise of native Claude support without committing to writing per-provider adapters ourselves. |
| Native `openrouter` SDK | Couples to OpenRouter vs generic OpenAI-compatible; not worth the extra dependency |
| Raw `requests`/`httpx` | The `openai` SDK handles streaming, retries, auth, and typing — no advantage to rolling raw |
| Account-level ZDR toggle | Not per-request; can't gate ZDR per invocation without body-level `provider.zdr` |

---

## 3. Hosting API — GitHub / GitLab PR Fetching

### Decision
Use **plain `httpx` (BSD-3-Clause)** for both GitHub and GitLab REST APIs directly. No third-party SDK.

### Rationale
- Only ~5 REST endpoints needed per provider (list PR files, get file contents, post PR comment). The surface is too small to justify an SDK.
- SDK licensing risk:
  - PyGithub: **LGPL-3.0** (actively seeking maintainers — bus-factor concern)
  - python-gitlab: **LGPL-3.0-or-later**
  - githubkit: **MIT** but GitHub-only (doesn't cover GitLab)
- httpx is BSD-3-Clause with permissive deps (httpcore BSD-3-Clause, h11 MIT). Only `certifi` is MPL-2.0 (weak file-level copyleft, acceptable for use). Avoids all LGPL concerns.
- Uniform auth/retry/pagination layer across both providers.

### REST endpoints (minimal surface)

| Operation | GitHub | GitLab |
|-----------|--------|--------|
| List PR files | `GET /repos/{owner}/{repo}/pulls/{n}/files` | `GET /projects/{id}/merge_requests/{iid}/changes` |
| Get file at ref | `GET /repos/{owner}/{repo}/contents/{path}?ref={sha}` | `GET /projects/{id}/repository/files/{path}?ref={sha}` |
| Post comment | `POST /repos/{owner}/{repo}/issues/{n}/comments` | `POST /projects/{id}/merge_requests/{iid}/notes` |

### Alternatives considered
| Option | Reason rejected |
|--------|-----------------|
| PyGithub (LGPL-3.0) | License risk + sync-only + bus factor |
| python-gitlab (LGPL-3.0) | License risk |
| githubkit (MIT) + httpx for GitLab | Splits codebase into two idioms; only worthwhile if typed GitHub models add significant value — they don't for this surface |
| requests (Apache-2.0) | httpx is already a dependency for GitHub REST; cleaner session model, type-annotated |

---

## 4. LangGraph Architecture — Read-Only Review Boundary

### Decision
Build a `StateGraph` with separate `ToolNode` instances for read-only review tools vs. post-review write tools. Review agent nodes call `model.bind_tools(read_only_tools)` and route to a `ToolNode(read_only_tools)`. Write tools (report file, suppression file) live in a separate `ToolNode(write_tools)` reached only after the review+verify path completes — never reachable from within review nodes. No back-edges from write to review.

### Rationale
- LangGraph's `ToolNode` takes an explicit tool list at construction: `ToolNode(tools=[read_file, list_dir, ...])`. If the model calls a tool not in that list, it simply won't execute. This is the structural/architectural boundary the constitution requires.
- Multiple `ToolNode` instances with different tool lists = clean separation. No "permission flag" — the tools are just not in the dict.
- Review nodes → read ToolNode loop; verification node → plain function (or read ToolNode); post-review → write ToolNode → END. No cycle.
- State: `TypedDict` with `Annotated[list, add_messages]` for message channels, plus structured `findings`, `scope`, `verification_notes` fields.

### Graph structure

```
START → scope_resolve (plain node, no tools)
         → review_loop (agent node + read_tool_node, conditional edges)
         → verify (plain function node, reads findings from state)
         → [if pass] → write (write_tool_node: report_file, suppression_file)
         → END
         → [if fail] → END
```

### Alternatives considered
| Option | Reason rejected |
|--------|-----------------|
| Single ToolNode with runtime permission checks | Violates constitution (must be architectural, not runtime flag) |
| Subgraph per review type | Premature complexity; can refactor later if needed |
| Multi-agent (supervisor) pattern | Unnecessary for sequential review pipeline; adds orchestration overhead |

---

## 5. CLI Parsing Library

### Decision
Use **Typer (MIT)** for the CLI layer — resolve and record the actual installed version in the lockfile at install time; do not hardcode an unverified version number here.

### Rationale
- Type-hint-first API: function signatures *are* the CLI schema. Cleaner than Click's decorator stacking for `--scope`, `--config`, `--verbose`, `--output`.
- Rich-formatted `--help` out of the box (via `rich` dependency).
- Native `envvar=` parameter for env-var support on every option.
- Actively maintained; MIT license.
- Typer vendored Click since 0.26.0 — no Click dependency conflict.

### License note
Typer: MIT. Click (vendored internally): BSD-3-Clause. Rich: MIT. All permissive. No license conflict.

### Alternatives considered
| Option | Reason rejected |
|--------|-----------------|
| Click 8.5 standalone (BSD-3-Clause) | Viable but more verbose API, plain `--help` by default, no type-hint integration |
| argparse | No auto-help formatting, no env-var support, painful subcommand handling |

---

## 6. Key Dependency Licenses Summary

| Dependency | License | Source |
|-----------|---------|--------|
| LangGraph | MIT | pypi.org/project/langgraph |
| LangChain (langchain-core) | MIT | pypi.org/project/langchain-core |
| langchain-openai | MIT | pypi.org/project/langchain-openai |
| langchain-anthropic | MIT | pypi.org/project/langchain-anthropic |
| openai SDK (transitive, via langchain-openai) | Apache-2.0 | pypi.org/project/openai |
| httpx | BSD-3-Clause | pypi.org/project/httpx |
| httpcore | BSD-3-Clause | pypi.org/project/httpcore |
| Typer | MIT | pypi.org/project/typer |
| Rich (Typer dep) | MIT | pypi.org/project/rich |
| Click (vendored in Typer) | BSD-3-Clause | pypi.org/project/click |
| pytest | MIT | pypi.org/project/pytest |
| certifi (httpx dep) | MPL-2.0 | pypi.org/project/certifi |
| OpenGrep binary | LGPL-2.1 (engine) | github.com/opengrep/opengrep |

No GPL-family licenses in the Python dependency tree (MPL-2.0 on certifi is file-level copyleft, generally acceptable for linking/use).
