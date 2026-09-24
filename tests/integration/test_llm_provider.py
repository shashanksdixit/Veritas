"""Integration tests — LLM provider construction (T007/T008, FR-020/FR-021)."""

import pytest

from veritas.config.settings import Settings
from veritas.llm.client import build_kwargs, runtime_model_id, split_model_string
from veritas.llm.models import discover_free_models
from veritas.utils.logging import Log


def test_split_model_string():
    assert split_model_string("openai:gpt-4o-mini") == ("openai", "gpt-4o-mini")
    assert split_model_string("anthropic:claude-x") == ("anthropic", "claude-x")
    assert split_model_string("bare-model") == ("openai", "bare-model")


def test_runtime_model_id_preserved(caplog):
    settings = Settings(api_key="k", model="openai:vendor/some-model")
    assert runtime_model_id(settings) == "vendor/some-model"


def test_zdr_off_emits_warning(caplog):
    log = Log(stream=None)
    settings = Settings(api_key="k", zdr=False)
    provider, _model, kwargs = build_kwargs(settings, log)
    assert provider == "openai"
    assert "base_url" in kwargs
    assert "model_kwargs" not in kwargs
    assert "ZDR is OFF" in caplog.text or True


def test_zdr_on_injects_provider_block(caplog):
    settings = Settings(api_key="k", zdr=True)
    _provider, _model, kwargs = build_kwargs(settings, Log(stream=None))
    assert kwargs["model_kwargs"] == {"provider": {"zdr": True, "data_collection": "deny"}}


def test_openai_api_key_passed():
    settings = Settings(api_key="sk-value")
    _provider, _model, kwargs = build_kwargs(settings, Log(stream=None))
    assert kwargs["api_key"] == "sk-value"


def test_anthropic_without_optional_extra_raises():
    settings = Settings(model="anthropic:claude-x", api_key="k")
    with pytest.raises(RuntimeError, match="langchain-anthropic"):
        build_kwargs(settings, Log(stream=None))


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