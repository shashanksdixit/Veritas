"""Contract tests — GitLab REST fetcher and poster vs contracts/hosting-api.md (T030, T110, T115)."""

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
from veritas.hosting.types import PullRefs, RootEntry

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


# -- T115: project-context reads -------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


TREE_PATH = "/api/v4/projects/group%2Fproject/repository/tree"


def test_pull_refs_returns_head_and_base_sha():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"diff_refs": {"head_sha": "headsha", "base_sha": "basesha", "start_sha": "s"}})

    client = _make_fetcher(handler)
    assert client.pull_refs(OWNER, REPO, IID) == PullRefs(head_sha="headsha", base_sha="basesha")
    assert client.head_sha(OWNER, REPO, IID) == "headsha"


def test_list_root_parses_name_and_type_with_size_none():
    """GitLab's tree API reports no size, so every entry's size is None;
    blob is a file, tree a directory, and a submodule (commit) is left out."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(
            [
                {"id": "1", "name": "docs", "type": "tree", "path": "docs", "mode": "040000"},
                {"id": "2", "name": "README.md", "type": "blob", "path": "README.md", "mode": "100644"},
                {"id": "3", "name": "vendored", "type": "commit", "path": "vendored", "mode": "160000"},
            ]
        )

    entries = _make_fetcher(handler).list_root(OWNER, REPO, "basesha")
    assert entries == [
        RootEntry(name="docs", type="dir", size=None),
        RootEntry(name="README.md", type="file", size=None),
    ]
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.raw_path.decode().split("?")[0] == TREE_PATH
    assert dict(seen[0].url.params) == {"ref": "basesha", "per_page": "100", "page": "1"}


def test_list_root_follows_x_next_page_across_pages():
    """A root of 101 entries comes in two pages of per_page=100; the second
    request asks for page 2 on the same URL and the empty x-next-page ends it."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if params["page"] == "1":
            items = [{"name": f"f{i:03}.py", "type": "blob"} for i in range(100)]
            return httpx.Response(200, json=items, headers={"x-next-page": "2", "x-page": "1"})
        items = [{"name": "zz", "type": "tree"}]
        return httpx.Response(200, json=items, headers={"x-next-page": "", "x-page": "2"})

    entries = _make_fetcher(handler).list_root(OWNER, REPO, "basesha")
    assert len(entries) == 101
    assert entries[0] == RootEntry(name="f000.py", type="file", size=None)
    assert entries[-1] == RootEntry(name="zz", type="dir", size=None)
    assert seen == [
        {"ref": "basesha", "per_page": "100", "page": "1"},
        {"ref": "basesha", "per_page": "100", "page": "2"},
    ]


def test_list_root_never_follows_a_link_header_to_another_host():
    """Pages are requested on the fetcher's own URL, so a Link header naming
    another host never receives the token."""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        page = request.url.params.get("page")
        headers = {
            "link": '<https://evil.example/api/v4/projects/x/repository/tree?page=2>; rel="next"',
            "x-next-page": "2" if page == "1" else "",
        }
        return httpx.Response(200, json=[{"name": f"p{page}", "type": "blob"}], headers=headers)

    entries = _make_fetcher(handler).list_root(OWNER, REPO, "basesha")
    assert [e.name for e in entries] == ["p1", "p2"]
    assert hosts == ["gitlab.com", "gitlab.com"]


def test_list_root_rejects_a_non_list_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "odd"})

    with pytest.raises(GitLabAPIError) as exc:
        _make_fetcher(handler).list_root(OWNER, REPO, "basesha")
    assert "not a list" in str(exc.value)


def test_read_optional_returns_decoded_text_on_200():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.decode().split("?")[0] == (
            "/api/v4/projects/group%2Fproject/repository/files/docs%2FSTYLE.md"
        )
        assert request.url.params.get("ref") == "basesha"
        return _json_response({"file_path": "docs/STYLE.md", "encoding": "base64", "content": _b64("No tabs.\n")})

    assert _make_fetcher(handler).read_optional(OWNER, REPO, "docs/STYLE.md", "basesha") == "No tabs.\n"


def test_read_optional_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "404 File Not Found"}, request=request)

    assert _make_fetcher(handler).read_optional(OWNER, REPO, "CONTRIBUTING.md", "basesha") is None


