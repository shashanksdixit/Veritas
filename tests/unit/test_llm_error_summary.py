"""Unit tests — LLM error summaries (FR-029).

Every summary is one line of at most 200 characters carrying the provider
label, the HTTP status, the provider's own message and a reason code — and
never the response body, which carries account identifiers.
"""

import json

import httpx
import openai

from veritas.llm.client import MAX_ERROR_SUMMARY_CHARS, summarize_llm_error

_CREDIT_MESSAGE = (
    "This request would exceed your available credits given your current "
    "in-flight requests."
)
_REASON = "in_flight_budget_exhausted"
_USER_ID = "user_TEST123"
_RAW_BODY = (
    "Error code: 402 - {'error': {'message': '"
    + _CREDIT_MESSAGE
    + "', 'code': 402, 'metadata': {'reason': '"
    + _REASON
    + "'}}, 'user_id': '"
    + _USER_ID
    + "'}"
)

_CREDIT_BODY = {
    "error": {
        "message": _CREDIT_MESSAGE,
        "code": 402,
        "metadata": {"reason": _REASON},
    },
    "user_id": _USER_ID,
}


class ProviderStatusError(Exception):
    """The shape a provider status failure arrives in: body, status, message."""

    def __init__(self, body, *, status_code=402, message=_RAW_BODY):
        super().__init__(message)
        self.body = body
        self.status_code = status_code
        self.message = message


def test_label_defaults_to_llm():
    assert summarize_llm_error(RuntimeError("provider exploded")) == (
        "llm: provider exploded"
    )


def test_label_comes_from_the_caller():
    assert summarize_llm_error(
        RuntimeError("provider exploded"), provider="anthropic"
    ) == "anthropic: provider exploded"


def test_label_falls_back_to_the_route_the_client_stamped():
    """A guarded node holds only the exception; the label rides on it."""
    exc = RuntimeError("provider exploded")
    exc.veritas_provider = "openrouter"
    assert summarize_llm_error(exc) == "openrouter: provider exploded"


def test_status_message_and_reason_come_from_the_parsed_body():
    summary = summarize_llm_error(ProviderStatusError(_CREDIT_BODY), provider="openai")
    assert summary == (
        f"openai 402: {_CREDIT_MESSAGE} [{_REASON}]"
    )
    assert _USER_ID not in summary
    assert "{'error'" not in summary
    assert len(summary) <= MAX_ERROR_SUMMARY_CHARS


def test_body_without_the_error_wrapper_is_read_the_same_way():
    """The SDK may hand over the inner object rather than the whole payload."""
    summary = summarize_llm_error(
        ProviderStatusError(_CREDIT_BODY["error"]), provider="openai"
    )
    assert summary == f"openai 402: {_CREDIT_MESSAGE} [{_REASON}]"


def test_body_as_a_json_string_is_parsed_not_echoed():
    summary = summarize_llm_error(
        ProviderStatusError(json.dumps(_CREDIT_BODY)), provider="openai"
    )
    assert summary == f"openai 402: {_CREDIT_MESSAGE} [{_REASON}]"
    assert _USER_ID not in summary


def test_status_read_from_the_response_when_the_exception_has_none():
    class ResponseError(Exception):
        message = _RAW_BODY

        def __init__(self):
            super().__init__(_RAW_BODY)
            self.response = httpx.Response(402, json=_CREDIT_BODY)

    summary = summarize_llm_error(ResponseError(), provider="openai")
    assert summary == f"openai 402: {_CREDIT_MESSAGE} [{_REASON}]"


def test_a_reason_equal_to_the_status_is_not_repeated():
    body = {"error": {"message": _CREDIT_MESSAGE, "code": "402"}}
    summary = summarize_llm_error(ProviderStatusError(body), provider="openai")
    assert summary == f"openai 402: {_CREDIT_MESSAGE}"
    assert "402] " not in summary


def test_a_code_that_is_not_the_status_is_the_reason():
    body = {"error": {"message": "Insufficient quota", "code": "insufficient_quota"}}
    summary = summarize_llm_error(
        ProviderStatusError(body, status_code=None), provider="openai"
    )
    assert summary == "openai: Insufficient quota [insufficient_quota]"


def test_a_long_message_is_truncated_but_the_reason_survives():
    body = {
        "error": {
            "message": "credit " * 100,
            "code": 402,
            "metadata": {"reason": _REASON},
        }
    }
    summary = summarize_llm_error(ProviderStatusError(body), provider="openai")
    assert len(summary) <= MAX_ERROR_SUMMARY_CHARS
    assert summary.startswith("openai 402: credit")
    assert summary.endswith(f"[{_REASON}]")


def test_a_long_reason_cannot_eat_the_whole_budget():
    body = {
        "error": {
            "message": _CREDIT_MESSAGE,
            "code": 402,
            "metadata": {"reason": "r" * 300},
        }
    }
    summary = summarize_llm_error(ProviderStatusError(body), provider="openai")
    assert len(summary) <= MAX_ERROR_SUMMARY_CHARS
    assert _CREDIT_MESSAGE in summary


def test_raw_body_text_is_cut_when_there_is_no_parsed_payload():
    summary = summarize_llm_error(ProviderStatusError(None), provider="openai")
    assert summary == "openai 402: Error code: 402"
    assert _USER_ID not in summary
    assert "{'error'" not in summary


def test_a_text_that_is_only_a_body_falls_back_to_the_exception_type():
    summary = summarize_llm_error(
        ProviderStatusError(None, message=json.dumps(_CREDIT_BODY))
    )
    assert summary == "llm 402: ProviderStatusError"
    assert _USER_ID not in summary


def test_timeout_error_summarizes_sensibly():
    exc = openai.APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )
    summary = summarize_llm_error(exc, provider="openai")
    assert summary == "openai: Request timed out."
    assert len(summary) <= MAX_ERROR_SUMMARY_CHARS


def test_generic_exception_summarizes_sensibly():
    summary = summarize_llm_error(ValueError("cannot unpack"), provider="openai")
    assert summary == "openai: cannot unpack"
