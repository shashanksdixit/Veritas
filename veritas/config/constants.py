"""Static defaults and version strings for Veritas."""

from __future__ import annotations

APP_NAME = "veritas"
VERSION = "0.1.0"

# Report output schema version (FR-016). Bump (and update CHANGELOG.md) on any
# breaking change to Report/CodeFinding/RequirementFinding shape.
SCHEMA_VERSION = "1.9.0"

# Prompt set version, recorded in ReviewRun.prompt_version (FR-024). Prompt
# files in review/prompts/ carry a `prompt_version:` header; this constant is
# the fallback when the header is missing.
PROMPT_VERSION = "1.9.0"

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai:openai/gpt-4o-mini"

CONFIG_PATH = ".veritas/config.toml"
LOCAL_CONFIG_PATH = ".veritas/config.local.toml"
SUPPRESSIONS_PATH = ".veritas/suppressions.json"
LAST_REPORT_JSON = ".veritas/last-report.json"

REPORT_PATTERN = "veritas-report-{timestamp}.md"

SUPPORTED_LANGUAGES = ("python", "java", "javascript", "csharp", "go", "rust")

# Max files collected into a review scope (safety valve for very large trees).
MAX_SCOPE_FILES = 500

# Files searched as project conventions documentation (FR-010).
CONVENTIONS_FILENAMES = ("AGENTS.md", "CONVENTIONS.md", "STYLE.md")

# Requirement sources (FR-008), in discovery priority order. Lower rank wins; a
# spec-kit feature spec is ahead of all of these and shares one rank, since which
# feature a spec describes does not make it more authoritative than another.
# Requires a companion entry in veritas/review/requirements_source.py, which
# owns the patterns; this is the order and the root/docs file names only.
REQUIREMENTS_ROOT_SOURCES = (
    "requirements.md",
    "REQUIREMENTS.md",
    "PRD.md",
    "docs/requirements.md",
    "docs/prd.md",
    "spec.md",
    "spec-kit.md",
    "README.md",
)
REQUIREMENTS_SPEC_GLOB_PARTS = ("specs", "*", "spec.md")

# Max characters of a requirement's text carried into a review prompt (FR-008);
# a longer FR line is cut here and marked with a trailing "...".
REQUIREMENTS_MAX_TEXT_CHARS = 800

# Backwards-compatible alias for the old fixed-name tuple. The discovery rules now
# live in veritas/review/requirements_source.py, which also recognises
# specs/<feature>/spec.md; this name is the root/docs subset only.
REQUIREMENTS_SOURCES = REQUIREMENTS_ROOT_SOURCES

# Natural-language review rules file (FR-011).
NATURAL_LANGUAGE_RULES_GLOB = ".veritas/rules.md"

DEFAULT_REPORT_OUTPUT = "./veritas-report-{timestamp}.md"