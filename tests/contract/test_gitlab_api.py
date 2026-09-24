"""Contract tests — GitLab REST client vs contracts/hosting-api.md (T030)."""

import base64
import json

import httpx
import pytest

from veritas.hosting.gitlab import GitLabAPIError, GitLabClient

BASE = "https://gitlab.com"
OWNER, REPO, IID = "group", "project", 7


def _make_client(handler) -> GitLabClient:
    client = GitLabClient("glpat-test", base_url="https://gitlab.com")
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=f"{BASE}/api/v4",
        headers={"PRIVATE-TOKEN": "glpat-test"},
        timeout=30.0,
    )
    return client


def _json_response(payload, status=200):
    return httpx.Response(status, json=payload)


def test_project_id_url_encoded():
    assert GitLabClient.project_id(OWNER, REPO) == "group%2Fproject"


def test_head_sha_resolved():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v4/projects/{OWNER}/{REPO}/merge_requests/{IID}"
        return _json_response({"diff_refs": {"head_sha": "mrsha"}})

    client = _make_client(handler)
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

    client = _make_client(handler)
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

    client = _make_client(handler)
    assert client.get_file_at_ref(OWNER, REPO, "src/app.py", "mrsha") == content


def test_post_note():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)["body"]
        return _json_response({}, status=201)

    client = _make_client(handler)
    client.post_note(OWNER, REPO, IID, "notes")
    assert captured["body"] == "notes"


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized", request=request)

    client = _make_client(handler)
    with pytest.raises(GitLabAPIError) as exc:
        client.mr_details(OWNER, REPO, IID)
    assert exc.value.status == 401