"""Integration tests — LLM provider construction (T007/T008, FR-020/FR-021)."""

import io
import json
from pathlib import Path

import pytest

from veritas.config.constants import LAST_REPORT_JSON
from veritas.config.settings import Settings
from veritas.llm.client import (
    MAX_ERROR_SUMMARY_CHARS,
    LLMClient,
    build_chat_model,
    build_kwargs,
    runtime_model_id,
    split_model_string,
    summarize_llm_error,
)
from veritas.llm.models import discover_free_models
from veritas.models.entities import Report, ReviewScope
from veritas.review.graph import run_review
from veritas.utils.logging import Log


def test_split_model_string():
    assert split_model_string("openai:gpt-4o-mini") == ("openai", "gpt-4o-mini")
    assert split_model_string("anthropic:claude-x") == ("anthropic", "claude-x")
    assert split_model_string("bare-model") == ("openai", "bare-model")


def test_runtime_model_id_preserved(caplog):
    settings = Settings(api_key="k", model="openai:vendor/some-model")
    assert runtime_model_id(settings) == "vendor/some-model"


def test_zdr_off_sends_no_provider_block(capsys):
    settings = Settings(api_key="k", zdr=False)
    provider, _model, kwargs = build_kwargs(settings)
    assert provider == "openai"
    assert "base_url" in kwargs
    assert "extra_body" not in kwargs
    assert "model_kwargs" not in kwargs
    # Nothing is emitted here: build_kwargs runs more than once per run, so the
    # per-run warning belongs to the start-up path (FR-021, T079).
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_zdr_on_injects_provider_block():
    """extra_body, never model_kwargs: LangChain spreads model_kwargs as top-level
    client arguments, where the OpenAI SDK rejects `provider` with a TypeError."""
    settings = Settings(api_key="k", zdr=True)
    _provider, _model, kwargs = build_kwargs(settings)
    assert kwargs["extra_body"] == {"provider": {"zdr": True, "data_collection": "deny"}}
    assert "model_kwargs" not in kwargs


def test_openai_api_key_passed():
    settings = Settings(api_key="sk-value")
    _provider, _model, kwargs = build_kwargs(settings)
    assert kwargs["api_key"] == "sk-value"


def test_anthropic_without_optional_extra_raises():
    settings = Settings(model="anthropic:claude-x", api_key="k")
    with pytest.raises(RuntimeError, match="langchain-anthropic"):
        build_kwargs(settings)


def test_discover_free_models_none_api_key_returns_empty(caplog):
    models = discover_free_models("https://openrouter.ai/api/v1", None)
    assert models == []


def test_discover_free_models_filters(monkeypatch):
    import httpx

    class FakeTransport(httpx.MockTransport):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "meta-llama/llama-3.1-8b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
                    {"id": "openai/gpt-4o-mini", "pricing": {"prompt": "0.00015", "completion": "0.0006"}},
                    {"id": "x/model:free", "pricing": {"prompt": "0.01", "completion": "0.01"}},
                ]
            },
        )

    fake_client = httpx.Client(transport=httpx.MockTransport(handler))

    def fake_get(*args, **kwargs):
        return fake_client.get(*args, **kwargs)

    monkeypatch.setattr("veritas.llm.models.httpx.Client", lambda **kw: fake_client)
    models = discover_free_models("https://openrouter.ai/api/v1", "k")
    assert models == ["meta-llama/llama-3.1-8b-instruct:free"]


# --- bounded requests (FR-019) ---


def _bounded(**overrides) -> Settings:
    base = {"api_key": "k", "timeout_seconds": 42.0, "max_retries": 4}
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize(
    "model_string",
    ["openai:gpt-4o-mini", "weird-provider:some-model"],
)
def test_build_kwargs_bounds_openai_compatible_branches(model_string):
    """Both the openai route and the unknown-provider fallback are bounded."""
    settings = _bounded(model=model_string)
    provider, model_id, kwargs = build_kwargs(settings)
    assert (provider, model_id) == tuple(model_string.split(":", 1))
    assert kwargs["timeout"] == 42.0
    assert kwargs["max_retries"] == 4


def test_build_kwargs_bounds_the_anthropic_branch(monkeypatch):
    """The anthropic route is bounded too.

    The branch imports the optional extra before it returns, so the import is
    stubbed: that is what keeps this test deterministic on machines without
    langchain-anthropic, rather than skipping the branch entirely.
    """
    import sys
    import types

    monkeypatch.setitem(sys.modules, "langchain_anthropic", types.ModuleType("langchain_anthropic"))
    settings = _bounded(model="anthropic:claude-x")
    provider, model_id, kwargs = build_kwargs(settings)
    assert (provider, model_id) == ("anthropic", "claude-x")
    assert kwargs["timeout"] == 42.0
    assert kwargs["max_retries"] == 4


