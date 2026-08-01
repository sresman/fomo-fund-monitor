from __future__ import annotations

"""Master-manifest YouTube-ID loader (Prompt 4).

``load_manifest_youtube_ids(path) -> set[str]`` reads the transcript master
manifest (a JSON list of dicts, each with an optional ``url``) and extracts the
set of YouTube video ids present, used by ``monitors/youtube.py`` to skip videos
already transcribed. Defensive: any problem (missing file, malformed JSON, wrong
container shape) logs a WARNING and returns ``set()`` -- it NEVER raises. YouTube
dedupe then relies on state alone. The current real manifest has ZERO YouTube
urls, so this returns ``set()`` today; the loader is future-proofed for when YT
urls are added.

Id extraction uses ``urllib.parse.urlparse`` + a host allowlist
(``YOUTUBE_MANIFEST_HOSTS``) + a bounded exactly-11-char id regex, which rejects
``notyoutube.com`` hosts, 12-char runs, and ``v=`` params on non-allowlisted
hosts.

Imports neither feedparser nor googleapiclient.
"""

import json
import logging
import re
import urllib.parse
from pathlib import Path

from constants import YOUTUBE_MANIFEST_HOSTS

_log = logging.getLogger(__name__)

# Anchored, exactly-11-char YouTube id shape (correct alphabet). A 12-char run
# does NOT match because of the anchored ``$``.
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _extract_youtube_id(url: str) -> str | None:
    """Return the 11-char YouTube video id in ``url``, or ``None``.

    Host must be in ``YOUTUBE_MANIFEST_HOSTS`` (exact netloc match). Candidate is
    the ``v=`` query param, a ``/shorts/<seg>`` or ``/embed/<seg>`` path segment,
    or (for ``youtu.be``) the first path segment. The candidate is returned only
    if it FULLY matches the 11-char id shape.
    """
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or "").lower()
    if host not in YOUTUBE_MANIFEST_HOSTS:
        return None

    candidate: str | None = None
    if host == "youtu.be":
        segments = parsed.path.lstrip("/").split("/", 1)
        candidate = segments[0] if segments and segments[0] else None
    else:
        v_values = urllib.parse.parse_qs(parsed.query).get("v")
        if v_values:
            candidate = v_values[0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed"):
                candidate = parts[1]

    if candidate is not None and _YT_ID_RE.match(candidate):
        return candidate
    return None


def load_manifest_youtube_ids(path: Path) -> set[str]:
    """Load the set of YouTube video ids referenced in the manifest at ``path``.

    Never raises: any missing/malformed/wrong-shape input logs a WARNING and
    returns ``set()``.
    """
    if not path.exists():
        _log.warning(
            "manifest not found at %s; YouTube dedupe relies on state only", path
        )
        return set()

    try:
        text = path.read_text(encoding="utf-8")
        obj: object = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("manifest at %s unreadable/invalid JSON: %s", path, exc)
        return set()

    if not isinstance(obj, list):
        _log.warning(
            "manifest at %s is not a JSON list (got %s); ignoring",
            path,
            type(obj).__name__,
        )
        return set()

    ids: set[str] = set()
    for entry in obj:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or url == "":
            continue
        video_id = _extract_youtube_id(url)
        if video_id is not None:
            ids.add(video_id)
    return ids
