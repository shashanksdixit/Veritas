"""Read-only review tools (T010).

These are the ONLY tools bound to review nodes (constitution Read-Only Safety
Boundary). They operate on an in-memory ``files`` mapping (relative path ->
content) populated by the scope node — never on the ability to write, execute,
or reach write/execute capabilities. ``write_tools`` lives in a separate module
imported only by the terminal render node.
"""

from __future__ import annotations

import ast
import re
from typing import Iterable


def read_file(files: dict[str, str], path: str) -> str:
    """Return file content at ``path`` ('' if absent)."""
    return files.get(path, "")


def list_dir(files: dict[str, str], path: str = "") -> list[str]:
    """List scoped file paths under ``path`` ('' = scope root)."""
    prefix = path.strip("/")
    prefix_slash = f"{prefix}/" if prefix else ""
    seen: set[str] = set()
    for key in files:
        if key.startswith(prefix_slash):
            seen.add(key)
    return sorted(seen)


def grep_code(files: dict[str, str], pattern: str, path: str = "") -> list[dict]:
    """Return ``{file, line, text}`` matches for ``pattern`` in scoped files."""
    regex = re.compile(pattern)
    prefix_slash = path.strip("/")
    results: list[dict] = []
    for key in sorted(files):
        if prefix_slash and not key.startswith(f"{prefix_slash}/"):
            continue
        for lineno, text in enumerate(files[key].splitlines(), start=1):
            if regex.search(text):
                results.append({"file": key, "line": lineno, "text": text})
    return results


def get_ast(files: dict[str, str], path: str) -> str:
    """Return a compact AST dump for Python files; informative message otherwise."""
    content = files.get(path)
    if content is None:
        return f"<file not in scope: {path}>"
    if not path.endswith(".py"):
        return f"<get_ast supports Python only; {path} is not a .py file>"
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        return f"<cannot parse: {exc}>"
    return ast.dump(tree, indent=None)


def scoped_files(files: dict[str, str]) -> Iterable[str]:
    return (key for key in sorted(files))