def test_read_optional_raises_on_502_with_the_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway", request=request)

    with pytest.raises(GitLabAPIError) as exc:
        _make_fetcher(handler).read_optional(OWNER, REPO, "CONTRIBUTING.md", "basesha")
    assert exc.value.status == 502
    assert exc.value.path == "CONTRIBUTING.md"


def test_read_optional_raises_for_a_directory_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response([{"name": "a.md", "type": "blob"}])

    with pytest.raises(GitLabAPIError) as exc:
        _make_fetcher(handler).read_optional(OWNER, REPO, "docs", "basesha")
    assert str(exc.value) == "gitlab API error: 200 — path is a directory, not a file (path: docs)"


def test_read_optional_raises_for_an_encoding_none_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"file_path": "big.md", "encoding": "none", "content": ""})

    with pytest.raises(GitLabAPIError) as exc:
        _make_fetcher(handler).read_optional(OWNER, REPO, "big.md", "basesha")
    assert str(exc.value) == (
        'gitlab API error: 200 — file has no content (encoding "none"); it may be too large (path: big.md)'
    )


def test_project_context_reads_send_only_gets_through_the_hooked_client():
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if "/repository/tree" in request.url.path:
            return _json_response([])
        if "/merge_requests/" in request.url.path:
            return _json_response({"diff_refs": {"head_sha": "h", "base_sha": "b"}})
        return _json_response({"encoding": "base64", "content": _b64("x")})

    client = _make_fetcher(handler)
    assert client.client.event_hooks["request"] == [_reject_writes]
    client.pull_refs(OWNER, REPO, IID)
    client.list_root(OWNER, REPO, "b")
    client.read_optional(OWNER, REPO, "README.md", "b")
    assert methods == ["GET", "GET", "GET"]


def test_list_root_raises_when_x_next_page_repeats_the_page_just_fetched():
    """A server that answers x-next-page: 2 for every page loops; list_root
    stops on page 2 (the repeat) instead of requesting it forever."""
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params["page"])
        return httpx.Response(200, json=[{"name": "a", "type": "blob"}], headers={"x-next-page": "2"})

    with pytest.raises(GitLabAPIError) as exc:
        _make_fetcher(handler).list_root(OWNER, REPO, "basesha")
    assert pages == ["1", "2"]
    assert exc.value.status == 200
    assert str(exc.value) == "gitlab API error: 200 — repository tree listing repeated page 2 (x-next-page)"


def test_list_root_raises_after_exactly_50_pages_when_more_remain():
    """x-next-page keeps advancing; list_root fetches pages 1 to 50 and raises
    because the 50th still names a next page."""
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(
            200, json=[{"name": f"p{page}", "type": "blob"}], headers={"x-next-page": str(page + 1)}
        )

    with pytest.raises(GitLabAPIError) as exc:
        _make_fetcher(handler).list_root(OWNER, REPO, "basesha")
    assert pages == list(range(1, 51))
    assert exc.value.status == 200
    assert str(exc.value) == "gitlab API error: 200 — repository tree listing exceeded 50 pages"


# --- strict decoding for pyproject.toml (T120) ---------------------------------


def _bytes_handler(data: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        content = base64.b64encode(data).decode()
        return _json_response({"file_path": "pyproject.toml", "encoding": "base64", "content": content})

    return handler


def test_read_optional_strict_utf8_drops_a_leading_bom():
    fetcher = _make_fetcher(_bytes_handler(b"\xef\xbb\xbf[project]\n"))
    assert fetcher.read_optional(OWNER, REPO, "pyproject.toml", "h", strict_utf8=True) == "[project]\n"


def test_read_optional_strict_utf8_raises_on_invalid_utf8():
    fetcher = _make_fetcher(_bytes_handler(b'name = "caf\xe9"\n'))
    with pytest.raises(UnicodeDecodeError):
        fetcher.read_optional(OWNER, REPO, "pyproject.toml", "h", strict_utf8=True)


def test_read_optional_default_decoding_is_unchanged():
    """Without strict_utf8 a BOM is kept and invalid bytes are replaced, as before T120."""
    fetcher = _make_fetcher(_bytes_handler(b'\xef\xbb\xbfname = "caf\xe9"\n'))
    assert fetcher.read_optional(OWNER, REPO, "pyproject.toml", "h") == '\ufeffname = "caf\ufffd"\n'


def test_fetcher_names_its_api_error():
    """The scope node catches the fetcher's api_error without importing this module."""
    assert GitLabFetcher.api_error is GitLabAPIError
