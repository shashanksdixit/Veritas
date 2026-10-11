"""Unit tests — cited-snippet line-number prefix stripping (T080, FR-014).

Review code is shown as ``{n:>5}| {line}`` (FR-014), so a model asked to quote
the offending code often quotes the prefix too::

    "   21| def f():"

That prefix is not in the file, so it fails verification for a real citation.
``strip_line_number_prefixes`` removes it — all of it or none of it, because a
partial strip would mangle code that merely contains a pipe.
"""

from __future__ import annotations

from veritas.review.nodes.common import strip_line_number_prefixes


def test_every_prefixed_line_is_cleaned_and_indentation_after_the_prefix_kept():
    snippet = "   21| def f():\n   22|     return 1"
    assert strip_line_number_prefixes(snippet) == ("def f():\n    return 1", True)
    # The number's own padding goes with it; only the code's indentation stays.
    assert strip_line_number_prefixes(snippet)[0].splitlines()[1] == "    return 1"
    # Tab indentation after the prefix is preserved too.
    assert strip_line_number_prefixes("    5|\tif x:") == ("\tif x:", True)


def test_blank_unprefixed_line_inside_a_prefixed_snippet_is_kept_as_empty():
    # A blank line carries no prefix, so it cannot veto the strip; it is kept
    # as an empty line so the lines around it keep their relative positions.
    snippet = "   21| def f():\n\n   23|     return 1"
    assert strip_line_number_prefixes(snippet) == ("def f():\n\n    return 1", True)
    # Whitespace-only lines count as blank.
    assert strip_line_number_prefixes("   21| a\n   \n   22| b") == ("a\n\nb", True)


def test_one_unprefixed_non_blank_line_leaves_the_snippet_unchanged():
    # Half a strip would turn line 21 into real-looking text and line 22 into
    # garbage, so nothing is removed unless every non-blank line is prefixed.
    snippet = "   21| def f():\n    return 1"
    assert strip_line_number_prefixes(snippet) == (snippet, False)
    assert strip_line_number_prefixes("intro\n   21| def f():") == (
        "intro\n   21| def f():",
        False,
    )


def test_ordinary_code_containing_a_pipe_is_unchanged():
    # A pipe mid-line is not a line-number prefix.
    assert strip_line_number_prefixes("x = a | b") == ("x = a | b", False)
    assert strip_line_number_prefixes("flags = a | b\nmode = c | d") == (
        "flags = a | b\nmode = c | d",
        False,
    )


def test_prefixed_line_with_no_content_becomes_an_empty_line():
    assert strip_line_number_prefixes("   21|") == ("", True)
    assert strip_line_number_prefixes("   21| def f():\n   22|") == ("def f():\n", True)


def test_single_prefixed_line_is_stripped():
    assert strip_line_number_prefixes("    5| foo") == ("foo", True)


def test_snippet_without_any_code_to_strip_is_reported_as_unchanged():
    # Nothing was removed, so the flag stays False even though nothing failed.
    assert strip_line_number_prefixes("") == ("", False)
    assert strip_line_number_prefixes("\n  \n") == ("\n  \n", False)


def test_prefix_numbers_are_removed_and_not_interpreted():
    # The number is not the location: the finding's line range says where the
    # code is, so any digits are simply dropped.
    assert strip_line_number_prefixes("   99| def f():\n  100|     return 1") == (
        "def f():\n    return 1",
        True,
    )