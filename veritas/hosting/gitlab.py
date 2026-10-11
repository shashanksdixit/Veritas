"""GitLab REST clients (T030, T110) — MR changes, file-at-ref, MR notes.

Plain httpx; ``PRIVATE-TOKEN`` auth; project id is the URL-encoded path
(``group%2Fproject``); instance URL configurable via ``VERITAS_GITLAB_URL``.

``GitLabFetcher`` reads (its client refuses every method but GET and HEAD,
through an httpx request event hook); ``GitLabPoster`` posts the report note.
Both share the request plumbing below.
"""

from __future__ import annotations

import base64
import time
from urllib.parse import quote

import httpx

from veritas.utils.logging import Log

DEFAULT_URL = "https://gitlab.com"
MAX_RETRIES = 3
BACKOFF = (1, 2, 4)


class GitLabAPIError(RuntimeError):
    def __init__(self, status: int, detail: str, path: str | None = None) -> None:
        message = f"gitlab API error: {status} — {detail}"
        if path is not None:
            message += f" (path: {path})"
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.path = path


# -- shared request plumbing ---------------------------------------------


def project_id(owner: str, repo: str) -> str:
    return quote(f"{owner}/{repo}", safe="")


def _api_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v4"


def _reject_writes(request: httpx.Request) -> None:
    """Request hook of a read-only client: only GET and HEAD may be sent.

    httpx runs request hooks before the request reaches the transport, so a
    refused request never touches the network. Status 0: no HTTP exchange
    took place (as for network errors).
    """
    method = request.method.upper()
    if method not in ("GET", "HEAD"):
        raise GitLabAPIError(0, f"blocked: {method} on read-only fetcher")


def _new_client(
    token: str, base_url: str, *, read_only: bool, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """The GitLab HTTP client; ``base_url`` is the ``.../api/v4`` URL. Production
    never passes ``transport`` (tests pass a MockTransport): giving httpx a
    transport turns off environment proxies."""
    return httpx.Client(
        base_url=base_url,
        headers={"PRIVATE-TOKEN": token, "User-Agent": "veritas"},
        timeout=30.0,
        event_hooks={"request": [_reject_writes]} if read_only else None,
        transport=transport,
    )


def _request(
    client: httpx.Client, log: Log, method: str, url: str, *, path: str | None = None, **kwargs
) -> httpx.Response:
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise GitLabAPIError(0, f"network error: {exc}", path=path) from exc
        if response.status_code == 429 and attempt < MAX_RETRIES:
            log.warn(f"gitlab API 429; retrying in {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
            continue
        if response.status_code >= 400:
            detail = (response.text or "").strip()[:300] or f"HTTP {response.status_code}"
            raise GitLabAPIError(response.status_code, detail, path=path)
        return response
    raise GitLabAPIError(429, "rate limited after retries", path=path)


# -- clients --------------------------------------------------------------


class GitLabFetcher:
    """Read-only GitLab client: MR details, changes, file at ref."""

    def __init__(self, token: str, log: Log | None = None, *, base_url: str = DEFAULT_URL) -> None:
        self.log = log or Log()
        self.base_url = _api_url(base_url)
        self.client = _new_client(token, self.base_url, read_only=True)

    project_id = staticmethod(project_id)

    def _request(self, method: str, url: str, *, path: str | None = None, **kwargs) -> httpx.Response:
        return _request(self.client, self.log, method, url, path=path, **kwargs)

    def mr_details(self, owner: str, repo: str, iid: int) -> dict:
        pid = self.project_id(owner, repo)
        return self._request("GET", f"/projects/{pid}/merge_requests/{iid}").json()

    def head_sha(self, owner: str, repo: str, iid: int) -> str | None:
        details = self.mr_details(owner, repo, iid)
        return (details.get("diff_refs") or {}).get("head_sha")

    def list_mr_changes(self, owner: str, repo: str, iid: int) -> list[dict]:
        pid = self.project_id(owner, repo)
        data = self._request("GET", f"/projects/{pid}/merge_requests/{iid}/changes").json()
        return data.get("changes", [])

    def get_file_at_ref(self, owner: str, repo: str, path: str, ref: str) -> str:
        pid = self.project_id(owner, repo)
        encoded = quote(path, safe="")
        response = self._request(
            "GET",
            f"/projects/{pid}/repository/files/{encoded}",
            params={"ref": ref},
            path=path,
        )
        data = response.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        return str(data.get("content", ""))

    def close(self) -> None:
        self.client.close()


class GitLabPoster:
    """Posts the report as an MR note."""

    def __init__(self, token: str, log: Log | None = None, *, base_url: str = DEFAULT_URL) -> None:
        self.log = log or Log()
        self.base_url = _api_url(base_url)
        self.client = _new_client(token, self.base_url, read_only=False)

    def post_note(self, owner: str, repo: str, iid: int, body: str) -> None:
        pid = project_id(owner, repo)
        _request(self.client, self.log, "POST", f"/projects/{pid}/merge_requests/{iid}/notes", json={"body": body})

    def close(self) -> None:
        self.client.close()
