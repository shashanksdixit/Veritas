"""Every place that names a registry ruleset states the rules licence (T128).

Constitution v7.0.0 Principle II: registry rulesets such as p/owasp-top-ten
carry the Semgrep Rules License, not OpenGrep's LGPL-2.1; the documentation
must say so, and name the local rules alternative, wherever it names one.
"""

from __future__ import annotations

from pathlib import Path

import veritas.security.opengrep as opengrep
from veritas.config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("Semgrep Rules License", "local rules")


def test_every_ruleset_mention_states_the_rules_licence():
    texts = {
        "README.md": (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        "contracts/cli.md": (
            REPO_ROOT / "specs" / "001-code-review" / "contracts" / "cli.md"
        ).read_text(encoding="utf-8"),
        "spec.md": (REPO_ROOT / "specs" / "001-code-review" / "spec.md").read_text(encoding="utf-8"),
        "Settings.opengrep_rules description": Settings.model_fields["opengrep_rules"].description or "",
        "veritas.security.opengrep docstring": opengrep.__doc__ or "",
    }
    missing = [
        f"{place}: {phrase!r}" for place, text in texts.items() for phrase in REQUIRED if phrase not in text
    ]
    assert missing == []
