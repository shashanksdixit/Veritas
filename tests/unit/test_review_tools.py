"""The review tools package holds only the render node's write tools (T113)."""

from __future__ import annotations

import importlib

import pytest

from veritas.review.tools import write_tools


def test_read_only_tools_module_is_not_importable() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("veritas.review.tools.read_only")


def test_write_tools_public_callables_are_exactly_the_render_writers() -> None:
    public = {
        name
        for name, value in vars(write_tools).items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", None) == write_tools.__name__
    }
    assert public == {"write_report", "persist_last_report_json"}
