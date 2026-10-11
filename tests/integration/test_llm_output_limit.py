"""Provider error URL removal and [llm] max_output_tokens (FR-019, FR-029)."""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import ValidationError

from veritas.config.settings import Settings, load_settings
from veritas.llm.client import LLMClient, summarize_llm_error
from veritas.utils.logging import Log

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _no_proxy(monkeypatch):
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _serve(finish_reason="stop"):
    """A loopback chat-completions endpoint that records each request body."""
    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            bodies.append(json.loads(self.rfile.read(length) or b"{}"))
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
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
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
    return server, thread, bodies


def _client(port: int, stream: io.StringIO) -> LLMClient:
    return LLMClient(
        Settings(
            api_key="test",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="openai:gpt-4o-mini",
            max_output_tokens=1234,
        ),
        Log(stream=stream),
    )


def test_provider_error_summary_removes_urls():
    summary = summarize_llm_error(
        RuntimeError(
            "Not enough credits. To increase, visit "
            "https://openrouter.ai/workspaces/default/keys/c04de123 now"
        )
    )
    assert "[link removed]" in summary
    assert "openrouter.ai/workspaces" not in summary
    assert len(summary) <= 200


def test_max_output_tokens_default():
    assert Settings().max_output_tokens == 8192


def test_max_output_tokens_toml_override(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[llm]\nmax_output_tokens = 2048\n", encoding="utf-8")
    assert load_settings(str(path)).max_output_tokens == 2048


def test_max_output_tokens_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VERITAS_MAX_OUTPUT_TOKENS", "3000")
    assert load_settings(str(tmp_path / "missing.toml")).max_output_tokens == 3000


@pytest.mark.parametrize("value", [255, 65537])
def test_max_output_tokens_bounds_rejected(value):
    with pytest.raises(ValidationError):
        Settings(max_output_tokens=value)


def test_request_body_carries_the_output_limit(monkeypatch):
    _no_proxy(monkeypatch)
    server, thread, bodies = _serve()
    try:
        _client(server.server_address[1], io.StringIO()).complete("system", "user")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(bodies) == 1
    keys = [key for key, value in bodies[0].items() if value == 1234]
    print(f"output limit sent as: {keys}")
    assert keys, f"1234 not found in request body: {bodies[0]}"


def test_truncated_response_warns_with_the_configured_limit(monkeypatch):
    _no_proxy(monkeypatch)
    stream = io.StringIO()
    server, thread, _bodies = _serve(finish_reason="length")
    try:
        _client(server.server_address[1], stream).complete("system", "user")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    output = stream.getvalue()
    assert "truncated" in output, output
    assert "1234" in output, output
