"""Project-context golden cases (T116).

Each builder lays out one project under ``root`` and returns the scope and the
target a review would be given. The goldens in
``tests/integration/data/project_context_golden.json`` were captured from the
context node at the commit before T116 using these same builders, so the golden
tests prove the refactor left what the model receives byte-for-byte unchanged.
"""

from __future__ import annotations

from pathlib import Path

from veritas.models.entities import ReviewScope

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def case_b(root: Path) -> tuple[ReviewScope, str]:
    """pyproject.toml (requires-python and dependencies), AGENTS.md, rules."""
    _write(
        root / "pyproject.toml",
        "[project]\n"
        'name = "demo"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["pydantic>=2.0", "typer", "httpx>=0.27,<1"]\n',
    )
    _write(root / "AGENTS.md", "Prefer early returns.\nNo print() in library code.\n")
    _write(root / ".veritas" / "rules.md", "Flag any eval() usage.\n")
    _write(root / "src" / "app.py", "x = 1\n")
    return ReviewScope.PROJECT, str(root)


def case_c(root: Path) -> tuple[ReviewScope, str]:
    """package.json and go.mod, no conventions or rules."""
    _write(root / "package.json", '{"name": "demo", "engines": {"node": ">=20"}}\n')
    _write(root / "go.mod", "module example.com/demo\n\ngo 1.22\n")
    _write(root / "main.go", "package main\n")
    return ReviewScope.PROJECT, str(root)


def case_d(root: Path) -> tuple[ReviewScope, str]:
    """One .csproj and a conventions file over 4,000 characters."""
    _write(
        root / "App.csproj",
        "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework>"
        "</PropertyGroup></Project>\n",
    )
    _write(
        root / "AGENTS.md",
        "".join(f"Rule {i:04d}: keep functions short.\n" for i in range(200)),
    )
    _write(root / "Program.cs", "class P {}\n")
    return ReviewScope.PROJECT, str(root)


def case_e(root: Path) -> tuple[ReviewScope, str]:
    """A package.json over 200,000 bytes next to a small go.mod."""
    padding = "x" * 210_000
    _write(
        root / "package.json",
        '{"engines": {"node": ">=18"}, "description": "' + padding + '"}\n',
    )
    _write(root / "go.mod", "module example.com/big\n\ngo 1.21\n")
    _write(root / "main.go", "package main\n")
    return ReviewScope.PROJECT, str(root)


def case_f(root: Path) -> tuple[ReviewScope, str]:
    """--scope module with a target below the root that holds the manifests."""
    case_b(root)
    _write(root / "src" / "pkg" / "mod.py", "y = 2\n")
    return ReviewScope.MODULE, str(root / "src")


PR_TARGET = "https://github.com/owner/repo/pull/1"

TMP_CASES = {
    "b": case_b,
    "c": case_c,
    "d": case_d,
    "e": case_e,
    "f": case_f,
}
