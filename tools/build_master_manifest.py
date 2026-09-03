from __future__ import annotations

"""Generate ``reference/master_manifest_v2.json`` from the celeb-pm corpus.

WHY. That file is the YouTube monitor's dedupe source: a video whose id appears
in it is skipped as already transcribed. It had been hand-maintained and drifted
out of sync with the corpus -- missing four 2026 appearances (Aria 04-16, a16z
07-14, ILTB 08-04, All-In 08-14) -- and, more seriously, contained ZERO YouTube
URLs, so ``load_manifest_youtube_ids`` returned an empty set and the manifest
contributed nothing to dedupe at all.

The corpus is authoritative and already carries everything needed:

  transcripts/_master_manifest.json   one row per appearance (label is the key)
  transcripts/youtube/_manifest.json  label -> YouTube video id, for the 34 rows
                                      whose transcript came from YouTube

MERGE RULES (deliberately a merge, NOT a regeneration):

  1. The corpus master is the canonical row set.
  2. Rows present ONLY in the existing manifest are PRESERVED verbatim. The four
     Boston Investment Conference rows exist here and not in the corpus; a pure
     regeneration would silently delete them.
  3. ``youtube_url`` is attached from the corpus YouTube manifest, matched on
     ``label``. This is the field that makes dedupe work.
  4. A non-null ``url`` in the existing manifest WINS over a null in the corpus.
     The corpus nulls many source links; the manifest has 18 real ones.
  5. Output is sorted by (date, label) and written with stable key order, so
     regenerating without a corpus change is a no-op diff.

USAGE::

    python tools/build_master_manifest.py --corpus ../celeb-pm
    python tools/build_master_manifest.py --corpus ../celeb-pm --check   # CI

``--check`` writes nothing and exits 1 if the file is stale, so drift is
detectable rather than discovered months later.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Field order for every emitted row (stable, diff-friendly).
_FIELD_ORDER: tuple[str, ...] = (
    "date",
    "source",
    "label",
    "host",
    "topic",
    "filepath",
    "quality",
    "status",
    "url",
    "youtube_url",
)
_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

CORPUS_MASTER = Path("transcripts/_master_manifest.json")
CORPUS_YOUTUBE = Path("transcripts/youtube/_manifest.json")
DEFAULT_OUTPUT = Path("reference/master_manifest_v2.json")


def _load_rows(path: Path) -> list[dict[str, Any]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, list):
        raise SystemExit(f"{path}: expected a JSON list, got {type(obj).__name__}")
    rows: list[dict[str, Any]] = []
    for row in obj:
        if not isinstance(row, dict):
            raise SystemExit(f"{path}: every element must be an object")
        rows.append(row)
    return rows


def _ordered(row: dict[str, Any]) -> dict[str, Any]:
    """Known fields first in a fixed order, then any extras alphabetically.

    Extras matter: four preserved rows carry ``found_by`` / ``confidence`` /
    ``has_transcript`` / ``format`` / ``notes`` that must not be dropped.
    """
    out: dict[str, Any] = {k: row[k] for k in _FIELD_ORDER if k in row}
    out.update({k: row[k] for k in sorted(row) if k not in _FIELD_ORDER})
    return out


def build(corpus: Path, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the merge rules and return the rows to write."""
    corpus_rows = _load_rows(corpus / CORPUS_MASTER)
    youtube_rows = _load_rows(corpus / CORPUS_YOUTUBE)

    yt_by_label: dict[str, str] = {}
    for row in youtube_rows:
        label, video_id = row.get("label"), row.get("id")
        if isinstance(label, str) and isinstance(video_id, str) and video_id:
            yt_by_label[label] = video_id

    existing_by_label = {
        r["label"]: r for r in existing if isinstance(r.get("label"), str)
    }

    merged: list[dict[str, Any]] = []
    for row in corpus_rows:
        label = row.get("label")
        out = dict(row)
        prior = existing_by_label.get(label) if isinstance(label, str) else None
        if prior is not None:
            # Rule 4: keep a real url the corpus has nulled.
            if not out.get("url") and prior.get("url"):
                out["url"] = prior["url"]
            # Preserve any extra fields the hand-maintained row carried.
            for key, value in prior.items():
                if key not in out:
                    out[key] = value
        if isinstance(label, str) and label in yt_by_label:
            out["youtube_url"] = _WATCH_URL.format(video_id=yt_by_label[label])
        merged.append(_ordered(out))

    # Rule 2: preserve rows that exist only in the current manifest.
    corpus_labels = {r.get("label") for r in corpus_rows}
    for row in existing:
        if row.get("label") not in corpus_labels:
            merged.append(_ordered(row))

    merged.sort(key=lambda r: (str(r.get("date", "")), str(r.get("label", ""))))
    return merged


def _serialize(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the YouTube-dedupe manifest from the celeb-pm corpus."
    )
    parser.add_argument(
        "--corpus", required=True, type=Path, help="path to the celeb-pm checkout"
    )
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if the output file is stale",
    )
    args = parser.parse_args(argv)

    for required in (CORPUS_MASTER, CORPUS_YOUTUBE):
        if not (args.corpus / required).exists():
            raise SystemExit(f"corpus file not found: {args.corpus / required}")

    existing = _load_rows(args.output) if args.output.exists() else []
    rows = build(args.corpus, existing)
    payload = _serialize(rows)

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current == payload:
            print(f"{args.output} is up to date ({len(rows)} rows)")
            return 0
        print(f"{args.output} is STALE -- rerun without --check", file=sys.stderr)
        return 1

    args.output.write_text(payload, encoding="utf-8")
    with_yt = sum(1 for r in rows if r.get("youtube_url"))
    print(f"wrote {args.output}: {len(rows)} rows, {with_yt} with a youtube_url")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
