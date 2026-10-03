"""Integration tests — the ZDR toggle (T079, FR-021).

Covers the three properties that were previously broken or unprovable:

  * ``provider.zdr`` / ``provider.data_collection`` really reach the request body
    (verified against a real HTTP server, not against the kwargs dict), and never
    as ``model_kwargs``, which the OpenAI SDK rejects.
  * ``zdr=true`` on a non-OpenRouter backend stops the run through the real CLI,
    before any fetch, read or model call.
  * the data-retention warning is printed exactly once per run, worded for the
    backend.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import FakeLLM, default_fake_llm

from veritas.config.settings import Settings
from veritas.llm.client import LLMClient, build_kwargs
from veritas.llm.zdr import (
    backend_is_openrouter,
    is_openrouter,
    zdr_body_extras,
    zdr_warning_text,
)
from veritas.models.entities import ReviewScope
from veritas.review.graph import run_review
from veritas.utils.logging import Log

_PROVIDER_BLOCK = {"provider": {"zdr": True, "data_collection": "deny"}}


# --- host detection ---


@pytest.mark.parametrize(
    "base_url",
    ["https://openrouter.ai/api/v1", "https://api.openrouter.ai/v1"],
)
def test_is_openrouter_true(base_url):
    assert is_openrouter(base_url) is True


@pytest.mark.parametrize(
    "base_url",
    [
        # Substring lookalikes: a naive `"openrouter.ai" in base_url` accepts both.
        "https://openrouter.ai.evil.com/v1",
        "https://evil-openrouter.ai/v1",
        "https://api.openai.com/v1",
        "http://127.0.0.1:8080/v1",
        "",
    ],
)
def test_is_openrouter_false(base_url):
    assert is_openrouter(base_url) is False


def test_anthropic_route_is_never_openrouter_despite_base_url():
    """The anthropic branch ignores base_url, so a default one must not imply support."""
    assert backend_is_openrouter("anthropic", "https://openrouter.ai/api/v1") is False
    assert backend_is_openrouter("openai", "https://openrouter.ai/api/v1") is True
    assert backend_is_openrouter("openai", "https://api.openai.com/v1") is False


def test_body_extras_are_empty_when_zdr_off():
    assert zdr_body_extras(False) == {}
    assert zdr_body_extras(True) == _PROVIDER_BLOCK


# --- no logging from the kwargs builder ---


@pytest.mark.parametrize("zdr", [True, False])
def test_build_kwargs_produces_no_log_output(capsys, zdr):
    """build_kwargs runs more than once per run; a warning here prints N times."""
    build_kwargs(Settings(api_key="k", zdr=zdr))
    build_kwargs(Settings(api_key="k", zdr=zdr))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_provider_block_never_reaches_a_non_openrouter_backend():
    """Even if the gate were bypassed, an un-honoured provider block must not be sent."""
    _p, _m, kwargs = build_kwargs(Settings(api_key="k", zdr=True, base_url="https://api.openai.com/v1"))
    assert "extra_body" not in kwargs
    assert "model_kwargs" not in kwargs


# --- the real request body ---


_CHAT_COMPLETION = {
    "id": "chatcmpl-zdr",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


@pytest.fixture
def echo_server():
    """A real HTTP server on 127.0.0.1 that records every POST body.

    Yields ``(port, bodies)``. Asserting on the kwargs dict would not prove the
    provider block reaches the wire: the bug being guarded against is precisely
    that a correct-looking kwarg never arrives (LangChain spreads ``model_kwargs``
    as client arguments, which raises before any request). So the body is read
    off the socket, not inferred from what was passed in.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 + an explicit close: each POST is one self-contained
        # request/response, so no handler thread is left parked in readline() on
        # an idle keep-alive socket at teardown (which both races the next test's
        # bind and leaks a thread into interpreter shutdown).
        protocol_version = "HTTP/1.0"

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                bodies.append(json.loads(raw))
            except json.JSONDecodeError:
                bodies.append({"__raw__": raw.decode("utf-8", "replace")})
            payload = json.dumps(_CHAT_COMPLETION).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], bodies
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
    import inspect

    model = getattr(client, "_model", None)
    for attr in ("client", "root_client", "async_client", "root_async_client"):
        inner = getattr(model, attr, None)
        if inner is None:
            continue
        closer = getattr(inner, "close", None)
        if not callable(closer) or inspect.iscoroutinefunction(closer):
            continue
        try:
            closer()
        except Exception:  # noqa: BLE001 - teardown must not mask a failure
            pass


