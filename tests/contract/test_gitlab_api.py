"""Contract tests — GitLab REST fetcher and poster vs contracts/hosting-api.md (T030, T110)."""

import base64
import json
import ssl

import httpx
import pytest

from veritas.hosting.gitlab import (
    GitLabAPIError,
    GitLabFetcher,
    GitLabPoster,
    _new_client,
    _reject_writes,
)

BASE = "https://gitlab.com"
OWNER, REPO, IID = "group", "project", 7


def _make_fetcher(handler) -> GitLabFetcher:
    """A fetcher whose client is built by the production ``_new_client`` with
    the read-only hook, with a mock in place of the network."""
    client = GitLabFetcher("glpat-test", base_url="https://gitlab.com")
    client.client = _new_client(
        "glpat-test", f"{BASE}/api/v4", read_only=True, transport=httpx.MockTransport(handler)
    )
    return client


def _make_poster(handler) -> GitLabPoster:
    client = GitLabPoster("glpat-test", base_url="https://gitlab.com")
    client.client = _new_client(
        "glpat-test", f"{BASE}/api/v4", read_only=False, transport=httpx.MockTransport(handler)
    )
    return client


def _json_response(payload, status=200):
    return httpx.Response(status, json=payload)


def test_project_id_url_encoded():
    assert GitLabFetcher.project_id(OWNER, REPO) == "group%2Fproject"


def test_head_sha_resolved():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v4/projects/{OWNER}/{REPO}/merge_requests/{IID}"
        return _json_response({"diff_refs": {"head_sha": "mrsha"}})

    client = _make_fetcher(handler)
    assert client.head_sha(OWNER, REPO, IID) == "mrsha"


def test_mr_changes_filtered_by_caller_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "changes": [
                    {"old_path": "a.py", "new_path": "b.py"},
                    {"old_path": "gone.md", "new_path": "gone.md", "deleted_file": True},
                ]
            }
        )

    client = _make_fetcher(handler)
    changes = client.list_mr_changes(OWNER, REPO, IID)
    assert len(changes) == 2
    assert changes[1]["deleted_file"] is True


def test_file_at_ref_base64():
    content = "const x = 1;"
    encoded = base64.b64encode(content.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("ref") == "mrsha"
        assert f"repository/files/src/app.py" in request.url.path
        return _json_response({"encoding": "base64", "content": encoded})

    client = _make_fetcher(handler)
    assert client.get_file_at_ref(OWNER, REPO, "src/app.py", "mrsha") == content


def test_post_note():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)["body"]
        return _json_response({}, status=201)

    client = _make_poster(handler)
    client.post_note(OWNER, REPO, IID, "notes")
    assert captured["body"] == "notes"


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized", request=request)

    client = _make_fetcher(handler)
    with pytest.raises(GitLabAPIError) as exc:
        client.mr_details(OWNER, REPO, IID)
    assert exc.value.status == 401


def test_post_note_url_and_method():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return _json_response({}, status=201)

    _make_poster(handler).post_note(OWNER, REPO, IID, "notes")
    assert seen == {
        "method": "POST",
        "url": f"{BASE}/api/v4/projects/group%2Fproject/merge_requests/{IID}/notes",
    }


# -- T110: read-only fetcher ---------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_fetcher_blocks_write_methods_before_any_request(method):
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return _json_response({}, status=201)

    client = _make_fetcher(handler)
    with pytest.raises(GitLabAPIError) as exc:
        client._request(method, f"/projects/group%2Fproject/merge_requests/{IID}/notes", json={"body": "x"})
    assert str(exc.value) == f"gitlab API error: 0 — blocked: {method} on read-only fetcher"
    assert exc.value.status == 0
    assert reached == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_fetcher_allows_read_methods(method):
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.method)
        return _json_response({"diff_refs": {"head_sha": "mrsha"}})

    client = _make_fetcher(handler)
    response = client._request(method, f"/projects/group%2Fproject/merge_requests/{IID}")
    assert response.status_code == 200
    assert reached == [method]


def test_production_fetcher_has_the_hook_and_the_poster_has_none():
    assert GitLabFetcher("glpat-test").client.event_hooks["request"] == [_reject_writes]
    assert GitLabPoster("glpat-test").client.event_hooks["request"] == []


def test_production_fetcher_keeps_plain_client_tls_and_env_proxies(monkeypatch):
    """The hook leaves transports alone: under HTTPS_PROXY a production fetcher
    has a plain httpx.Client's proxy mounts and certificate verification.
    Reads private httpx fields, in this test only, to confirm nothing changed."""
    for name in ("HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    fetcher = GitLabFetcher("glpat-test", base_url="https://gitlab.example.com")
    plain = httpx.Client()

    def mounts(client):
        return {str(pattern.pattern): type(t) for pattern, t in client._mounts.items()}

    assert fetcher.base_url == "https://gitlab.example.com/api/v4"
    assert mounts(fetcher.client) == mounts(plain) == {"https://": httpx.HTTPTransport}
    context = fetcher.client._transport._pool._ssl_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
