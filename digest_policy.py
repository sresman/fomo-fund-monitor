from __future__ import annotations

"""What reaches the weekly digest, and what is captured silently without being
rendered.

The digest exists for ONE case: a first-party appearance on a venue that is not
allowlisted -- an interview the alert path could not recognise, visible within a
week instead of never. Everything else in it is noise against that purpose, and
noise in a weekly email is indistinguishable from an empty one.

Two filters, applied at ENQUEUE (``main._process_monitor``) rather than at render
time, so the on-disk queue stays small and a missed heartbeat cannot accumulate
a thousand rows nobody will read:

1. **Event type.** Only ``constants.DIGEST_EVENT_TYPES`` is rendered. Dropping a
   type here does NOT stop detection: the event is still dispatched (to nothing,
   for a silently-captured type) and still committed to the dedupe bucket, so
   re-enabling a type is a one-line change with no backlog flood -- the same
   "silence over disabling" rule the alert policy already follows.

2. **Duration floor**, for the types that carry one. An appearance worth
   recovering is long-form; nothing three minutes long is one.

Duration is deliberately the ONLY second signal. Title framing ("interview",
"sits down", "joins") was measured against the same corpus and failed in both
directions: it admitted a market podcast merely DISCUSSING the subject, and
would have dropped a genuine "<name> on the AI bubble" upload. Duration is a
property of the artefact itself; a title is a claim by whoever uploaded it, and
this system has already been burned once by trusting uploader-written titles.

A row whose duration is UNKNOWN is kept. Missing metadata must never silently
discard the recovery case -- that failure mode is exactly what the digest is
insurance against.
"""

import constants
from state_manager import DigestEntry

__all__ = ["duration_of", "should_queue"]


def duration_of(entry: DigestEntry) -> int | None:
    """Seconds, or None when the row carries no usable duration.

    None is UNKNOWN and never 0: a caller applying a floor must be able to tell
    "shorter than the floor" from "we could not find out".
    """
    text = entry.duration_seconds.strip()
    if text == "":
        return None
    try:
        seconds = int(text)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def should_queue(entry: DigestEntry) -> bool:
    """True iff this silently-captured event earns a line in the weekly digest."""
    if entry.event_type not in constants.DIGEST_EVENT_TYPES:
        return False
    seconds = duration_of(entry)
    if seconds is None:
        return True  # unknown duration: keep, never silently discard
    return seconds >= constants.DIGEST_MIN_DURATION_SECONDS
