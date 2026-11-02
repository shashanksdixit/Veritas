"""Every place that names a registry ruleset states the rules licence (T128, T136).

Constitution v8.0.0 Principle II: registry rulesets such as p/owasp-top-ten
carry the Semgrep Rules License, not OpenGrep's LGPL-2.1; using one is the
user's licensing choice, a local rules file or directory is the alternative,
and the documentation must say so wherever it names a registry ruleset.

T136: every tracked Markdown file is checked, not a fixed list, and each place
that names a ruleset (a paragraph, list item or table row) must carry the
statement itself. Historical records are exempt: CHANGELOG.md, and completed
("- [X]") task lines, which record what a past task did.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import veritas.security.opengrep as opengrep
from veritas.config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("Semgrep Rules License", "licensing choice", "local rules")

# A registry ruleset name (p/owasp-top-ten, r/corp-pack) that is not part of a
# path or URL, or OpenGrep's registry auto-selection (--config auto).
RULESET = re.compile(r"(?<![\w./-])[pr]/[a-z][\w.-]*|--config\s+auto\b")
LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+\.)\s")
COMPLETED_TASK = re.compile(r"\s*- \[[xX]\]\s")
HISTORICAL_FILES = {"CHANGELOG.md"}


def _units(text: str) -> list[tuple[int, str]]:
    """Split Markdown into (first line number, text) per paragraph, list item and table row.

    Lines are joined with spaces, and a "#" that continues a code comment onto
    the next line is dropped, so a statement wrapped across lines still matches.
    """
    units: list[tuple[int, list[str]]] = []
    in_fence = previous_stands_alone = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        if not stripped:
            units.append((number, []))
            continue
        # A table row and a heading are each one unit on their own.
        stands_alone = stripped.startswith("|") or (not in_fence and line.startswith("#"))
        starts_unit = stands_alone or previous_stands_alone or LIST_ITEM.match(line)
        if starts_unit or not units or not units[-1][1]:
            units.append((number, [line]))
        else:
            units[-1][1].append(line)
        previous_stands_alone = stands_alone
    joined = []
    for start, lines in units:
        if lines:
            flat = re.sub(r"\s+#\s+", " ", " ".join(lines))
            joined.append((start, re.sub(r"\s+", " ", flat)))
    return joined


def _missing(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    return [phrase for phrase in REQUIRED if phrase not in flat]


def _tracked_markdown() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(path for path in listed.split("\0") if path and path not in HISTORICAL_FILES)


def _offences() -> list[str]:
    offences = []
    for path in _tracked_markdown():
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        for start, unit in _units(text):
            if not RULESET.search(unit) or COMPLETED_TASK.match(unit):
                continue
            missing = _missing(unit)
            if missing:
                offences.append(f"{path}:{start}: missing {missing}")
    return offences


def test_every_ruleset_mention_states_the_rules_licence():
    offences = _offences()
    assert offences == [], "\n".join(offences)


def test_code_documentation_states_the_rules_licence():
    texts = {
        "Settings.opengrep_rules description": Settings.model_fields["opengrep_rules"].description or "",
        "veritas.security.opengrep docstring": opengrep.__doc__ or "",
        "run_opengrep docstring": opengrep.run_opengrep.__doc__ or "",
    }
    missing = {place: _missing(text) for place, text in texts.items() if _missing(text)}
    assert missing == {}


def test_the_scan_covers_the_documentation_that_names_rulesets():
    """Guards against a vacuous pass: the files known to name a ruleset are scanned."""
    tracked = _tracked_markdown()
    for path in ("README.md", "specs/001-code-review/research.md", "specs/001-code-review/plan.md"):
        assert path in tracked
    assert "CHANGELOG.md" not in tracked


def test_ruleset_names_are_detected_and_paths_are_not():
    assert RULESET.search("default `p/owasp-top-ten`")
    assert RULESET.search('opengrep_rules = "r/corp-pack"')
    assert RULESET.search("`--config auto`")
    assert not RULESET.search("github.com/opengrep/opengrep/releases")
    assert not RULESET.search("a `p/...` or `r/...` value")
    assert not RULESET.search("veritas/review/nodes/security.py")


def test_each_paragraph_list_item_and_table_row_must_carry_the_statement():
    statement = "the Semgrep Rules License; using one is your licensing choice, and local rules are the alternative"
    text = "\n".join(
        [
            f"Uses p/owasp-top-ten, {statement}.",
            "",
            "Uses p/owasp-top-ten.",
            f"- r/corp-pack carries {statement}",
            "- r/corp-pack is used",
            "| p/owasp-top-ten | default |",
            f"| p/owasp-top-ten | {statement} |",
            '[security] opengrep_rules = "p/owasp-top-ten"  # carries the Semgrep Rules',
            "                                               #  License; using one is your",
            "                                               #  licensing choice. A local",
            "                                               #  rules file is the alternative.",
            "### Using p/owasp-top-ten",
            f"The heading is its own unit: {statement}.",
        ]
    )
    flagged = [start for start, unit in _units(text) if RULESET.search(unit) and _missing(unit)]
    assert flagged == [3, 5, 6, 12]


def test_completed_task_lines_are_historical():
    assert COMPLETED_TASK.match("- [X] T094 ... default p/owasp-top-ten ...")
    assert not COMPLETED_TASK.match("- [ ] T136 ... p/owasp-top-ten ...")
