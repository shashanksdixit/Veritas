"""Every LLM call is logged with its structured fields in every run (T135, FR-019).

The real ``LLMClient`` is used throughout, never with ``--verbose``. The OpenAI
shape goes through langchain-openai's own response parsing against a loopback
endpoint; the Anthropic shapes are constructed responses, because
langchain-anthropic is an optional extra not installed in the test environment.
"""

from __future__ import annotations

import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from langchain_core.messages import AIMessage

from veritas.config.settings import Settings
from veritas.llm import client as client_module
from veritas.llm.client import LLMClient
from veritas.utils.logging import Log

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No VERITAS_* variable and no config file of the developer's leaks in."""
    for name in list(os.environ):
        if name.startswith("VERITAS_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def _no_proxy(monkeypatch):
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _serve_openai(usage: dict):
    """A loopback chat-completions endpoint answering with ``usage``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            payload = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "[]"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _llm_call_fields(client: LLMClient, stream: io.StringIO) -> dict:
    """Run one completion; return the JSON fields of its one LLM-call line."""
    client.complete("system", "user")
    lines = [line for line in stream.getvalue().splitlines() if line.startswith("[info] LLM call ")]
    assert len(lines) == 1, stream.getvalue()
    return json.loads(lines[0][lines[0].rindex(" {") + 1 :])


def _fake_client(monkeypatch, response: AIMessage) -> tuple[LLMClient, io.StringIO]:
    """A real LLMClient whose chat model answers ``response``."""

    class FakeChat:
        def invoke(self, messages, **kwargs):
            return response

    monkeypatch.setattr(client_module, "init_chat_model", lambda **_kwargs: FakeChat())
    stream = io.StringIO()
    return LLMClient(Settings(api_key="test-key"), Log(stream=stream, verbose=False)), stream


def test_openai_usage_is_logged_without_verbose(monkeypatch):
    _no_proxy(monkeypatch)
    server, thread = _serve_openai({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
    stream = io.StringIO()
    client = LLMClient(
        Settings(
            api_key="test",
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            model="openai:gpt-4o-mini",
        ),
        Log(stream=stream, verbose=False),
    )
    try:
        fields = _llm_call_fields(client, stream)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert fields["model"] == client.model_name
    assert isinstance(fields["latency_ms"], float)
    assert (fields["prompt_tokens"], fields["completion_tokens"], fields["total_tokens"]) == (7, 3, 10)


def test_anthropic_style_usage_metadata_is_logged(monkeypatch):
    client, stream = _fake_client(
        monkeypatch,
        AIMessage(
            content="[]",
            usage_metadata={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
            response_metadata={"usage": {"input_tokens": 11, "output_tokens": 4}},
        ),
    )
    fields = _llm_call_fields(client, stream)
    assert fields["model"] == client.model_name
    assert (fields["prompt_tokens"], fields["completion_tokens"], fields["total_tokens"]) == (11, 4, 15)


def test_anthropic_style_response_metadata_alone_is_logged_without_a_computed_total(monkeypatch):
    client, stream = _fake_client(
        monkeypatch,
        AIMessage(content="[]", response_metadata={"usage": {"input_tokens": 11, "output_tokens": 4}}),
    )
    fields = _llm_call_fields(client, stream)
    assert fields["model"] == client.model_name
    assert (fields["prompt_tokens"], fields["completion_tokens"], fields["total_tokens"]) == (11, 4, None)


def test_a_response_without_usage_logs_null_token_counts(monkeypatch):
    client, stream = _fake_client(monkeypatch, AIMessage(content="[]"))
    fields = _llm_call_fields(client, stream)
    assert fields["model"] == client.model_name
    assert isinstance(fields["latency_ms"], float)
    assert (fields["prompt_tokens"], fields["completion_tokens"], fields["total_tokens"]) == (None, None, None)