def test_zdr_true_reaches_the_wire_as_a_top_level_provider_block(
    echo_server, no_proxy, monkeypatch
):
    """The regression itself: zdr=true used to raise TypeError on every call."""
    port, bodies = echo_server
    # The only thing stubbed: make the loopback URL count as OpenRouter, so the
    # real request-construction path (and its host guard) still runs.
    monkeypatch.setattr("veritas.llm.zdr.is_openrouter", lambda _url: True)

    client = LLMClient(
        Settings(
            api_key="test",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="openai:gpt-4o-mini",
            zdr=True,
            timeout_seconds=10,
            max_retries=0,
        ),
        Log(stream=None),
    )
    try:
        assert client.complete("system", "user") == "ok"
    finally:
        _close_client(client)

    assert len(bodies) == 1, f"expected one recorded request, got {len(bodies)}"
    print(f"\n[zdr real body] {json.dumps(bodies[0], sort_keys=True)}")
    assert bodies[0]["provider"] == {"zdr": True, "data_collection": "deny"}


def test_zdr_false_sends_no_provider_block(echo_server, no_proxy, monkeypatch):
    port, bodies = echo_server
    monkeypatch.setattr("veritas.llm.zdr.is_openrouter", lambda _url: True)

    client = LLMClient(
        Settings(
            api_key="test",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="openai:gpt-4o-mini",
            zdr=False,
            timeout_seconds=10,
            max_retries=0,
        ),
        Log(stream=None),
    )
    try:
        assert client.complete("system", "user") == "ok"
    finally:
        _close_client(client)

    assert len(bodies) == 1
    assert "provider" not in bodies[0]


# --- fail closed, through the real CLI ---


def _no_work_spies(monkeypatch) -> dict:
    """Record every attempt to reach a hosting provider or an LLM."""
    from veritas.llm import client as client_module
    from veritas.review.nodes import scope as scope_module

    calls: dict[str, list] = {"hosting": [], "llm_client": [], "llm_call": []}

    def fake_hosting(settings, provider, log):
        calls["hosting"].append(provider)
        raise AssertionError("a hosting client was built on a refused run")

    def fake_init_chat_model(*args, **kwargs):
        calls["llm_client"].append(kwargs)
        raise AssertionError("an LLM client was built on a refused run")

    def fake_complete(self, *args, **kwargs):
        calls["llm_call"].append(args)
        raise AssertionError("an LLM call was made on a refused run")

    monkeypatch.setattr(scope_module, "build_hosting_client", fake_hosting)
    monkeypatch.setattr(client_module, "init_chat_model", fake_init_chat_model)
    monkeypatch.setattr(client_module.LLMClient, "complete", fake_complete)
    return calls


@pytest.mark.parametrize(
    ("case", "env_overrides"),
    [
        # The anthropic route ignores base_url entirely, so it is refused even
        # though the default base_url still points at OpenRouter.
        ("anthropic-route", {"VERITAS_MODEL": "anthropic:claude-sonnet-4-6"}),
        (
            "openai-route-on-openai",
            {
                "VERITAS_MODEL": "openai:gpt-4o-mini",
                "VERITAS_BASE_URL": "https://api.openai.com/v1",
            },
        ),
    ],
)
def test_cli_fails_closed_when_zdr_on_a_non_openrouter_backend(
    monkeypatch, case, env_overrides
):
    """Both non-OpenRouter routes are refused at the real CLI entry point.

    ``--scope pr`` is deliberate: without the gate the scope node WOULD build a
    hosting client, which is what makes the spy a real assertion rather than a
    vacuous one for a scope that never contacts a hosting provider.
    """
    from typer.testing import CliRunner

    from veritas.cli.app import app

    calls = _no_work_spies(monkeypatch)
    env = {
        "VERITAS_API_KEY": "test-key",
        "VERITAS_GITHUB_TOKEN": "ghp_test",
        "VERITAS_ZDR": "true",
    }
    env.update(env_overrides)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["review", "--scope", "pr", "--target", "acme/widget#3"],
        env=env,
    )

    assert result.exit_code == 1, result.output
    assert "ZDR is only supported with OpenRouter" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
    # Named so a failure says which case regressed.
    assert calls == {"hosting": [], "llm_client": [], "llm_call": []}, case


