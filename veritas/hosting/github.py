"""GitHub REST client (T029) — PR files, contents, comment posting.

Plain httpx (BSD-3-Clause); bearer-token auth; Link-header pagination; 429
exponential backoff (1s/2s/4s, max 3) per contracts/hosting-api.md.
"""

from __future__ import annotations

import base64
import re
import time

import httpx

from veritas.utils.logging import Log


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


class GitHubClient:
    BASE_URL = "https://api.github.com"
    MAX_RETRIES = 3
    BACKOFF = (1, 2, 4)

    def __init__(self, token: str, log: Log | None = None, *, base_url: str | None = None) -> None:
        self.log = log or Log()
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "veritas",
            },
            timeout=30.0,
        )

    # -- request plumbing -------------------------------------------------

    def _request(self, method: str, url: str, *, path: str | None = None, **kwargs) -> httpx.Response:
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise GitHubAPIError(0, f"network error: {exc}", path=path) from exc
            if response.status_code == 429 and attempt < self.MAX_RETRIES:
                self.log.warn(f"github API 429; retrying in {self.BACKOFF[attempt]}s")
                time.sleep(self.BACKOFF[attempt])
                continue
            if response.status_code >= 400:
                detail = (response.text or "").strip()[:300] or f"HTTP {response.status_code}"
                raise GitHubAPIError(response.status_code, detail, path=path)
            return response
        raise GitHubAPIError(429, "rate limited after retries", path=path)

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
            current = self._next_link(link)
        return items

    @staticmethod
    def _next_link(link_header: str) -> str | None:
        for part in link_header.split(","):
            match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
            if match:
                return match.group(1)
        return None

    # -- endpoints --------------------------------------------------------

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

    def post_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self._request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})

    def close(self) -> None:
        self.client.close()