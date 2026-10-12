"""Contract tests — GitHub REST fetcher and poster vs contracts/hosting-api.md (T029, T110, T115)."""

import base64
import json
import ssl

import httpx
import pytest

from veritas.hosting.github import (
    GitHubAPIError,
    GitHubFetcher,
    GitHubPoster,
    _new_client,
    _reject_writes,
)
from veritas.hosting.types import PullRefs, RootEntry
from veritas.utils.logging import Log

BASE = "https://api.github.com"
OWNER, REPO, NUM = "acme", "widget", 123


def _make_fetcher(handler) -> GitHubFetcher:
    """A fetcher whose client is built by the production ``_new_client`` with
    the read-only hook, with a mock in place of the network."""
    client = GitHubFetcher("ghp_test", base_url=BASE)
    client.client = _new_client(
        "ghp_test", BASE, read_only=True, transport=httpx.MockTransport(handler)
    )
    return client


def _make_poster(handler) -> GitHubPoster:
    client = GitHubPoster("ghp_test", base_url=BASE)
    client.client = _new_client(
        "ghp_test", BASE, read_only=False, transport=httpx.MockTransport(handler)
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

    client = _make_fetcher(handler)
    assert client.head_sha(OWNER, REPO, NUM) == "sha123"


def test_pr_files_paginated():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return _json_response([{"filename": "src/util.py"}])
        headers = {
            "link": f'<{BASE}/repos/{OWNER}/{REPO}/pulls/{NUM}/files?per_page=100&page=2>; rel="next"'
        }
        return _json_response([{"filename": "src/app.py"}], headers=headers)

    client = _make_fetcher(handler)
    files = client.list_pr_files(OWNER, REPO, NUM)
    assert [f["filename"] for f in files] == ["src/app.py", "src/util.py"]


def test_file_contents_base64_decoded():
    content = "import os\nprint(1)\n"
    encoded = base64.b64encode(content.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("ref") == "sha123"
        return _json_response({"encoding": "base64", "content": encoded})

    client = _make_fetcher(handler)
    assert client.get_file_contents(OWNER, REPO, "src/app.py", "sha123") == content


def test_post_comment():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{OWNER}/{REPO}/issues/{NUM}/comments"
        captured["body"] = json.loads(request.content)["body"]
        return _json_response({}, status=201)

    client = _make_poster(handler)
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

    client = _make_fetcher(handler)
    assert client.head_sha(OWNER, REPO, NUM) == "ok"
    assert calls["n"] == 2


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found", request=request)

    client = _make_fetcher(handler)
    with pytest.raises(GitHubAPIError) as exc:
        client.pull_details(OWNER, REPO, NUM)
    assert exc.value.status == 404


def test_rate_limited_after_retries(monkeypatch):
    monkeypatch.setattr("veritas.hosting.github.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    client = _make_fetcher(handler)
    with pytest.raises(GitHubAPIError) as exc:
        client.pull_details(OWNER, REPO, NUM)
    assert exc.value.status == 429


# -- T110: read-only fetcher ---------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_fetcher_blocks_write_methods_before_any_request(method):
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return _json_response({}, status=201)

    client = _make_fetcher(handler)
    with pytest.raises(GitHubAPIError) as exc:
        client._request(method, f"/repos/{OWNER}/{REPO}/issues/{NUM}/comments", json={"body": "x"})
    assert str(exc.value) == f"github API error: 0 — blocked: {method} on read-only fetcher"
    assert exc.value.status == 0
    assert reached == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_fetcher_allows_read_methods(method):
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.method)
        return _json_response({"head": {"sha": "sha123"}})

    client = _make_fetcher(handler)
    response = client._request(method, f"/repos/{OWNER}/{REPO}/pulls/{NUM}")
    assert response.status_code == 200
    assert reached == [method]


def test_production_fetcher_has_the_hook_and_the_poster_has_none():
    assert GitHubFetcher("ghp_test").client.event_hooks["request"] == [_reject_writes]
    assert GitHubPoster("ghp_test").client.event_hooks["request"] == []


def test_production_fetcher_keeps_plain_client_tls_and_env_proxies(monkeypatch):
    """The hook leaves transports alone: under HTTPS_PROXY a production fetcher
    has a plain httpx.Client's proxy mounts and certificate verification.
    Reads private httpx fields, in this test only, to confirm nothing changed."""
    for name in ("HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    fetcher = GitHubFetcher("ghp_test")
    plain = httpx.Client()

    def mounts(client):
        return {str(pattern.pattern): type(t) for pattern, t in client._mounts.items()}

    assert mounts(fetcher.client) == mounts(plain) == {"https://": httpx.HTTPTransport}
    context = fetcher.client._transport._pool._ssl_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


# -- T115: project-context reads -------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_pull_refs_returns_head_and_base_sha():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{OWNER}/{REPO}/pulls/{NUM}"
        return _json_response({"head": {"sha": "headsha"}, "base": {"sha": "basesha"}})

    client = _make_fetcher(handler)
    assert client.pull_refs(OWNER, REPO, NUM) == PullRefs(head_sha="headsha", base_sha="basesha")
    assert client.head_sha(OWNER, REPO, NUM) == "headsha"


def test_list_root_parses_name_type_and_size_in_one_request():
    """GitHub's contents listing is not paginated: one GET, no page parameter.
    Directories get size None; symlinks and submodules are left out."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(
            [
                {"name": "README.md", "type": "file", "size": 120},
                {"name": "docs", "type": "dir", "size": 0},
                {"name": "link", "type": "symlink", "size": 9},
                {"name": "vendored", "type": "submodule", "size": 0},
                {"name": "pyproject.toml", "type": "file", "size": 2048},
            ]
        )

    client = _make_fetcher(handler)
    entries = client.list_root(OWNER, REPO, "basesha")
    assert entries == [
        RootEntry(name="README.md", type="file", size=120),
        RootEntry(name="docs", type="dir", size=None),
        RootEntry(name="pyproject.toml", type="file", size=2048),
    ]
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/repos/{OWNER}/{REPO}/contents"
    assert dict(seen[0].url.params) == {"ref": "basesha"}


def test_list_root_rejects_a_non_list_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"type": "file", "name": "x"})

    with pytest.raises(GitHubAPIError) as exc:
        _make_fetcher(handler).list_root(OWNER, REPO, "basesha")
    assert "not a directory listing" in str(exc.value)


def test_read_optional_returns_decoded_text_on_200():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{OWNER}/{REPO}/contents/CONTRIBUTING.md"
        assert request.url.params.get("ref") == "basesha"
        return _json_response({"type": "file", "encoding": "base64", "content": _b64("Use tabs.\n")})

    assert _make_fetcher(handler).read_optional(OWNER, REPO, "CONTRIBUTING.md", "basesha") == "Use tabs.\n"


def test_read_optional_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"}, request=request)

    assert _make_fetcher(handler).read_optional(OWNER, REPO, "CONTRIBUTING.md", "basesha") is None


def test_read_optional_raises_on_502_with_the_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway", request=request)

    with pytest.raises(GitHubAPIError) as exc:
        _make_fetcher(handler).read_optional(OWNER, REPO, "CONTRIBUTING.md", "basesha")
    assert exc.value.status == 502
    assert exc.value.path == "CONTRIBUTING.md"


def test_read_optional_raises_for_a_directory():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response([{"name": "a.md", "type": "file", "size": 1}])

    with pytest.raises(GitHubAPIError) as exc:
        _make_fetcher(handler).read_optional(OWNER, REPO, "docs", "basesha")
    assert str(exc.value) == "github API error: 200 — path is a directory, not a file (path: docs)"


def test_read_optional_raises_for_a_too_large_file():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"type": "file", "encoding": "none", "content": "", "size": 5_000_000})

    with pytest.raises(GitHubAPIError) as exc:
        _make_fetcher(handler).read_optional(OWNER, REPO, "big.md", "basesha")
    assert str(exc.value) == (
        'github API error: 200 — file too large for the contents API (encoding "none", no content)'
        " (path: big.md)"
    )


def test_read_optional_raises_for_a_symlink_entry():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"type": "symlink", "target": "../elsewhere"})

    with pytest.raises(GitHubAPIError) as exc:
        _make_fetcher(handler).read_optional(OWNER, REPO, "link", "basesha")
    assert "path is a symlink, not a file" in str(exc.value)


def test_project_context_reads_send_only_gets_through_the_hooked_client():
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path.endswith("/contents"):
            return _json_response([])
        if "/pulls/" in request.url.path:
            return _json_response({"head": {"sha": "h"}, "base": {"sha": "b"}})
        return _json_response({"type": "file", "encoding": "base64", "content": _b64("x")})

    client = _make_fetcher(handler)
    assert client.client.event_hooks["request"] == [_reject_writes]
    client.pull_refs(OWNER, REPO, NUM)
    client.list_root(OWNER, REPO, "b")
    client.read_optional(OWNER, REPO, "README.md", "b")
    assert methods == ["GET", "GET", "GET"]
