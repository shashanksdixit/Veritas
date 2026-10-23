"""Integration tests — review artifacts are purgeable (T102, FR-030).

A review stores artifacts only in the report file and .veritas/last-report.json
(the suppression store is written by suppress/unsuppress, never by a review),
and removes every temporary file it created before it ends.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.conftest import APP_CONTENT, default_fake_llm

from veritas.models.entities import ReviewScope
from veritas.review.graph import run_review
from veritas.security import opengrep
from veritas.security.opengrep import OpengrepResult

REPO_ROOT = Path(__file__).resolve().parents[2]


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every entry under ``root``: file -> its bytes, directory -> None."""
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in root.rglob("*")
    }


def test_review_leaves_only_the_report_and_its_json_copy(tmp_path, monkeypatch, settings):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text(APP_CONTENT, encoding="utf-8")
    (project / "spec.md").write_text("# REQ-1\n\n- SATISFIED: x.\n", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    # Python's temporary directory, redirected so it can be listed too.
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    # Run the real collect_sast (the autouse fixture stubs it out) so the SAST
    # temporary directory is really created and filled. Only the binary is
    # faked: it records the directory it was given and what it held.
    monkeypatch.setattr("veritas.review.nodes.scope.collect_sast", opengrep.collect_sast)
    monkeypatch.setattr(opengrep.shutil, "which", lambda _name: "/fake/opengrep")
    scanned: list[tuple[Path, list[str]]] = []

    def fake_run_opengrep(target_dir, *, rules=None, opengrep_bin="opengrep", **_):
        root = Path(target_dir)
        scanned.append(
            (root, sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()))
        )
        return OpengrepResult(findings=[], rules=rules or "p/owasp-top-ten")

    monkeypatch.setattr(opengrep, "run_opengrep", fake_run_opengrep)

    project_before = _snapshot(project)
    workdir_before = _snapshot(workdir)
    temp_before = _snapshot(temp_root)

    outcome = run_review(settings, ReviewScope.PROJECT, str(project), llm=default_fake_llm())

    assert outcome.exit_code == 0
    # The SAST step really ran in a temporary directory under temp_root ...
    ((sast_dir, sast_files),) = scanned
    assert sast_dir.name.startswith("veritas-sast-")
    assert sast_dir.parent == temp_root
    assert "src/app.py" in sast_files
    # ... and that directory is gone, with nothing else left in the temp root.
    assert not sast_dir.exists()
    assert _snapshot(temp_root) == temp_before
    assert not list(temp_root.glob("veritas-sast-*"))

    # The reviewed project is untouched: same entries, same bytes.
    assert _snapshot(project) == project_before

    # The working directory gains the report and .veritas/last-report.json only.
    report_name = Path(str(outcome.report_path)).name
    assert report_name.startswith("veritas-report-") and report_name.endswith(".md")
    workdir_after = _snapshot(workdir)
    new_entries = set(workdir_after) - set(workdir_before)
    assert new_entries == {report_name, ".veritas", ".veritas/last-report.json"}
    assert set(workdir_before) <= set(workdir_after)
    assert not (workdir / ".veritas" / "suppressions.json").exists()

    # Deleting those files purges every stored review artifact.
    (workdir / report_name).unlink()
    (workdir / ".veritas" / "last-report.json").unlink()
    assert set(_snapshot(workdir)) - set(workdir_before) == {".veritas"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_last_report_json_is_gitignored_but_committable_veritas_files_are_not():
    def ignored(path: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", path],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        assert result.returncode in (0, 1), result.stderr  # 128 means git failed
        return result.returncode == 0

    assert ignored(".veritas/last-report.json")
    # Only that file: config.toml and suppressions.json may be committed deliberately.
    assert not ignored(".veritas/config.toml")
    assert not ignored(".veritas/suppressions.json")