def test_build_kwargs_defaults_are_used_when_settings_not_set():
    _provider, _model_id, kwargs = build_kwargs(Settings(api_key="k"))
    assert kwargs["timeout"] == 120
    assert kwargs["max_retries"] == 2


def test_build_kwargs_passes_zero_retries_through():
    """0 means "no retry" and must survive, not be dropped as falsy."""
    _provider, _model_id, kwargs = build_kwargs(Settings(api_key="k", max_retries=0))
    assert kwargs["max_retries"] == 0


def test_constructed_openai_model_carries_the_bounds():
    """The values must survive model construction, not just build_kwargs."""
    from langchain_openai import ChatOpenAI

    model = build_chat_model(_bounded())
    assert isinstance(model, ChatOpenAI)
    # `timeout` is ChatOpenAI's validation alias for `request_timeout`.
    assert model.request_timeout == 42.0
    assert model.max_retries == 4


# --- the real stall tests (FR-019) ---


@pytest.fixture
def stall_server():
    """A TCP server on 127.0.0.1 that accepts connections and never replies.

    Yields ``(port, accepted)``. ``accepted`` grows as connections land, so a
    test can prove the request really reached the socket rather than failing
    fast for some unrelated reason.
    """
    import socket
    import struct
    import threading

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    accepted: list = []
    stop = threading.Event()

    def _accept_and_hold():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except (socket.timeout, OSError):
                continue
            # RST on close rather than a graceful FIN: the peer has an
            # unanswered request on this socket and must not be left waiting on a
            # half-closed connection.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            accepted.append(conn)  # keep it open: no response, no close

    thread = threading.Thread(target=_accept_and_hold, daemon=True)
    thread.start()
    try:
        yield port, accepted
    finally:
        stop.set()
        thread.join(timeout=5)
        for conn in accepted:
            try:
                conn.close()
            except OSError:
                pass
        server.close()


@pytest.fixture
def no_proxy(monkeypatch):
    """Stop a proxy in the environment from swallowing the loopback call."""
    for var in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _close_client(client) -> None:
    """Release the chat model's HTTP connection pools.

    Each test leaves one unanswered request behind. Closing the pools
    deterministically keeps that half-open socket from lingering into the next
    test or into interpreter shutdown.
    """
    import inspect

    model = getattr(client, "_model", None)
    for attr in ("client", "root_client", "async_client", "root_async_client"):
        inner = getattr(model, attr, None)
        if inner is None:
            continue
        closer = getattr(inner, "close", None)
        # The async clients' close() is a coroutine; these tests are synchronous,
        # and calling it un-awaited would leave a "never awaited" warning behind.
        if not callable(closer) or inspect.iscoroutinefunction(closer):
            continue
        try:
            closer()
        except Exception:  # noqa: BLE001 - teardown must not mask a failure
            pass


def _stalled_client(port: int) -> LLMClient:
    # zdr stays OFF: this endpoint is not OpenRouter, so a zdr=true client is
    # refused outright by the run start-up gate and would never reach this socket.
    # The point here is the timeout, so the refusal must not be what fails first.
    return LLMClient(
        Settings(
            api_key="test",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="openai:gpt-4o-mini",
            timeout_seconds=1,
            max_retries=0,
        ),
        Log(stream=None),
    )


def test_stalled_endpoint_times_out_instead_of_waiting(stall_server, no_proxy, capsys):
    """A real request to a real socket that never answers must fail, fast.

    This is the only test that catches a silently-unbounded request: these models
    are configured `extra="ignore"`, so a misspelled kwarg is dropped rather than
    raising, and only an actually-timing-out request proves the bound is wired.
    """
    import time

    port, accepted = stall_server
    client = _stalled_client(port)
    try:
        started = time.monotonic()
        with pytest.raises(Exception) as excinfo:
            client.complete("system", "user")
        elapsed = time.monotonic() - started
        # Reported so a regression is visible in CI output, not just a green test.
        with capsys.disabled():
            print(
                f"\n[stall test] reached_socket={bool(accepted)} "
                f"raised {type(excinfo.value).__name__} after {elapsed:.2f}s: "
                f"{str(excinfo.value)[:160]}"
            )
        assert accepted, "no connection reached the stall server; the test proved nothing"
        assert elapsed < 10, f"request took {elapsed:.1f}s; it was not bounded"
    finally:
        _close_client(client)


