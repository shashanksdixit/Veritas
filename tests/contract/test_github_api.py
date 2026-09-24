"""Contract tests — GitHub REST client vs contracts/hosting-api.md (T029)."""

import base64
import json

import httpx
import pytest

from veritas.hosting.github import GitHubAPIError, GitHubClient
from veritas.utils.logging import Log

BASE = "https://api.github.com"
OWNER, REPO, NUM = "acme", "widget", 123


def _make_client(handler) -> GitHubClient:
    client = GitHubClient("ghp_test", base_url=BASE)
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=BASE,
        headers={"Authorization": "Bearer ghp_test"},
        timeout=30.0,
    )
    return client


def _json_response(payload, status=200, headers=None):
    return httpx.Response(
        status, json=payload, headers=headers or {"content-type": "application/json"}
    )


def test_head_sha_resolved():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{OWNER}/{REPO}/pulls/{NUM}"
        return _json_response({"head": {"sha": "sha123"}})

    client = _make_client(handler)
    assert client.head_sha(OWNER, REPO, NUM) == "sha123"


def test_pr_files_paginated():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return _json_response([{"filename": "src/util.py"}])
        headers = {
            "link": f'<{BASE}/repos/{OWNER}/{REPO}/pulls/{NUM}/files?per_page=100&page=2>; rel="next"'
        }
        return _json_response([{"filename": "src/app.py"}], headers=headers)

    client = _make_client(handler)
    files = client.list_pr_files(OWNER, REPO, NUM)
    assert [f["filename"] for f in files] == ["src/app.py", "src/util.py"]


def test_file_contents_base64_decoded():
    content = "import os\nprint(1)\n"
    encoded = base64.b64encode(content.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("ref") == "sha123"
        return _json_response({"encoding": "base64", "content": encoded})

    client = _make_client(handler)
    assert client.get_file_contents(OWNER, REPO, "src/app.py", "sha123") == content


def test_post_comment():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{OWNER}/{REPO}/issues/{NUM}/comments"
        captured["body"] = json.loads(request.content)["body"]
        return _json_response({}, status=201)

    client = _make_client(handler)
    client.post_comment(OWNER, REPO, NUM, "report body")
    assert captured["body"] == "report body"


def test_429_then_success_retries(monkeypatch):
    monkeypatch.setattr("veritas.hosting.github.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, request=request)
        return _json_response({"head": {"sha": "ok"}})

    client = _make_client(handler)
    assert client.head_sha(OWNER, REPO, NUM) == "ok"
    assert calls["n"] == 2


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found", request=request)

    client = _make_client(handler)
    with pytest.raises(GitHubAPIError) as exc:
        client.pull_details(OWNER, REPO, NUM)
    assert exc.value.status == 404


def test_rate_limited_after_retries(monkeypatch):
    monkeypatch.setattr("veritas.hosting.github.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    client = _make_client(handler)
    with pytest.raises(GitHubAPIError) as exc:
        client.pull_details(OWNER, REPO, NUM)
    assert exc.value.status == 429