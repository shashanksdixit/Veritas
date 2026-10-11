"""GitHub REST clients (T029, T110) — PR files, contents, comment posting.

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
    """Read-only GitHub client: PR details, changed files, file contents."""

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

    def head_sha(self, owner: str, repo: str, number: int) -> str | None:
        details = self.pull_details(owner, repo, number)
        return (details.get("head") or {}).get("sha")

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
