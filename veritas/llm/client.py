"""LLM client (T007) — single entry point via LangChain's ``init_chat_model``.

The resolved model string is ``<provider>:<model>`` (research.md §2):
  * ``openai:...``   → OpenAI-compatible route (OpenRouter by default)
  * ``anthropic:...``→ native Anthropic route (requires the optional
    ``langchain-anthropic`` package — see FR-020 / T007).
Never the openai SDK directly. Calls are wrapped with structured logging
(latency, model, token usage — constitution Principle VIII).
"""

from __future__ import annotations

import json
import time

from langchain.chat_models import init_chat_model  # type: ignore[import-untyped]
from langchain_core.messages import HumanMessage, SystemMessage

from veritas.config.settings import Settings
from veritas.llm.zdr import backend_is_openrouter, zdr_body_extras
from veritas.utils.logging import Log
from veritas.utils.redaction import redact_secrets


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


MAX_ERROR_SUMMARY_CHARS = 200

# Serialized-body markers. A provider exception whose text still carries its
# response body is cut at the first one, because a body carries the account
# identifiers (`user_id`, ...) that must never reach the report (FR-029).
_BODY_MARKERS = ("{'error'", '{"error"', '"error":', "'error':", "user_id")

# A reason is a short code; anything longer is not one, and must not eat the
# budget the message needs.
_MAX_REASON_CHARS = 60


def _as_json(value) -> dict | None:
    """Parse ``value`` as a JSON object, or return None if it is not one."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "[{":
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _is_status(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status an exception carries, if it carries one."""
    for attribute in ("status_code", "status"):
        value = getattr(exc, attribute, None)
        if _is_status(value):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    return value if _is_status(value) else None


def _provider_payload(exc: BaseException) -> dict | None:
    """The parsed response body, read structurally so nothing else leaks.

    Prefers ``exc.body`` (already parsed by the SDK), then the response's own
    JSON. Only the two fields :func:`_payload_parts` asks for are ever read out
    of it — the body is never echoed.
    """
    payload = _as_json(getattr(exc, "body", None))
    if payload is not None:
        return payload
    response = getattr(exc, "response", None)
    if response is None:
        return None
    reader = getattr(response, "json", None)
    if callable(reader):
        try:
            payload = _as_json(reader())
        except Exception:  # noqa: BLE001 - a body that is not JSON is normal
            payload = None
        if payload is not None:
            return payload
    return _as_json(getattr(response, "text", None))


def _payload_parts(payload) -> tuple[str, str | None]:
    """``(message, reason)`` from a parsed provider payload."""
    if not isinstance(payload, dict):
        return "", None
    error = payload.get("error")
    if isinstance(error, str):
        return " ".join(error.split()), None
    node = error if isinstance(error, dict) else payload
    message = node.get("message") or payload.get("message") or ""
    if not isinstance(message, str):
        message = str(message)
    message = " ".join(message.split())
    metadata = node.get("metadata")
    if isinstance(metadata, dict):
        reason = metadata.get("reason")
    elif metadata is not None:
        reason = getattr(metadata, "reason", None)
    else:
        reason = None
    if not reason:
        code = node.get("code")
        if isinstance(code, str) and code.strip():
            reason = code.strip()
        elif isinstance(code, int) and not isinstance(code, bool):
            reason = code
    if reason is None:
        return message, None
    return message, str(reason)[:_MAX_REASON_CHARS]


def _strip_body(text: str) -> str:
    """Whitespace-collapse ``text`` and cut a serialized body out of it.

    This is the fallback for an exception that carries no parsed payload: the
    SDK renders a body into ``str(exc)``, so the raw text is only usable up to
    the first thing that says "here comes the body".
    """
    collapsed = " ".join(text.split())
    lowered = collapsed.lower()
    cut = len(collapsed)
    for marker in _BODY_MARKERS:
        index = lowered.find(marker)
        if index != -1:
            cut = min(cut, index)
    return collapsed[:cut].rstrip(" -:{[(").strip()


def summarize_llm_error(exc: BaseException, provider: str | None = None) -> str:
    """One line, at most ``MAX_ERROR_SUMMARY_CHARS`` characters, for one failure.

    Carries the provider label, the HTTP status when the exception has one, the
    provider's own message and a reason code when present (FR-029) — and
    nothing else. A raw provider body is not echoed even when it is all there
    is: bodies carry account identifiers, and a full body is useless in a log
    line anyway. Callers pass the result through ``redact_secrets()`` as well.

    ``provider`` is the label. When the caller has none — a node holding only
    the exception — the label the client stamped on it as it re-raised
    (``veritas_provider``) is used, then ``llm``.

    Never raises: it is used to describe an already-failed call.
    """
    label = provider or getattr(exc, "veritas_provider", None) or "llm"
    try:
        status = _http_status(exc)
        message, reason = _payload_parts(_provider_payload(exc))
        if not message:
            message = _strip_body(str(getattr(exc, "message", None) or exc))
    except Exception:  # noqa: BLE001 - describing a failure must not fail too
        status, message, reason = None, "", None
    if not message:
        message = type(exc).__name__
    if reason is not None and str(reason) == str(status):
        reason = None

    head = f"{label} {status}" if status is not None else label
    tail = f" [{reason}]" if reason else ""
    budget = MAX_ERROR_SUMMARY_CHARS - len(head) - len(tail) - 2
    if budget < 1:
        # A pathological label or reason: the message goes, the status stays.
        tail = ""
        budget = MAX_ERROR_SUMMARY_CHARS - len(head) - 2
    if len(message) > budget:
        message = message[: max(budget - 1, 0)].rstrip() + "…"
    return f"{head}: {message}{tail}" if budget > 0 else head[:MAX_ERROR_SUMMARY_CHARS]


class LLMClient:
    """Thin wrapper around a LangChain chat model with structured logging."""

    def __init__(self, settings: Settings, log: Log | None = None) -> None:
        self.settings = settings
        self.log = log or Log()
        self._model = build_chat_model(settings)
        self.model_name = runtime_model_id(settings)
        # The route this client talks to (`openai`, `anthropic`, ...): the label
        # every error summary for it carries (FR-029).
        self.provider = split_model_string(settings.model_runtime)[0]

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        """Run one chat completion; returns the assistant text (never None)."""
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        started = time.monotonic()
        error: str | None = None
        try:
            response = self._model.invoke(messages, **( {"max_tokens": max_tokens} if max_tokens else {}))
            text = response.content or ""
        except Exception as exc:  # noqa: BLE001 - record then re-raise for FR-027 handling
            # The route is stamped on the way out so a summary built further up
            # (guarded nodes) can still name it without a reference to this
            # client (FR-029).
            setattr(exc, "veritas_provider", self.provider)
            error = redact_secrets(summarize_llm_error(exc, provider=self.provider))
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