from __future__ import annotations

"""Shared content-hash-diff helpers for the SCRAPE/DIFF monitors (Prompt 5).

Pure, stateless helpers -- NO state I/O and NO fetch (the monitors own snapshot
persistence and inject the fetch seam from ``_common``). A distinct module from
``_common.py`` (the RSS-family FEED helper) so the feed helpers stay free of
BeautifulSoup / difflib / hashlib: importing ``_common`` must not pull in bs4.
This module REUSES ``matches_keywords`` (and the fetch Protocols) from
``_common`` rather than duplicating them.

Pipeline: raw HTML bytes -> strip non-content tags -> extract visible text with
newline separators -> per-line strip + drop-blank + rejoin (MULTI-LINE
normalized text) -> SHA-256 the normalized text (never raw HTML). A real change
then yields a meaningful LINE-ORIENTED unified diff.

Why hash normalized, tag-stripped text and not raw HTML: raw HTML carries
volatile ads, timestamps, nonces, CSRF tokens, rotating asset hashes AND
``<script>``-injected JSON / build-ids that change every load -> naive page
hashing false-positives every run. Stripping the non-content tags kills that.

``bs4`` is a hard dependency imported at module top (unlike feedparser, which
stays deferred in ``_common``). Dynamic bs4 return values are narrowed to ``str``
at the boundary so no bare ``Any`` escapes (``types-beautifulsoup4`` under strict
mypy).
"""

import hashlib
from difflib import unified_diff

from bs4 import BeautifulSoup, Tag

from constants import CONTENT_MIN_TEXT_LEN, WAF_CHALLENGE_PHRASES

# Non-content tags decomposed BEFORE text extraction. ``<noscript>`` is
# deliberately KEPT: it holds the no-JS fallback content a plain ``requests``
# fetch actually renders, so decomposing it could drop the REAL content we hash.
_STRIP_TAGS: tuple[str, ...] = ("script", "style", "template", "svg", "head")

_TRUNCATION_MARKER: str = "\n…(truncated)"  # "\n…(truncated)"


def extract_normalized_text(html: bytes) -> str:
    """Strip non-content tags, extract visible text, line-normalize.

    Steps:
      1. Parse with the stdlib ``html.parser`` (deterministic, no hard lxml
         bind; lxml is an available fallback if html.parser proves fragile on a
         live page).
      2. Decompose ``script`` / ``style`` / ``template`` / ``svg`` / ``head``
         (KEEP ``<noscript>``).
      3. ``get_text("\\n")`` -- newline separator, NOT space.
      4. Per-line strip, DROP blank lines, rejoin with ``"\\n"`` -> multi-line
         normalized text.

    Returns "" if nothing survives.

    DOCUMENTED LIMITATIONS: ``get_text("\\n")`` can OVER-SPLIT inline elements
    (text wrapped in ``<span>`` / ``<a>``), so a purely presentational
    span-wrapping redesign would change the hash even with identical visible
    words. The output is "HTML text after stripping selected tags", NOT true
    rendered-visible text; svg/head/title are dropped from the diff. Accepted
    for v1 (favours over-alerting on a real redesign over silently missing one).
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_STRIP_TAGS):
        if isinstance(tag, Tag):
            tag.decompose()
    text: str = soup.get_text("\n")
    lines = [ln.strip() for ln in text.split("\n")]
    non_blank = [ln for ln in lines if ln]
    return "\n".join(non_blank)


def content_hash(normalized_text: str) -> str:
    """SHA-256 hexdigest of the MULTI-LINE NORMALIZED text (never raw HTML).

    Encoding is inlined ``"utf-8"`` (no CONTENT_HASH_ENCODING constant).
    """
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def is_suspect_content(text: str) -> bool:
    """True if the normalized text is a failed/suspect fetch and MUST NOT be
    seeded or diffed (WAF/JS false-baseline guard, SD-P5-8).

    Suspect iff EITHER:
      - ``len(text) < CONTENT_MIN_TEXT_LEN`` (empty / JS-only skeleton / error
        shell); OR
      - it contains (case-insensitively) any ``WAF_CHALLENGE_PHRASES`` phrase (a
        bot-challenge interstitial long enough to pass the min-length check but
        not real page content).

    HTTP 403/429/503 are already turned into ``MonitorError`` by
    ``raise_for_status`` upstream, so those never reach here.
    """
    if len(text) < CONTENT_MIN_TEXT_LEN:
        return True
    lowered = text.lower()
    for phrase in WAF_CHALLENGE_PHRASES:
        if phrase in lowered:
            return True
    return False


def _is_change_line(line: str) -> bool:
    """A unified-diff BODY change line (``+``/``-``), excluding the ``+++``/``---``
    file headers and the ``@@`` hunk headers."""
    if line.startswith("+++") or line.startswith("---"):
        return False
    if line.startswith("@@"):
        return False
    return line.startswith("+") or line.startswith("-")


def make_diff(old_text: str, new_text: str, limit: int) -> str:
    """Line-oriented unified diff, changed-lines-first, capped INCLUDING marker.

    Builds the snippet PRIORITIZING changed (``+``/``-``) lines so a small cap
    never yields all-context. Identical text -> "". The cap INCLUDES the
    truncation marker: if the assembled diff exceeds ``limit`` the returned
    string (marker included) is ``<= limit``; otherwise it is returned as-is with
    no marker.

    Does its OWN cap (never ``excerpt()``, which would collapse newlines and
    destroy the diff).
    """
    diff_lines = list(
        unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="")
    )
    if not diff_lines:
        return ""

    changed = [ln for ln in diff_lines if _is_change_line(ln)]
    context = [
        ln
        for ln in diff_lines
        if not _is_change_line(ln)
        and not ln.startswith("@@")
        and not ln.startswith("+++")
        and not ln.startswith("---")
    ]
    # Changed lines first (so a cap keeps signal), then remaining context (the
    # ``@@`` hunk headers and empty ``---``/``+++`` file headers -- carrying no
    # filename here -- are dropped as noise).
    ordered = changed + context
    assembled = "\n".join(ordered)

    if len(assembled) <= limit:
        return assembled

    marker = _TRUNCATION_MARKER
    keep = limit - len(marker)
    if keep <= 0:
        # Degenerate cap smaller than the marker -> return a hard-capped marker.
        return marker[:limit]
    return assembled[:keep] + marker


def changed_lines(old_text: str, new_text: str) -> tuple[str, ...]:
    """TRULY-changed content lines for the keyword gate.

    Returns genuinely-ADDED lines (present in ``new`` but not in the ``old`` set)
    PLUS genuinely-REMOVED lines (present in ``old`` but not in the ``new`` set).
    A line that merely MOVED (identical text, different position -> present in
    BOTH sets) is EXCLUDED. Removals ARE included so a Baker speaker
    CANCELLATION / de-listing (a removed keyword line) is treated as signal too.

    Returns () when nothing truly changed.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    old_set = set(old_lines)
    new_set = set(new_lines)
    added = [ln for ln in new_lines if ln not in old_set]
    removed = [ln for ln in old_lines if ln not in new_set]
    return tuple(added + removed)
