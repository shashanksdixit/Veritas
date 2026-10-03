"""LLM client (T007) — single entry point via LangChain's ``init_chat_model``.

The resolved model string is ``<provider>:<model>`` (research.md §2):
  * ``openai:...``   → OpenAI-compatible route (OpenRouter by default)
  * ``anthropic:...``→ native Anthropic route (requires the optional
    ``langchain-anthropic`` package — see FR-020 / T007).
Never the openai SDK directly. Calls are wrapped with structured logging
(latency, model, token usage — constitution Principle VIII).
"""

from __future__ import annotations

import time

from langchain.chat_models import init_chat_model  # type: ignore[import-untyped]
from langchain_core.messages import HumanMessage, SystemMessage

from veritas.config.settings import Settings
from veritas.llm.zdr import backend_is_openrouter, zdr_body_extras
from veritas.utils.logging import Log


def split_model_string(model_string: str) -> tuple[str, str]:
    """Split ``<provider>:<model>``. A bare model id defaults to the openai prefix."""
    if ":" in model_string:
        provider, model_id = model_string.split(":", 1)
    else:
        provider, model_id = "openai", model_string
    return provider, model_id


def build_kwargs(settings: Settings) -> tuple[str, str, dict]:
    """LangChain init_chat_model kwargs for the configured provider routing.

    Deliberately free of logging: this runs more than once per run (see
    ``runtime_model_id``), so anything emitted here is printed more than once per
    run. The single per-run data-retention warning belongs to the run start-up
    path, which runs exactly once (FR-021).
    """
    provider, model_id = split_model_string(settings.model_runtime)
    # Bound every request (FR-019). These two are set once, before the branches,
    # so no provider route can be added later without them: without a timeout the
    # client library's own default applies, which on the OpenAI-compatible route
    # is 30 minutes — a stalled endpoint would hold the whole run. `timeout` is
    # the only spelling both backends accept: it is ChatOpenAI's validation alias
    # for `request_timeout`, and ChatAnthropic's alias for
    # `default_request_timeout`. Both models set `extra="ignore"`, so a wrong
    # name would be dropped silently and leave the request unbounded.
    kwargs: dict = {
        "temperature": 0.0,
        "timeout": settings.timeout_seconds,
        "max_retries": settings.max_retries,
    }

    if provider == "anthropic":
        try:
            import langchain_anthropic  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Native Anthropic support requires the optional extra. "
                "Install it with: `pip install veritas[anthropic]` (langchain-anthropic)."
            ) from exc
        if settings.api_key:
            kwargs["api_key"] = settings.api_key
    else:
        # OpenAI-compatible route, reached by both the `openai` prefix and an
        # unknown one: OpenRouter by default, or any base_url the user configures
        # (e.g. a self-hosted OpenAI-compatible endpoint). Neither prefix has a
        # native client of its own, so both are handled identically here.
        kwargs["base_url"] = settings.base_url
        if settings.api_key:
            kwargs["api_key"] = settings.api_key
        # ZDR's provider block is an OpenRouter *request-body* field, so it travels
        # as `extra_body` (a real ChatOpenAI field the OpenAI SDK merges into the
        # JSON payload) and never as `model_kwargs`: LangChain spreads those as
        # top-level client arguments, where `provider` is not a parameter of
        # Completions.create and raises TypeError on every call. Guarded on the
        # host as well — the run start-up gate has already refused zdr=true on a
        # non-OpenRouter backend, and a provider block nothing honours would be a
        # silent false claim of zero data retention.
        if settings.zdr and backend_is_openrouter(provider, settings.base_url):
            kwargs["extra_body"] = zdr_body_extras(True)

    # model_id here is the backend's own model identifier (e.g. OpenRouter
    # catalog id "openai/gpt-4o-mini" or Anthropic's "claude-sonnet-4-6").
    return provider, model_id, kwargs


def build_chat_model(settings: Settings):
    provider, model_id, kwargs = build_kwargs(settings)
    return init_chat_model(model=model_id, model_provider=provider, **kwargs)


def runtime_model_id(settings: Settings) -> str:
    """The backend model identifier recorded as ReviewRun.model_name."""
    _provider, model_id = split_model_string(settings.model_runtime)
    return model_id


class LLMClient:
    """Thin wrapper around a LangChain chat model with structured logging."""

    def __init__(self, settings: Settings, log: Log | None = None) -> None:
        self.settings = settings
        self.log = log or Log()
        self._model = build_chat_model(settings)
        self.model_name = runtime_model_id(settings)

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        """Run one chat completion; returns the assistant text (never None)."""
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        started = time.monotonic()
        error: str | None = None
        try:
            response = self._model.invoke(messages, **( {"max_tokens": max_tokens} if max_tokens else {}))
            text = response.content or ""
        except Exception as exc:  # noqa: BLE001 - record then re-raise for FR-027 handling
            error = str(exc)
            latency_ms = (time.monotonic() - started) * 1000
            usage = {}
            self.log.llm_call(self.model_name, latency_ms, error=error)
            raise
        latency_ms = (time.monotonic() - started) * 1000
        metadata = response.response_metadata or {}
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
        self.log.llm_call(
            self.model_name,
            latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        return text if isinstance(text, str) else str(text)