"""Suppression allowlist store (T015) — reads/writes ``.veritas/suppressions.json``.

The file is git-tracked (constitution Review Suppression). Entries are keyed by
code fingerprint so suppression survives unrelated edits and lapses when the
flagged code changes.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from veritas.config.constants import SUPPRESSIONS_PATH, VERSION
from veritas.models.entities import Category, SuppressionEntry, compute_fingerprint

_ENTRIES_ADAPTER = TypeAdapter(list[SuppressionEntry])


class SuppressionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else Path(SUPPRESSIONS_PATH)

    def load(self) -> list[SuppressionEntry]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = data.get("entries", []) if isinstance(data, dict) else data
        return _ENTRIES_ADAPTER.validate_python(entries or [])

    def save(self, entries: list[SuppressionEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "generated_by": f"veritas/{VERSION}",
            "entries": [entry.model_dump(mode="json") for entry in entries],
        }
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def upsert_suppression(
        self,
        file: str,
        category: Category | str,
        title: str,
        snippet: str,
        reason: str | None = None,
    ) -> SuppressionEntry:
        category_val = category if isinstance(category, Category) else Category(category)
        fingerprint = compute_fingerprint(file, category_val.value, snippet)
        entries = self.load()
        for entry in entries:
            if entry.fingerprint == fingerprint:
                return entry
        entry = SuppressionEntry(
            fingerprint=fingerprint,
            file=file,
            category=category_val,
            title=title,
            added_at=datetime.now(),
            reason=reason,
        )
        entries.append(entry)
        self.save(entries)
        return entry

    def remove(self, fingerprint: str) -> bool:
        entries = self.load()
        kept = [e for e in entries if e.fingerprint != fingerprint]
        if len(kept) == len(entries):
            return False
        self.save(kept)
        return True

    def find(self, fingerprint: str) -> SuppressionEntry | None:
        for entry in self.load():
            if entry.fingerprint == fingerprint:
                return entry
        return None

    def list(self) -> list[SuppressionEntry]:
        return self.load()