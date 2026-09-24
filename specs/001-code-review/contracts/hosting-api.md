# Hosting API Contracts: GitHub & GitLab

**Date**: 2026-09-16

Minimal REST surface for PR fetching (FR-002) and comment posting. All calls use `httpx` (BSD-3-Clause) with bearer-token auth.

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
