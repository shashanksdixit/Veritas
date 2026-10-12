# Hosting API Contracts: GitHub & GitLab

**Date**: 2026-09-16

Minimal REST surface for PR fetching (FR-002) and comment posting. All calls use `httpx` (BSD-3-Clause) with bearer-token auth.

## Client roles: fetcher and poster

Each provider has two clients, built from the same token:

| Provider | Fetcher (read-only) | Poster |
|----------|---------------------|--------|
| GitHub | `GitHubFetcher`: `pull_details`, `pull_refs`, `head_sha`, `list_pr_files`, `get_file_contents`, `list_root`, `read_optional`, `close` | `GitHubPoster`: `post_comment`, `close` |
| GitLab | `GitLabFetcher`: `project_id`, `mr_details`, `pull_refs`, `head_sha`, `list_mr_changes`, `get_file_at_ref`, `list_root`, `read_optional`, `close` | `GitLabPoster`: `post_note`, `close` |

**GET-only request hook**: a fetcher's HTTP client allows only `GET` and `HEAD`. Its `httpx` request event hook (`_reject_writes`) runs before the request reaches the transport, so any other method fails before any network I/O with the provider's API error, status 0: `github API error: 0 — blocked: POST on read-only fetcher` (`gitlab API error: ...` for GitLab). Apart from the hook, fetcher and poster clients are the same `httpx.Client(base_url=..., headers=..., timeout=30.0)` with no `transport`, so certificate verification (`verify=True`, honouring `SSL_CERT_FILE` / `SSL_CERT_DIR`) and environment proxies (`HTTP(S)_PROXY`, `ALL_PROXY`, `NO_PROXY`) are httpx's defaults for both. (Passing `transport=` would turn environment proxies off; only tests pass one.)

Both clients share the retry and error behaviour in "Error Handling Contract" below.

### Project-context reads (T115)

Both fetchers offer the same three project-context methods, all plain `GET` requests through the hooked client. They return the provider-neutral values in `veritas/hosting/types.py`:

| Method | Returns | Meaning |
|--------|---------|---------|
| `pull_refs(owner, repo, number)` | `PullRefs(head_sha, base_sha)` | The PR/MR's head and base commit SHAs. `head_sha(...)` is now `pull_refs(...).head_sha`, unchanged for its callers. |
| `list_root(owner, repo, ref)` | `list[RootEntry(name, type, size)]` | The repository root at `ref`. Only the root is listed; `type` is `"file"` or `"dir"`, and other entries (symlinks, submodules) are left out. `size` is the file's byte count where the provider reports it, else `None`; it is always `None` for directories. |
| `read_optional(owner, repo, path, ref)` | `str` or `None` | The file's text at `ref`. **`None` means absent and is returned only for HTTP 404.** Every other HTTP or network error raises the provider's API error unchanged (for example status 502), so a caller can tell "absent" from "failed". A response that is not one file with base64 content (a directory listing, a non-file entry, encoding `"none"`) raises the provider's API error with the response's HTTP status and a detail naming the problem, never returning `""`. |

No caller uses these methods yet.

---

## GitHub REST API v3

**Base URL**: `https://api.github.com`
**Auth**: `Authorization: Bearer <VERITAS_GITHUB_TOKEN>`

### List PR files

```
GET /repos/{owner}/{repo}/pulls/{pull_number}/files
```

Response (array of):
```jsonc
{
  "filename": "src/app.py",        // path in repo
  "status": "modified",            // added|removed|modified|renamed
  "additions": 12,
  "deletions": 5,
  "sha": "abc123..."               // blob SHA at PR head
}
```

**Pagination**: `Link` header; follow `rel="next"` until absent. Max 100 per page (`?per_page=100`).

### Get file contents at ref

```
GET /repos/{owner}/{repo}/contents/{path}?ref={sha}
```

Response:
```jsonc
{
  "encoding": "base64",
  "content": "<base64-encoded file content>",
  "name": "app.py",
  "path": "src/app.py",
  "sha": "abc123..."
}
```

**Error handling**: 404 → file not found or inaccessible; 403 → access denied; 422 → invalid ref.

**`read_optional`**: 404 → `None` (absent); every other error raises. A directory path returns a JSON array, which raises `path is a directory, not a file`. A file of 1–100 MB comes back with `"encoding": "none"` and empty `content`, which raises `file too large for the contents API (encoding "none", no content)`. A `symlink` or `submodule` entry raises `path is a <type>, not a file`.

### List repository root (`list_root`)

```
GET /repos/{owner}/{repo}/contents?ref={sha}
```

Returns an array with one object per root entry (`name`, `type`, `size`, ...). **No pagination**: GitHub documents no `page`/`per_page` for this endpoint and an upper limit of 1,000 entries per directory, so `list_root` makes exactly one request. `size` is taken for `file` entries; `dir` entries get `None`.

### Get PR refs (`pull_refs`)

```
GET /repos/{owner}/{repo}/pulls/{pull_number}
```

