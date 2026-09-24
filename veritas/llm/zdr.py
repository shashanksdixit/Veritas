"""ZDR toggle (T008, FR-021).

Off by default. When on, BOTH ``provider.zdr: true`` AND
``provider.data_collection: "deny"`` are injected into the request (two
distinct documented OpenRouter fields — research.md §2). When off, a warning is
printed to stderr once per run stating that some free-tier models reserve the
right to train on inputs/outputs and recommending ZDR for proprietary or
sensitive code.
"""

from __future__ import annotations

_WARNING = (
    "ZDR is OFF: some free-tier models reserve the right to train on "
    "inputs/outputs. Set VERITAS_ZDR=true for reviews of proprietary or "
    "sensitive code."
)


def zdr_model_kwargs(settings, log) -> dict:
    """Return request-body extras for the ZDR setting."""
    if settings.zdr:
        return {"provider": {"zdr": True, "data_collection": "deny"}}
    if log is not None:
        log.warn(_WARNING)
    return {}