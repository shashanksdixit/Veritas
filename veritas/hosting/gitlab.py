"""GitLab REST client (T030) — MR changes, file-at-ref, MR notes.

Plain httpx; ``PRIVATE-TOKEN`` auth; project id is the URL-encoded path
(``group%2Fproject``); instance URL configurable via ``VERITAS_GITLAB_URL``.
"""

from __future__ import annotations

import base64
import time
from urllib.parse import quote

import httpx

from veritas.utils.logging import Log


class GitLabAPIError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"gitlab API error: {status} — {detail}")
        self.status = status
        self.detail = detail


class GitLabClient:
    MAX_RETRIES = 3
    BACKOFF = (1, 2, 4)

    def __init__(self, token: str, log: Log | None = None, *, base_url: str = "https://gitlab.com") -> None:
        self.log = log or Log()
        self.base_url = f"{base_url.rstrip('/')}/api/v4"
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"PRIVATE-TOKEN": token, "User-Agent": "veritas"},
            timeout=30.0,
        )

    @staticmethod
    def project_id(owner: str, repo: str) -> str:
        return quote(f"{owner}/{repo}", safe="")

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise GitLabAPIError(0, f"network error: {exc}") from exc
            if response.status_code == 429 and attempt < self.MAX_RETRIES:
                self.log.warn(f"gitlab API 429; retrying in {self.BACKOFF[attempt]}s")
                time.sleep(self.BACKOFF[attempt])
                continue
            if response.status_code >= 400:
                detail = (response.text or "").strip()[:300] or f"HTTP {response.status_code}"
                raise GitLabAPIError(response.status_code, detail)
            return response
        raise GitLabAPIError(429, "rate limited after retries")

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
        )
        data = response.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        return str(data.get("content", ""))

    def post_note(self, owner: str, repo: str, iid: int, body: str) -> None:
        pid = self.project_id(owner, repo)
        self._request("POST", f"/projects/{pid}/merge_requests/{iid}/notes", json={"body": body})

    def close(self) -> None:
        self.client.close()