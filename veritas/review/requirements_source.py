"""Which files are requirement sources, and what requirements they hold (FR-008).

Discovery is by pattern, not by asking the model where the requirements are, so
the same repository always yields the same sources in the same order. The
priority order is the one FR-008 fixes: a spec-kit feature spec first (because its
`- **FR-NNN**: text` lines parse into discrete, citable requirements), then the
conventional root and docs files, then `README.md` last.

Pure by design: no settings, no filesystem, no logging. Everything here takes a
path string or the text of a file that a caller has already read, which keeps the
discovery rules testable and makes the scope node responsible for I/O alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from veritas.config.constants import (
    REQUIREMENTS_MAX_TEXT_CHARS,
    REQUIREMENTS_ROOT_SOURCES,
    REQUIREMENTS_SPEC_GLOB_PARTS,
)

# FR-008: the functional requirement line of a spec-kit spec, e.g.
#   - **FR-008**: Findings MUST carry a confidence between 0.0 and 1.0.
# A suffix letter is allowed so a spec can amend a requirement (- **FR-008a**:)
# without a new number. Anchored per line, no DOTALL: the ID must open the line
# after an optional indent and a single "- ", and the text runs to end of line.
FR_LINE = re.compile(r"^\s*-\s*\*\*(FR-\d+[a-z]?)\*\*:\s*(.+)$")

# A `specs/<feature>/spec.md` is a spec-kit feature spec; anything else under
# `specs/` (a plan, a checklist, a README) is not a requirement source.
_SPEC_PARTS = REQUIREMENTS_SPEC_GLOB_PARTS


@dataclass(frozen=True)
class Requirement:
    """One requirement line lifted out of a spec-kit spec (FR-008).

    ``file`` and ``line`` are what a reviewer cites as evidence, so they are the
    location in the spec the text came from, 1-based, not in the reviewer's copy
    of it. Frozen because a requirement is a fact about the source file: two
    consumers reading the same scope must not see different text.
    """

    id: str
    text: str
    file: str
    line: int


def is_requirements_source(path: str) -> bool:
    """True when ``path`` is a requirement source under the FR-008 rules.

    ``path`` is the repository-relative, forward-slash path. A checklists
    directory disqualifies a file whatever it is called: those files track
    whether an item is done, not what the software must do, so reading them as
    requirements produces findings about the project's own to-do list.
    """
    parts = _parts(path)
    if not parts:
        return False
    if any(part.lower() == "checklists" for part in parts[:-1]):
        return False
    if _is_spec_kit_spec(path):
        return True
    return "/".join(parts) in REQUIREMENTS_ROOT_SOURCES


def source_priority(path: str) -> int:
    """Discovery rank of ``path``, lower meaning higher priority (FR-008).

    Returns ``len(REQUIREMENTS_ROOT_SOURCES) + 1`` for anything that is not a
    requirement source, so a caller can sort a whole file set without filtering
    first and still get non-sources last. Every spec-kit spec shares one rank:
    which feature a spec describes does not make it more authoritative than
    another, and the caller's tie-break on path keeps the order stable.
    """
    if not is_requirements_source(path):
        return len(REQUIREMENTS_ROOT_SOURCES) + 1
    if _is_spec_kit_spec(path):
        return 0
    return REQUIREMENTS_ROOT_SOURCES.index("/".join(_parts(path))) + 1


def extract_requirements(
    path: str, text: str
) -> tuple[list[Requirement], list[str]]:
    """Pull the functional requirement lines out of one spec-kit spec (FR-008).

    Returns ``(requirements, warnings)``. A source with no matching line yields
    an empty list and no warning: the caller falls back to free text, which is a
    normal case (an early draft, a PRD) rather than a problem to report.

    Lines are returned in file order with 1-based line numbers, so the list is
    the spec as written. Text is truncated to
    :data:`REQUIREMENTS_MAX_TEXT_CHARS` characters with a trailing ``...``, since
    a single FR line can be a page long and a review prompt cannot carry it.

    A duplicate ID keeps the first occurrence and reports the rest, because two
    lines claiming ``FR-003`` means the spec contradicts itself and silently
    picking one would make the review look like it had read both.
    """
    requirements: list[Requirement] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}
    for index, line in enumerate(text.splitlines(), start=1):
        match = FR_LINE.match(line)
        if match is None:
            continue
        identifier = match.group(1)
        if identifier in seen:
            warnings.append(
                f"{path}:{index}: duplicate requirement id {identifier} "
                f"(first seen on line {seen[identifier]}); keeping the first"
            )
            continue
        seen[identifier] = index
        requirements.append(
            Requirement(
                id=identifier,
                text=_truncate(match.group(2).strip()),
                file=path,
                line=index,
            )
        )
    return requirements, warnings


def _truncate(value: str) -> str:
    if len(value) <= REQUIREMENTS_MAX_TEXT_CHARS:
        return value
    return value[:REQUIREMENTS_MAX_TEXT_CHARS] + "..."


def _parts(path: str) -> list[str]:
    """The path as non-empty components, tolerant of `.`/`..` and separators."""
    return [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]


def _is_spec_kit_spec(path: str) -> bool:
    """True for ``specs/<one directory>/spec.md``; a `*` part matches anything."""
    parts = _parts(path)
    return len(parts) == len(_SPEC_PARTS) and all(
        expected == "*" or part == expected
        for part, expected in zip(parts, _SPEC_PARTS)
    )