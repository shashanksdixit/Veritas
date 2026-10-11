"""Integration tests — the input revision of a local review (T101, FR-024).

Local scopes record "sha256:" + the hex SHA-256 of the reviewed files after
exclusion (sorted by path; path, NUL, content, NUL per file). PR scope keeps
the head commit SHA.
"""

import hashlib
import re
from pathlib import Path

from tests.conftest import default_fake_llm

from veritas.config.constants import LAST_REPORT_JSON
from veritas.models.entities import Report, ReviewScope
from veritas.review.graph import run_review
from veritas.review.nodes.scope import _run_local, local_input_revision

REVISION_SHAPE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "src" / "util.py").write_text("X = 2\n", encoding="utf-8")
    (root / "vendor").mkdir()
    (root / "vendor" / "lib.py").write_text("Y = 3\n", encoding="utf-8")
    return root


def test_digest_follows_the_specified_construction():
    files = {"b.py": "B\n", "a.py": "A\n"}
    expected = hashlib.sha256(b"a.py\0A\n\0b.py\0B\n\0").hexdigest()
    assert local_input_revision(files) == f"sha256:{expected}"


def test_identical_file_sets_give_identical_revisions_regardless_of_dict_order():
    forward = {"src/a.py": "a = 1\n", "src/b.py": "b = 2\n", "spec.md": "# REQ-1\n"}
    backward = dict(reversed(list(forward.items())))
    assert list(forward) != list(backward)
    assert local_input_revision(forward) == local_input_revision(backward)
    assert REVISION_SHAPE.match(local_input_revision(forward))


def test_path_and_content_boundaries_are_unambiguous():
    # Without the NUL separators these two inputs would hash the same bytes.
    assert local_input_revision({"ab": "c"}) != local_input_revision({"a": "bc"})


def test_changing_one_file_changes_the_revision(tmp_path):
    root = _project(tmp_path / "proj")
    before = _run_local(ReviewScope.PROJECT, str(root), [])["input_revision"]

    (root / "src" / "util.py").write_text("X = 3\n", encoding="utf-8")
    after = _run_local(ReviewScope.PROJECT, str(root), [])["input_revision"]

    assert REVISION_SHAPE.match(before) and REVISION_SHAPE.match(after)
    assert before != after


def test_changing_an_excluded_file_does_not_change_the_revision(tmp_path):
    root = _project(tmp_path / "proj")
    first = _run_local(ReviewScope.PROJECT, str(root), ["vendor/"])

    (root / "vendor" / "lib.py").write_text("Y = 'changed'\n", encoding="utf-8")
    second = _run_local(ReviewScope.PROJECT, str(root), ["vendor/"])

    assert [e.path for e in first["excluded_files"]] == ["vendor/lib.py"]
    assert first["input_revision"] == second["input_revision"]
    # The same edit with nothing excluded does change it.
    unexcluded = _run_local(ReviewScope.PROJECT, str(root), [])["input_revision"]
    assert unexcluded != second["input_revision"]


def test_file_scope_revision_covers_the_single_file(tmp_path):
    target = tmp_path / "solo.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = _run_local(ReviewScope.FILE, str(target), [])
    assert result["input_revision"] == local_input_revision(result["files"])
    assert REVISION_SHAPE.match(result["input_revision"])


def test_revision_appears_in_the_report_header(sample_project, settings):
    expected = _run_local(ReviewScope.PROJECT, str(sample_project), settings.exclude)[
        "input_revision"
    ]
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm())

    assert outcome.exit_code == 0
    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert f"- **Input revision**: `{expected}`" in markdown
    report = Report.model_validate_json(Path(LAST_REPORT_JSON).read_text(encoding="utf-8"))
    assert report.run.input_revision == expected


def test_pr_scope_keeps_the_head_commit_sha(monkeypatch, settings):
    from veritas.review.nodes import scope as scope_module

    class Host:
        provider = "github"

        def head_sha(self, owner, repo, number):
            return "abcd1234"

        def list_pr_files(self, owner, repo, number):
            return [{"filename": "src/app.py"}]

        def get_file_contents(self, owner, repo, path, ref):
            return "def f():\n    return 1\n"

    monkeypatch.setattr(scope_module, "build_hosting_client", lambda _s, _p, _l: scope_module.HostingClients(Host(), Host()))
    outcome = run_review(settings, ReviewScope.PR, "acme/widget#3", llm=default_fake_llm())

    markdown = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert "- **Input revision**: `abcd1234`" in markdown
