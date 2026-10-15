"""Unit tests — suppression fingerprint determinism (T021)."""

from veritas.models.entities import compute_fingerprint


def test_same_input_same_hash():
    fp1 = compute_fingerprint("src/app.py", "security", "subprocess.call(cmd, shell=True)")
    fp2 = compute_fingerprint("src/app.py", "security", "subprocess.call(cmd, shell=True)")
    assert fp1 == fp2
    assert len(fp1) == 64


def test_unrelated_whitespace_edits_same_hash():
    a = compute_fingerprint("a.py", "code_quality", "def f():\n    return 1\n")
    b = compute_fingerprint("a.py", "code_quality", "def f():    \n   return 1\n\n")
    assert a == b


def test_changed_snippet_different_hash():
    a = compute_fingerprint("a.py", "code_quality", "def f():\n    return 1")
    b = compute_fingerprint("a.py", "code_quality", "def f():\n    return 2")
    assert a != b


def test_file_or_category_change_different_hash():
    base = "def f():\n    return 1"
    assert compute_fingerprint("a.py", "code_quality", base) != compute_fingerprint("b.py", "code_quality", base)
    assert compute_fingerprint("a.py", "code_quality", base) != compute_fingerprint("a.py", "security", base)