"""GitHub REST clients (T029, T110, T115) — PR files and refs, contents, root listing, comment posting.

Plain httpx (BSD-3-Clause); bearer-token auth; Link-header pagination; 429
exponential backoff (1s/2s/4s, max 3) per contracts/hosting-api.md.

``GitHubFetcher`` reads (its client refuses every method but GET and HEAD,
through an httpx request event hook); ``GitHubPoster`` posts the report
comment. Both share the request plumbing below.
"""

from __future__ import annotations

import base64
import re
import time

import httpx

from veritas.hosting.types import PullRefs, RootEntry
from veritas.utils.logging import Log

BASE_URL = "https://api.github.com"
MAX_RETRIES = 3
BACKOFF = (1, 2, 4)


class GitHubAPIError(RuntimeError):
    """Fatal hosting API error (contracts/hosting-api.md)."""

    def __init__(self, status: int, detail: str, path: str | None = None) -> None:
        message = f"github API error: {status} — {detail}"
        if path is not None:
            message += f" (path: {path})"
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.path = path


# -- shared request plumbing ---------------------------------------------


def _reject_writes(request: httpx.Request) -> None:
    """Request hook of a read-only client: only GET and HEAD may be sent.

    httpx runs request hooks before the request reaches the transport, so a
    refused request never touches the network. Status 0: no HTTP exchange
    took place (as for network errors).
    """
    method = request.method.upper()
    if method not in ("GET", "HEAD"):
        raise GitHubAPIError(0, f"blocked: {method} on read-only fetcher")


def _new_client(
    token: str, base_url: str, *, read_only: bool, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """The GitHub HTTP client. Production never passes ``transport`` (tests pass
    a MockTransport): giving httpx a transport turns off environment proxies."""
    return httpx.Client(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "veritas",
        },
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
            raise GitHubAPIError(0, f"network error: {exc}", path=path) from exc
        if response.status_code == 429 and attempt < MAX_RETRIES:
            log.warn(f"github API 429; retrying in {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
            continue
        if response.status_code >= 400:
            detail = (response.text or "").strip()[:300] or f"HTTP {response.status_code}"
            raise GitHubAPIError(response.status_code, detail, path=path)
        return response
    raise GitHubAPIError(429, "rate limited after retries", path=path)


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


# -- clients --------------------------------------------------------------


class GitHubFetcher:
    """Read-only GitHub client: PR details and refs, changed files, file
    contents, root listing."""

    # The error every failed request raises, named here so the scope node can
    # catch it without importing this module (T120).
    api_error = GitHubAPIError

    def __init__(self, token: str, log: Log | None = None, *, base_url: str | None = None) -> None:
        self.log = log or Log()
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.client = _new_client(token, self.base_url, read_only=True)

    def _request(self, method: str, url: str, *, path: str | None = None, **kwargs) -> httpx.Response:
        return _request(self.client, self.log, method, url, path=path, **kwargs)

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        current: str | None = url
        while current:
            param_key = params or {}
            parts = current.split("?", 1)
            path = parts[0]
            if len(parts) == 2:
                param_key = {**param_key, **{k: v for k, v in (p.split("=", 1) for p in parts[1].split("&") if "=" in p)}}
            response = self._request("GET", path, params=param_key)
            items.extend(response.json() if isinstance(response.json(), list) else [])
            link = response.headers.get("link", "")
            current = _next_link(link)
        return items

    def pull_details(self, owner: str, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}").json()

    def pull_refs(self, owner: str, repo: str, number: int) -> PullRefs:
        """The PR's head and base commit SHAs (``head.sha``, ``base.sha``)."""
        details = self.pull_details(owner, repo, number)
        return PullRefs(
            head_sha=(details.get("head") or {}).get("sha"),
            base_sha=(details.get("base") or {}).get("sha"),
        )

    def head_sha(self, owner: str, repo: str, number: int) -> str | None:
        return self.pull_refs(owner, repo, number).head_sha

    def list_root(self, owner: str, repo: str, ref: str) -> list[RootEntry]:
        """The files and directories at the repository root at ``ref``.

        One request: GitHub's contents listing is not paginated; it returns the
        whole directory up to its documented limit of 1,000 entries. Entries
        that are neither files nor directories (symlinks, submodules) are left
        out. A file's ``size`` is the listing's byte count; a directory's is None.
        """
        response = self._request("GET", f"/repos/{owner}/{repo}/contents", params={"ref": ref})
        data = response.json()
        if not isinstance(data, list):
            raise GitHubAPIError(response.status_code, "repository root listing is not a directory listing")
        entries: list[RootEntry] = []
        for item in data:
            kind = item.get("type")
            if kind not in ("file", "dir"):
                continue
            size = item.get("size")
            entries.append(
                RootEntry(
                    name=str(item.get("name", "")),
                    type=kind,
                    size=size if kind == "file" and isinstance(size, int) else None,
                )
            )
        return entries

    def read_optional(
        self, owner: str, repo: str, path: str, ref: str, *, strict_utf8: bool = False
    ) -> str | None:
        """A file's text at ``ref``, or None when GitHub answers 404 (absent).

        Every other failure raises ``GitHubAPIError`` unchanged, so "absent"
        and "failed" stay distinguishable. A directory, a non-file entry and a
        file too large for the contents API (encoding ``"none"`` with empty
        content, GitHub's answer for 1-100 MB files) also raise, carrying the
        response's HTTP status, instead of reading as empty text.

        Text is decoded as UTF-8 with invalid bytes replaced. With
        ``strict_utf8`` (T120, for pyproject.toml) it is decoded as
        ``utf-8-sig`` instead, so a leading byte-order mark is dropped and
        invalid UTF-8 raises ``UnicodeDecodeError``, as the local reader does.
        """
        try:
            response = self._request(
                "GET",
                f"/repos/{owner}/{repo}/contents/{path.lstrip('/')}",
                params={"ref": ref},
                path=path,
            )
        except GitHubAPIError as exc:
            if exc.status == 404:
                return None
            raise
        status = response.status_code
        data = response.json()
        if isinstance(data, list):
            raise GitHubAPIError(status, "path is a directory, not a file", path=path)
        if not isinstance(data, dict):
            raise GitHubAPIError(status, "unexpected contents response", path=path)
        kind = data.get("type", "file")
        if kind != "file":
            raise GitHubAPIError(status, f"path is a {kind}, not a file", path=path)
        encoding = data.get("encoding")
        content = data.get("content")
        if encoding == "none":
            raise GitHubAPIError(
                status, 'file too large for the contents API (encoding "none", no content)', path=path
            )
        if encoding != "base64" or not isinstance(content, str):
            raise GitHubAPIError(status, f"unexpected file encoding {encoding!r}", path=path)
        raw = base64.b64decode(content)
        if strict_utf8:
            return raw.decode("utf-8-sig")
        return raw.decode("utf-8", errors="replace")

    def list_pr_files(self, owner: str, repo: str, number: int) -> list[dict]:
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/files", {"per_page": "100"})

    def get_file_contents(self, owner: str, repo: str, path: str, ref: str) -> str:
        response = self._request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{path.lstrip('/')}",
            params={"ref": ref},
            path=path,
        )
        data = response.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        return str(data.get("content", ""))

    def close(self) -> None:
        self.client.close()


class GitHubPoster:
    """Posts the report as a PR (issue) comment."""

    def __init__(self, token: str, log: Log | None = None, *, base_url: str | None = None) -> None:
        self.log = log or Log()
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.client = _new_client(token, self.base_url, read_only=False)

    def post_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        _request(self.client, self.log, "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})

    def close(self) -> None:
        self.client.close()
