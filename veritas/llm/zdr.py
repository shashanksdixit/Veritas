"""ZDR toggle (T008/T079, FR-021).

Off by default. When on, BOTH ``provider.zdr: true`` AND
``provider.data_collection: "deny"`` are injected into the OpenRouter request body
(two distinct documented OpenRouter fields — research.md §2). When off, a
data-retention warning is printed to stderr exactly once per run, worded for the
configured backend (constitution Privacy & Data Handling, v5.1.0).

Every function here is pure: no logging, no I/O, no settings lookup. The single
per-run warning is emitted by the run start-up path, never from here, because
client kwargs are built more than once per run and a warning emitted from
``build_kwargs`` is therefore printed more than once.
"""

from __future__ import annotations

from urllib.parse import urlparse

_OPENROUTER_HOST = "openrouter.ai"

_OPENROUTER_WARNING = (
    "ZDR is OFF: some free-tier models reserve the right to train on "
    "inputs/outputs. Set VERITAS_ZDR=true for reviews of proprietary or "
    "sensitive code."
)

_OTHER_BACKEND_WARNING = (
    "ZDR is OFF: data retention for this backend is governed by your account "
    "agreement with {provider}. Set VERITAS_ZDR=true only if that agreement "
    "already provides zero data retention (ZDR is enforced per request on "
    "OpenRouter only)."
)


def is_openrouter(base_url: str) -> bool:
    """True only when ``base_url``'s host is openrouter.ai or a subdomain of it.

    Compared against the parsed hostname, not by substring, so lookalikes such as
    ``https://openrouter.ai.evil.com/v1`` and ``https://evil-openrouter.ai/v1``
    are rejected. A missing, empty or unparseable base_url is not OpenRouter.
    """
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except ValueError:
        return False
    return host == _OPENROUTER_HOST or host.endswith(f".{_OPENROUTER_HOST}")


def backend_is_openrouter(provider: str, base_url: str) -> bool:
    """True only when ZDR can actually be enforced for this route.

    The anthropic route talks to Anthropic's native API and ignores ``base_url``
    entirely, so a left-over default OpenRouter ``base_url`` must not make it look
    supported (FR-020 native Claude path).
    """
    if provider == "anthropic":
        return False
    return is_openrouter(base_url)


def zdr_body_extras(zdr: bool) -> dict:
    """The request-body extras for the ZDR setting (``{}`` when off)."""
    if zdr:
        return {"provider": {"zdr": True, "data_collection": "deny"}}
    return {}


def zdr_warning_text(base_url: str, provider: str = "") -> str:
    """The per-run data-retention warning, worded for the configured backend."""
    if is_openrouter(base_url):
        return _OPENROUTER_WARNING
    return _OTHER_BACKEND_WARNING.format(provider=provider or base_url or "that provider")