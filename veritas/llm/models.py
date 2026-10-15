"""Free-model discovery (T009, FR-019).

Authenticated ``GET {base_url}/models`` (OpenRouter); select ``:free``-suffixed
models whose ``pricing.prompt == "0"`` (research.md §2). Returns the OpenRouter
catalog ids (slash-formatted, e.g. ``meta-llama/llama-3.1-8b-instruct:free``).
"""

from __future__ import annotations

import httpx

from veritas.utils.logging import Log

_MODELS_ENDPOINT = "/models"


def discover_free_models(base_url: str, api_key: str | None, log: Log | None = None) -> list[str]:
    """Discover ``:free`` models from the LLM endpoint.

    Returns an empty list on any failure (auth, network, malformed response) —
    callers fall back to the configured default model rather than failing.
    """
    log = log or Log()
    if not api_key:
        log.warn("No VERITAS_API_KEY set; skipping free-model discovery, using default model.")
        return []
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                f"{base_url.rstrip('/')}{_MODELS_ENDPOINT}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warn(f"Free-model discovery failed ({exc}); using default model.")
        return []

    free_ids: list[str] = []
    for model in data.get("data", []):
        model_id = model.get("id", "")
        pricing = model.get("pricing", {}) or {}
        if model_id.endswith(":free") and str(pricing.get("prompt")) == "0":
            free_ids.append(model_id)
    return free_ids