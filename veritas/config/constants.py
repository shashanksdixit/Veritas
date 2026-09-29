"""Static defaults and version strings for Veritas."""

from __future__ import annotations

APP_NAME = "veritas"
VERSION = "0.1.0"

# Report output schema version (FR-016). Bump (and update CHANGELOG.md) on any
# breaking change to Report/CodeFinding/RequirementFinding shape.
SCHEMA_VERSION = "1.1.0"

# Prompt set version, recorded in ReviewRun.prompt_version (FR-024). Prompt
# files in review/prompts/ carry a `prompt_version:` header; this constant is
# the fallback when the header is missing.
PROMPT_VERSION = "1.0.0"

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

# Files searched as requirement sources (FR-008, spec-kit structured first).
REQUIREMENTS_SOURCES = (
    "spec.md",
    "spec-kit.md",
    "requirements.md",
    "REQUIREMENTS.md",
    "PRD.md",
    "docs/requirements.md",
    "docs/prd.md",
    "README.md",
)

# Natural-language review rules file (FR-011).
NATURAL_LANGUAGE_RULES_GLOB = ".veritas/rules.md"

DEFAULT_REPORT_OUTPUT = "./veritas-report-{timestamp}.md"