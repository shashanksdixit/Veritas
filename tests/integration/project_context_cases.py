"""Project-context golden cases (T116, T118, T119).

Each builder lays out one project under ``root`` and returns the scope and the
target a review would be given. The goldens a to g in
``tests/integration/data/project_context_golden.json`` were captured from the
context node at the commit before T116 using these same builders, so the golden
tests prove the refactor left what the model receives byte-for-byte unchanged.
T118 added one ``[warn]`` line to the logs of d (truncation) and e (file too
large), leaving every ``context`` string as it was, and added cases h to k for
the problems and fixes it introduced.

T119 added cases l to o (``WALK_UP_CASES``) for the walk up to the project
root. Each marks its project root with ``.git``, so the walk stops there. Their
expectations are written by hand in ``test_project_context_golden.py``, not
captured, because they are new behaviour.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import fake_secret
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


# Built at runtime (tests/conftest.py): no secret-shaped literal in the source.
CASE_K_SECRET = fake_secret("ghp_", "k" * 36)


def case_h(root: Path) -> tuple[ReviewScope, str]:
    """A pyproject.toml that is not valid UTF-8 (Latin-1 "é") next to a small go.mod (T118)."""
    (root / "pyproject.toml").write_bytes(
        b'[project]\nname = "caf\xe9"\nrequires-python = ">=3.12"\n'
    )
    _write(root / "go.mod", "module example.com/latin1\n\ngo 1.22\n")
    _write(root / "main.go", "package main\n")
    return ReviewScope.PROJECT, str(root)


def case_i(root: Path) -> tuple[ReviewScope, str]:
    """A pyproject.toml that is valid UTF-8 but not valid TOML, nothing else (T118)."""
    _write(root / "pyproject.toml", '[project\nrequires-python = ">=3.12"\n')
    _write(root / "src" / "app.py", "x = 1\n")
    return ReviewScope.PROJECT, str(root)


def case_j(root: Path) -> tuple[ReviewScope, str]:
    """A whitespace-only AGENTS.md next to a real STYLE.md (T118)."""
    _write(root / "AGENTS.md", "  \n\n\t\n")
    _write(root / "STYLE.md", "Use four-space indentation.\n")
    _write(root / "src" / "app.py", "x = 1\n")
    return ReviewScope.PROJECT, str(root)


def case_k(root: Path) -> tuple[ReviewScope, str]:
    """An AGENTS.md holding a GitHub-token-shaped value (T118): it reaches the model masked."""
    _write(root / "AGENTS.md", f"Deploy with {CASE_K_SECRET} only from CI.\n")
    _write(root / "src" / "app.py", "x = 1\n")
    return ReviewScope.PROJECT, str(root)


def case_l(root: Path) -> tuple[ReviewScope, str]:
    """Case f with a ``.git`` directory at the root (T119): ``--scope module src``
    walks up to the root and finds b's pyproject.toml, AGENTS.md and rules."""
    case_f(root)
    (root / ".git").mkdir()
    return ReviewScope.MODULE, str(root / "src")


def case_m(root: Path) -> tuple[ReviewScope, str]:
    """Case l's layout, reviewing one file two levels below the root (T119)."""
    case_l(root)
    return ReviewScope.FILE, str(root / "src" / "pkg" / "mod.py")


def case_n(root: Path) -> tuple[ReviewScope, str]:
    """Case l plus src/pkg/pyproject.toml and a root package.json (T119): the
    package's own pyproject.toml supplies every manifest, so neither the root's
    Python >=3.12 nor its Node >=20 is mixed in; conventions and rules still
    come from the root."""
    case_l(root)
    _write(
        root / "src" / "pkg" / "pyproject.toml",
        '[project]\nname = "pkg"\nrequires-python = ">=3.13"\n',
    )
    _write(root / "package.json", '{"name": "tooling", "engines": {"node": ">=20"}}\n')
    return ReviewScope.MODULE, str(root / "src" / "pkg")


def case_o(root: Path) -> tuple[ReviewScope, str]:
    """A ``.git`` file at the root, as in a git worktree or submodule (T119):
    src/pkg/go.mod, a blank src/pkg/AGENTS.md passed over for the root's
    STYLE.md, and the root's rules."""
    _write(root / ".git", "gitdir: ../.git/worktrees/project\n")
    _write(root / "STYLE.md", "Use four-space indentation.\n")
    _write(root / ".veritas" / "rules.md", "Never log request bodies.\n")
    _write(root / "src" / "pkg" / "go.mod", "module example.com/pkg\n\ngo 1.22\n")
    _write(root / "src" / "pkg" / "AGENTS.md", " \n\n")
    _write(root / "src" / "pkg" / "main.go", "package main\n")
    return ReviewScope.MODULE, str(root / "src" / "pkg")


PR_TARGET = "https://github.com/owner/repo/pull/1"

TMP_CASES = {
    "b": case_b,
    "c": case_c,
    "d": case_d,
    "e": case_e,
    "f": case_f,
    "h": case_h,
    "i": case_i,
    "j": case_j,
    "k": case_k,
}

# T119: expectations hand-written in the golden test, not in the JSON.
WALK_UP_CASES = {
    "l": case_l,
    "m": case_m,
    "n": case_n,
    "o": case_o,
}