def test_stalled_endpoint_fails_without_retrying(stall_server, no_proxy):
    """max_retries=0 means the timeout costs one attempt, not several."""
    import time

    port, accepted = stall_server
    client = _stalled_client(port)
    try:
        started = time.monotonic()
        with pytest.raises(Exception):
            client.complete("system", "user")
        elapsed = time.monotonic() - started
        # 4 retries against a 1s timeout would take ~4s; one attempt is ~1s. The
        # bound is loose on purpose: this asserts the order of magnitude, not timing.
        assert len(accepted) == 1, f"expected exactly one attempt, saw {len(accepted)}"
        assert elapsed < 3.5, f"{elapsed:.1f}s suggests the request was retried"
    finally:
        _close_client(client)


# --- provider failures are summarized, never echoed (FR-029) ---

_CREDIT_MESSAGE = (
    "This request would exceed your available credits given your current "
    "in-flight requests."
)
_REASON = "in_flight_budget_exhausted"
_USER_ID = "user_TEST123"

# The body a real provider sends with a 402: the words worth reporting, plus an
# account identifier that must not travel.
_PAYMENT_REQUIRED_BODY = json.dumps(
    {
        "error": {
            "message": _CREDIT_MESSAGE,
            "code": 402,
            "metadata": {"reason": _REASON},
        },
        "user_id": _USER_ID,
    }
).encode("utf-8")


@pytest.fixture
def payment_required_server():
    """An OpenAI-compatible endpoint that always answers 402.

    Yields ``(port, requests_seen)``.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    requests_seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            requests_seen.append(self.path)
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(_PAYMENT_REQUIRED_BODY)))
            self.end_headers()
            self.wfile.write(_PAYMENT_REQUIRED_BODY)

        def log_message(self, *_args):  # keep the test output clean
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], requests_seen
    finally:
        server.shutdown()
        server.server_close()


def _payment_client(port: int, log: Log | None = None) -> LLMClient:
    return LLMClient(
        Settings(
            api_key="test",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="openai:gpt-4o-mini",
            timeout_seconds=10,
            max_retries=0,
        ),
        log or Log(stream=None),
    )


def test_a_real_402_is_summarized_in_the_log_and_never_echoed(
    payment_required_server, no_proxy
):
    """The status, the provider's words and the reason reach the log; the body
    (and its user_id) does not, in the summary or anywhere else."""
    port, requests_seen = payment_required_server
    stream = io.StringIO()
    client = _payment_client(port, Log(stream=stream))
    try:
        with pytest.raises(Exception) as excinfo:
            client.complete("system", "user")
    finally:
        _close_client(client)

    assert requests_seen, "no request reached the endpoint; nothing was proved"

    summary = summarize_llm_error(excinfo.value, provider=client.provider)
    assert summary == (
        f"openai 402: {_CREDIT_MESSAGE} [{_REASON}]"
    )
    assert len(summary) <= MAX_ERROR_SUMMARY_CHARS
    assert _USER_ID not in summary
    assert "{'error'" not in summary

    logged = stream.getvalue()
    assert summary in logged
    assert _USER_ID not in logged


def test_a_timeout_is_summarized_too(stall_server, no_proxy):
    """A failure with no status and no body still yields one sensible line."""
    port, accepted = stall_server
    client = _stalled_client(port)
    try:
        with pytest.raises(Exception) as excinfo:
            client.complete("system", "user")
    finally:
        _close_client(client)

    assert accepted, "no connection reached the stall server; nothing was proved"
    summary = summarize_llm_error(excinfo.value)
    assert summary.startswith("openai")
    assert "timed out" in summary.lower()
    assert len(summary) <= MAX_ERROR_SUMMARY_CHARS


def test_the_report_carries_the_summary_and_not_the_body(
    payment_required_server, no_proxy, monkeypatch, sample_project, settings
):
    """End to end: every batch failing with 402 leaves run.error holding the
    summary — status, provider message, reason — and no account identifier."""
    from veritas.review.nodes import scope as scope_module
    from veritas.security.opengrep import OpengrepResult

    # SAST is not what this test is about, and whether Opengrep is installed
    # would decide part of the run.
    monkeypatch.setattr(
        scope_module,
        "collect_sast",
        lambda *_a, **_k: OpengrepResult(findings=[], rules="r"),
    )

    port, _requests_seen = payment_required_server
    client = _payment_client(port)
    try:
        outcome = run_review(
            settings, ReviewScope.PROJECT, str(sample_project), llm=client
        )
    finally:
        _close_client(client)

    assert outcome.exit_code == 2
    report = Report.model_validate_json(
        Path(LAST_REPORT_JSON).read_text(encoding="utf-8")
    )
    error = report.run.error or ""
    assert report.run.report_status.value == "incomplete"
    assert "openai 402:" in error
    assert _CREDIT_MESSAGE in error
    assert _REASON in error
    assert _USER_ID not in error
    assert "{'error'" not in error