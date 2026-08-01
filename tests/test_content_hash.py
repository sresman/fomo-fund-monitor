from __future__ import annotations

"""Tests for the shared content-hash helpers (monitors/_content_hash.py).

Pure stateless helpers -- no network, no state I/O. Inline canned HTML bytes."""

from constants import CONTENT_MIN_TEXT_LEN, DIFF_SNIPPET_MAX
from monitors._content_hash import (
    changed_lines,
    content_hash,
    extract_normalized_text,
    is_suspect_content,
    make_diff,
)

# --------------------------------------------------------------------------- #
# extract_normalized_text + content_hash
# --------------------------------------------------------------------------- #


def test_tag_strip_same_visible_text_same_hash() -> None:
    """Same visible text, DIFFERENT script/style/attrs -> SAME hash (the #1
    correctness fix: volatile <script> JSON / build-ids must not change hash)."""
    a = (
        b"<html><head><title>T</title></head><body>"
        b'<script>window.__DATA__={"build":"abc123","nonce":"xyz"}</script>'
        b'<style>.x{color:red}</style>'
        b'<p class="one" data-ts="111">Gavin Baker speaks at 3pm</p>'
        b"<p>Second line here for length padding padding</p>"
        b"</body></html>"
    )
    b = (
        b"<html><head><title>T</title></head><body>"
        b'<script>window.__DATA__={"build":"zzz999","nonce":"qqq"}</script>'
        b'<style>.x{color:blue}</style>'
        b'<p class="two" data-ts="222">Gavin Baker speaks at 3pm</p>'
        b"<p>Second line here for length padding padding</p>"
        b"</body></html>"
    )
    assert content_hash(extract_normalized_text(a)) == content_hash(
        extract_normalized_text(b)
    )


def test_different_visible_text_different_hash() -> None:
    a = b"<html><body><p>Speaker: Gavin Baker padding padding padding</p></body></html>"
    b = b"<html><body><p>Speaker: Leopold Aschenbrenner padding padding</p></body></html>"
    assert content_hash(extract_normalized_text(a)) != content_hash(
        extract_normalized_text(b)
    )


def test_noscript_kept() -> None:
    """<noscript> content is KEPT (no-JS fallback a requests fetch renders); two
    docs differing only in <noscript> -> DIFFERENT hashes."""
    base = b"<html><body><noscript>%s</noscript></body></html>"
    a = base % b"Gavin Baker will present the AI keynote at the conference here"
    b = base % b"Leopold Aschenbrenner will present the AGI keynote at the event"
    text_a = extract_normalized_text(a)
    assert "Gavin Baker" in text_a  # not stripped
    assert content_hash(text_a) != content_hash(extract_normalized_text(b))


def test_normalization_drops_blanks_and_strips() -> None:
    html = b"<html><body><p>   Line one   </p><p></p><p>Line two</p></body></html>"
    text = extract_normalized_text(html)
    assert text == "Line one\nLine two"


def test_empty_and_tags_only_returns_empty() -> None:
    assert extract_normalized_text(b"") == ""
    assert extract_normalized_text(b"<html><head><script>x=1</script></head></html>") == ""


# --------------------------------------------------------------------------- #
# make_diff
# --------------------------------------------------------------------------- #


def test_make_diff_identical_returns_empty() -> None:
    assert make_diff("a\nb\nc", "a\nb\nc", DIFF_SNIPPET_MAX) == ""


def test_make_diff_multiline_prioritizes_changed_lines() -> None:
    old = "alpha\nbeta\ngamma\ndelta"
    new = "alpha\nBETA\ngamma\ndelta"
    diff = make_diff(old, new, DIFF_SNIPPET_MAX)
    assert "-beta" in diff
    assert "+BETA" in diff
    # No file-header noise.
    assert "+++" not in diff
    assert "---" not in diff


def test_make_diff_cap_includes_marker() -> None:
    """A change surrounded by lots of context, capped small: TOTAL length
    (marker included) <= limit, still contains +/- lines, ends with marker."""
    old = "\n".join(f"context line number {i}" for i in range(200))
    new = old + "\nBRAND NEW ADDED LINE"
    limit = 80
    diff = make_diff(old, new, limit)
    assert len(diff) <= limit
    assert "+BRAND NEW ADDED LINE" in diff
    assert diff.endswith("…(truncated)")


def test_make_diff_no_marker_when_under_limit() -> None:
    diff = make_diff("a\nb", "a\nX", DIFF_SNIPPET_MAX)
    assert "…(truncated)" not in diff


# --------------------------------------------------------------------------- #
# changed_lines
# --------------------------------------------------------------------------- #


def test_changed_lines_added_and_removed() -> None:
    old = "keep\nold_line\nshared"
    new = "keep\nnew_line\nshared"
    changed = changed_lines(old, new)
    assert "new_line" in changed  # genuinely added
    assert "old_line" in changed  # genuinely removed
    assert "keep" not in changed
    assert "shared" not in changed


def test_changed_lines_removed_is_signal() -> None:
    old = "Gavin Baker\nother\nfooter"
    new = "other\nfooter"
    assert "Gavin Baker" in changed_lines(old, new)


def test_changed_lines_moved_excluded() -> None:
    """A line whose TEXT is identical but at a different position (present in both
    sets) is EXCLUDED."""
    old = "moved_line\nA\nB"
    new = "A\nB\nmoved_line"
    assert changed_lines(old, new) == ()


def test_changed_lines_nothing_changed() -> None:
    assert changed_lines("a\nb", "a\nb") == ()


# --------------------------------------------------------------------------- #
# is_suspect_content
# --------------------------------------------------------------------------- #


def test_is_suspect_min_length() -> None:
    assert is_suspect_content("")
    assert is_suspect_content("x" * (CONTENT_MIN_TEXT_LEN - 1))
    assert not is_suspect_content("y" * (CONTENT_MIN_TEXT_LEN + 10))


def test_is_suspect_waf_phrase() -> None:
    long_challenge = (
        "Please wait while we are Checking Your Browser before proceeding "
        "to the requested page. This may take a few seconds."
    )
    assert len(long_challenge) >= CONTENT_MIN_TEXT_LEN  # passes min-length
    assert is_suspect_content(long_challenge)  # but caught by phrase


def test_is_suspect_real_content_passes() -> None:
    real = "Gavin Baker will present the keynote on AI compute at the conference."
    assert not is_suspect_content(real)
