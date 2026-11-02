"""GitLab REST clients (T030, T110, T115) — MR changes and refs, file-at-ref, root tree, MR notes.

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

from veritas.hosting.types import PullRefs, RootEntry
from veritas.review import ContentWithheldError
from veritas.utils.logging import Log

DEFAULT_URL = "https://gitlab.com"
MAX_RETRIES = 3
BACKOFF = (1, 2, 4)
MAX_TREE_PAGES = 50  # list_root's bound: 50 pages of 100 root entries


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
    """Read-only GitLab client: MR details and refs, changes, file at ref,
    root listing."""

    # The error every failed request raises, named here so the scope node can
    # catch it without importing this module (T120).
    api_error = GitLabAPIError
    # What get_file_at_ref raises when GitLab withholds a file's content
    # (T124); the scope node records the file as excluded.
    content_withheld = ContentWithheldError

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

    def pull_refs(self, owner: str, repo: str, iid: int) -> PullRefs:
        """The MR's head and base commit SHAs (``diff_refs.head_sha``,
        ``diff_refs.base_sha``)."""
        refs = self.mr_details(owner, repo, iid).get("diff_refs") or {}
        return PullRefs(head_sha=refs.get("head_sha"), base_sha=refs.get("base_sha"))

    def head_sha(self, owner: str, repo: str, iid: int) -> str | None:
        return self.pull_refs(owner, repo, iid).head_sha

    def list_root(self, owner: str, repo: str, ref: str) -> list[RootEntry]:
        """The files and directories at the repository root at ``ref``.

        Pages through GitLab's tree API at ``per_page=100`` (offset
        pagination, GitLab's default), asking for the page named by each
        response's ``x-next-page`` header until it is empty. The next request is
        built on this client's own URL, so the token never follows a host named
        by the server. Pagination is bounded: ``GitLabAPIError`` (with the last
        response's status) is raised when ``x-next-page`` names the page just
        fetched, or is still set after ``MAX_TREE_PAGES`` pages.

        ``blob`` entries are files and ``tree`` entries are directories;
        anything else (submodule ``commit`` entries) is left out.

        ``size`` is always None: GitLab's tree API returns only ``id``,
        ``name``, ``type``, ``path`` and ``mode`` per entry, never a size, so a
        size limit has to be applied after a file is fetched.
        """
        pid = self.project_id(owner, repo)
        url = f"/projects/{pid}/repository/tree"
        entries: list[RootEntry] = []
        page = "1"
        fetched = 0
        while True:
            response = self._request("GET", url, params={"ref": ref, "per_page": "100", "page": page})
            fetched += 1
            data = response.json()
            if not isinstance(data, list):
                raise GitLabAPIError(response.status_code, "repository tree listing is not a list")
            for item in data:
                kind = {"blob": "file", "tree": "dir"}.get(item.get("type"))
                if kind is not None:
                    entries.append(RootEntry(name=str(item.get("name", "")), type=kind, size=None))
            next_page = response.headers.get("x-next-page", "").strip()
            if not next_page:
                return entries
            if next_page == page:
                raise GitLabAPIError(
                    response.status_code, f"repository tree listing repeated page {page} (x-next-page)"
                )
            if fetched >= MAX_TREE_PAGES:
                raise GitLabAPIError(
                    response.status_code, f"repository tree listing exceeded {MAX_TREE_PAGES} pages"
                )
            page = next_page

    def read_optional(
        self, owner: str, repo: str, path: str, ref: str, *, strict_utf8: bool = False
    ) -> str | None:
        """A file's text at ``ref``, or None when GitLab answers 404 (absent).

        Every other failure raises ``GitLabAPIError`` unchanged, so "absent"
        and "failed" stay distinguishable. A response that is not a file
        object (a list, as for a directory) or has no base64 content (encoding
        ``"none"`` or anything else) also raises, carrying the response's HTTP
        status, instead of reading as empty text.

        Text is decoded as UTF-8 with invalid bytes replaced. With
        ``strict_utf8`` (T120, for pyproject.toml) it is decoded as
        ``utf-8-sig`` instead, so a leading byte-order mark is dropped and
        invalid UTF-8 raises ``UnicodeDecodeError``, as the local reader does.
        """
        pid = self.project_id(owner, repo)
        try:
            response = self._request(
                "GET",
                f"/projects/{pid}/repository/files/{quote(path, safe='')}",
                params={"ref": ref},
                path=path,
            )
        except GitLabAPIError as exc:
            if exc.status == 404:
                return None
            raise
        status = response.status_code
        data = response.json()
        if isinstance(data, list):
            raise GitLabAPIError(status, "path is a directory, not a file", path=path)
        if not isinstance(data, dict):
            raise GitLabAPIError(status, "unexpected file response", path=path)
        encoding = data.get("encoding")
        content = data.get("content")
        if encoding == "none":
            raise GitLabAPIError(status, 'file has no content (encoding "none"); it may be too large', path=path)
        if encoding != "base64" or not isinstance(content, str):
            raise GitLabAPIError(status, f"unexpected file encoding {encoding!r}", path=path)
        raw = base64.b64decode(content)
        if strict_utf8:
            return raw.decode("utf-8-sig")
        return raw.decode("utf-8", errors="replace")

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
        encoding = data.get("encoding")
        content = data.get("content") or ""
        size = data.get("size")
        # T124: GitLab's documentation does not say what this endpoint returns
        # for a large file (contracts/hosting-api.md). Empty content for a
        # blob GitLab says is not empty, or encoding "none" with empty
        # content, is withheld content, never an empty file.
        if not content and encoding == "none":
            raise ContentWithheldError(
                path, 'content not provided by the hosting API (encoding "none")'
            )
        if not content and isinstance(size, int) and size > 0:
            raise ContentWithheldError(
                path, f"content not provided by the hosting API ({size}-byte file returned empty)"
            )
        if encoding == "base64":
            return base64.b64decode(content).decode("utf-8", errors="replace")
        return str(content)

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
