"""Unit tests — PR ref resolution + local scope walking (T031/T033/T047)."""

import pytest

from veritas.hosting.resolver import UnresolvableTarget, parse_pr_target
from veritas.review import ReviewNotFoundError
from veritas.review.nodes.scope import _normalize_rel, _run_local, _walk_local_scope
from veritas.config.constants import MAX_SCOPE_FILES


def _normalize_rel_target(target: str) -> str:
    return _normalize_rel(target)


def test_github_short_form():
    parsed = parse_pr_target("owner/repo#123")
    assert parsed.provider == "github"
    assert (parsed.owner, parsed.repo, parsed.number) == ("owner", "repo", 123)
    assert parsed.ref == "owner/repo#123"


def test_gitlab_short_form():
    parsed = parse_pr_target("group/project!42")
    assert parsed.provider == "gitlab"
    assert (parsed.owner, parsed.repo, parsed.number) == ("group", "project", 42)
    assert parsed.ref == "group/project!42"


def test_github_url_form():
    parsed = parse_pr_target("https://github.com/owner/repo/pull/7")
    assert parsed.number == 7


def test_gitlab_url_form():
    parsed = parse_pr_target("https://gitlab.com/group/project/-/merge_requests/9")
    assert parsed.provider == "gitlab"
    assert parsed.number == 9
    assert parsed.repo == "project"


def test_bare_number_rejected():
    with pytest.raises(UnresolvableTarget):
        parse_pr_target("123")


def test_garbage_rejected():
    with pytest.raises(UnresolvableTarget):
        parse_pr_target("not-a-ref")


@pytest.fixture
def scope_tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("const x = 1;", encoding="utf-8")
    (tmp_path / "src" / "c.cob").write_text("IDENTIFICATION DIVISION.", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "vendored.md").write_text("docs", encoding="utf-8")
    return tmp_path


def test_walk_collects_only_supported_sources(scope_tree):
    files, skipped = _walk_local_scope(str(scope_tree), "project")
    assert set(files) == {"src/a.py", "src/b.ts"}
    assert "src/c.cob" in skipped
    assert "vendored.md" in skipped
    assert not any("node_modules" in f for f in files)


def test_walk_missing_dir_raises(tmp_path):
    with pytest.raises(ReviewNotFoundError):
        _walk_local_scope(str(tmp_path / "nope"), "project")


def test_file_scope_reads_single_file(scope_tree):
    target = str(scope_tree / "src" / "a.py")
    result = _run_local("file", target)
    assert result["files"][_normalize_rel_target(target)] == "x = 1"
    assert result["input_revision"] is None


def test_file_scope_missing_raises(scope_tree):
    with pytest.raises(ReviewNotFoundError):
        _run_local("file", str(scope_tree / "src" / "missing.py"))


def test_unsupported_file_scope_skipped_note(scope_tree):
    target = str(scope_tree / "vendored.md")
    result = _run_local("file", target)
    assert result["files"] == {_normalize_rel_target(target): "docs"}
    assert _normalize_rel_target(target) in result["skipped_languages"]