`head.sha` → `head_sha`, `base.sha` → `base_sha`.

### Post issue comment (for report posting)

```
POST /repos/{owner}/{repo}/issues/{pull_number}/comments
```

Body:
```jsonc
{
  "body": "<markdown report content>"
}
```

---

## GitLab REST API v4

**Base URL**: `https://gitlab.com/api/v4` (configurable via `VERITAS_GITLAB_URL`)
**Auth**: `PRIVATE-TOKEN: <VERITAS_GITLAB_TOKEN>`

### List MR changes (files + diffs)

```
GET /projects/{id}/merge_requests/{merge_request_iid}/changes
```

Response includes `changes[]`:
```jsonc
{
  "changes": [
    {
      "old_path": "src/app.py",
      "new_path": "src/app.py",
      "new_file": false,
      "renamed_file": false,
      "deleted_file": false,
      "diff": "@@ -1,5 +1,7 @@ ..."
    }
  ]
}
```

**Note**: GitLab bundles file diffs in this call — no separate "get file at ref" is strictly needed for diff-based review. For full-file context at MR head, use the repository files API:

### Get file at ref

```
GET /projects/{id}/repository/files/{url_encoded_path}?ref={sha}
```

Response:
```jsonc
{
  "file_name": "app.py",
  "file_path": "src/app.py",
  "encoding": "base64",
  "content": "<base64-encoded file content>"
}
```

**`read_optional`**: 404 → `None` (absent); every other error raises. A response that is a JSON array (a directory) raises `path is a directory, not a file`; a missing or non-base64 encoding raises (`file has no content (encoding "none"); it may be too large` for `"none"`). The path is URL-encoded as one segment (`docs%2FSTYLE.md`).

### List repository root (`list_root`)

```
GET /projects/{id}/repository/tree?ref={sha}&per_page=100&page={n}
```

Each entry carries `id`, `name`, `type` (`blob`, `tree`, or `commit` for a submodule), `path` and `mode`. `blob` → `"file"`, `tree` → `"dir"`, and `commit` entries are left out.

**No size (verified)**: GitLab's tree API returns no file size. The official "List all repository trees in a project" docs (`doc/api/repositories.md`, docs.gitlab.com/api/repositories) list only `id`, `name`, `type`, `path`, `mode` per entry (plus `last_commit` with `with_last_commit=true`); only the blob endpoints return `size`. So `list_root` returns `size=None` for every GitLab entry, and a size limit has to be applied after a file is fetched.

**Pagination**: offset pagination (GitLab's default; keyset exists for this endpoint only from GitLab 17.1). The default page size is 20, so `list_root` asks for `per_page=100` and keeps requesting `page=<x-next-page>` until the `x-next-page` header is empty. Each request goes to the fetcher's own URL with only `page` changed. A `Link` header is never followed, so the `PRIVATE-TOKEN` cannot be sent to a host the server names. Pagination is bounded by `MAX_TREE_PAGES` (50 pages, 5,000 entries): if `x-next-page` is still set after the 50th page, `list_root` raises `repository tree listing exceeded 50 pages`; if `x-next-page` names the page just fetched (a looping server), it raises `repository tree listing repeated page <n> (x-next-page)`. Both are `GitLabAPIError` with the last response's status.

### Get MR refs (`pull_refs`)

```
GET /projects/{id}/merge_requests/{merge_request_iid}
```

`diff_refs.head_sha` → `head_sha`, `diff_refs.base_sha` → `base_sha`.

### Post MR note (for report posting)

```
POST /projects/{id}/merge_requests/{merge_request_iid}/notes
```

Body:
```jsonc
{
  "body": "<markdown report content>"
}
```

---

## PR Reference Parsing

The `--target` argument for `--scope pr` is parsed as:

| Pattern | Parsed as |
|---------|-----------|
| `owner/repo#123` | GitHub PR #123 |
| `https://github.com/owner/repo/pull/123` | GitHub PR #123 |
| `group/project!123` | GitLab MR #123 (matches GitLab's own MR-reference syntax) |
| `https://gitlab.com/owner/repo/-/merge_requests/123` | GitLab MR #123 |

A bare number (e.g. `123`) is **rejected** with exit code 1 and a clear stderr diagnostic: a bare number cannot be resolved to a specific project without either a config-level default project or reading local git state, both of which are out of scope. The full reference — `owner/repo#N` or a full PR URL for GitHub; `group/project!N` or a full MR URL for GitLab — is always required; a bare number is always ambiguous.

---

## Error Handling Contract

All hosting API errors produce:
1. Exit code 1 (fatal)
2. Diagnostic message on stderr: `[error] {provider} API error: {status} — {detail}`
3. No partial report emitted (FR-002 edge case: PR not found / access denied → clear failure, no misrepresentation)

Rate limiting (HTTP 429): retry with exponential backoff (max 3 retries, 1s/2s/4s delays). If all retries exhausted, treat as fatal.