def test_cli_fail_closed_message_names_the_backend(monkeypatch):
    """The user is told what is configured and what to do about it."""
    from typer.testing import CliRunner

    from veritas.cli.app import app

    _no_work_spies(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["review", "--scope", "pr", "--target", "acme/widget#3"],
        env={
            "VERITAS_API_KEY": "test-key",
            "VERITAS_ZDR": "true",
            "VERITAS_MODEL": "openai:gpt-4o-mini",
            "VERITAS_BASE_URL": "https://api.openai.com/v1",
        },
    )
    assert result.exit_code == 1
    for expected in (
        "The configured backend is openai at https://api.openai.com/v1",
        "Nothing was sent.",
        "VERITAS_ZDR=false",
        "https://openrouter.ai/api/v1",
    ):
        assert expected in result.stderr, expected


def test_zdr_true_on_openrouter_is_not_refused(sample_project):
    """The gate must not block the one backend that supports ZDR."""
    settings = Settings(
        api_key="k",
        base_url="https://openrouter.ai/api/v1",
        zdr=True,
    )
    outcome = run_review(
        settings,
        ReviewScope.PROJECT,
        str(sample_project),
        llm=default_fake_llm(),
    )
    assert outcome.exit_code == 0


# --- the warning, exactly once, worded for the backend ---


@pytest.mark.parametrize(
    ("base_url", "expect_free_tier", "expect_account"),
    [
        ("https://openrouter.ai/api/v1", True, False),
        ("https://api.openai.com/v1", False, True),
    ],
)
def test_data_retention_warning_printed_exactly_once_per_run(
    sample_project, capsys, base_url, expect_free_tier, expect_account
):
    settings = Settings(api_key="k", base_url=base_url, zdr=False)
    outcome = run_review(
        settings,
        ReviewScope.PROJECT,
        str(sample_project),
        llm=default_fake_llm(),
    )
    assert outcome.exit_code == 0
    stderr = capsys.readouterr().err
    count = stderr.count("ZDR is OFF")
    assert count == 1, f"expected 1 data-retention warning, saw {count}:\n{stderr}"
    assert ("free-tier models" in stderr) is expect_free_tier
    assert ("account agreement" in stderr) is expect_account


def test_warning_appears_once_even_with_many_llm_calls(sample_project, capsys):
    """A full run makes several LLM calls; the warning is per run, not per call."""
    llm = default_fake_llm()
    settings = Settings(api_key="k", base_url="https://openrouter.ai/api/v1", zdr=False)
    run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=llm)
    stderr = capsys.readouterr().err
    assert len(llm.calls) > 1, "the run made too few calls to prove anything"
    assert stderr.count("ZDR is OFF") == 1


def test_no_warning_when_zdr_is_on(sample_project, capsys):
    settings = Settings(api_key="k", base_url="https://openrouter.ai/api/v1", zdr=True)
    run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=FakeLLM({}))
    assert "ZDR is OFF" not in capsys.readouterr().err


def test_warning_text_is_backend_specific():
    assert "free-tier models" in zdr_warning_text("https://openrouter.ai/api/v1", "openai")
    other = zdr_warning_text("https://api.openai.com/v1", "openai")
    assert "account agreement" in other
    assert "free-tier" not in other