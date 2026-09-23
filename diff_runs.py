#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku`,
which here is the video id.

    python3 diff_runs.py --old nasa.2026-09-01.json \\
                          --new nasa.2026-09-07.json

Typical use is a scheduled re-run kept under a dated filename, diffed against
the previous one:

    python3 playwright_scraper.py --url nasa --out "nasa_$(date +%F)"
    python3 diff_runs.py --old "$(ls -t nasa_*.json | grep -v meta | sed -n 2p)" \\
                          --new "nasa_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new: the video was
                   deleted or made private, or simply fell out of the
                   account's embed window (see below)
  changed        — sku present in both, with a different caption, count,
                   sound, entity list, caption-track list or flag. See
                   TRACKED_FIELDS.
  source_changed — sku present in both, but one row was read from the embed
                   window alone (`data_source` = `embed`, as `--no-enrich`
                   writes) and the other from the video page, and they
                   differ on a column only the video page fills. Reported
                   separately because this says something about our own two
                   snapshots rather than about the video — and
                   --fail-on-change deliberately ignores it.

TWO THINGS TO KNOW BEFORE READING A DIFF OF THIS SITE
-----------------------------------------------------
**`removed` usually does not mean deleted.** `--mode videos` reads an
account's embed window — the ten to twelve most recent videos, however many
the account has posted — so the window moves on as the account posts, and
the oldest video in last night's file falls out of tonight's without
anything having happened to it. The sidecar says so (`window_is_a_sample`),
and this tool repeats it.

**`position` and `page` are deliberately not tracked.** A new post at the
top of the window shifts every row beneath it by one, so tracking position
would report most of the file as changed whenever the account posts, and a
diff that is always noisy is one nobody reads.

A row this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# What is worth watching on a video, and nothing else.
#
# EVERY NAME HERE MUST EXIST ON THE ROW CLASS, and that is not a style
# rule. This tuple used to be tiktok-profile-scraper's, arriving with the
# copied core and naming 26 account fields `Video` does not have — so the
# diff compared the caption and one count and nothing else, and reported
# "0 changed" on runs where a tracked value had changed. A monitor that
# cannot see the thing it monitors is worse than no monitor, because it
# reports success. `smoke_test.py` pins every name here against the
# dataclass.
#
# DELIBERATELY NOT TRACKED:
#
#   every media URL — `play_addr`, `download_addr`, `cover_url`,
#     `dynamic_cover_url`, `image_urls`, `subtitle_url` — and
#     `media_expires_at`. They are signed and the signature is re-minted
#     per request, so tracking them would report every video as changed on
#     every run (README, "Every media URL expires").
#
#   `video_id` is not tracked because it IS the join key (`sku`), and
#     `author_id` / `author_sec_uid` are identifiers rather than things a
#     creator edits; the visible handle and name are tracked instead.
#
#   the media's own dimensions, format and byte size — properties of an
#     encode, not of what the creator published.
#
#   `position` and `page` — see the docstring.
#
#   `scraped_at`, `stats_source` and `data_source` — the last drives
#     `source_changed` instead, so an `--no-enrich` run diffed against a
#     full one does not report every video-page column as having changed.

TRACKED_FIELDS = (
    # what the video says
    "title",
    "description",
    "hashtags",
    "mentions",
    "content_type",
    "image_count",
    "image_title",
    # engagement
    "play_count",
    "digg_count",
    "comment_count",
    "share_count",
    "collect_count",
    "repost_count",
    # who made it, as displayed
    "author_username",
    "author_nickname",
    "author_verified",
    # the sound
    "music_id",
    "music_title",
    # caption tracks, and the site's own labels on the video
    "subtitle_languages",
    "is_ad",
    "is_aigc",
    "diversification_labels",
    "anchor_keywords",
)

# The column that says which source a row was read from, and the tracked
# columns only ONE of those sources fills. An embed row carries the
# caption, the play count, the author and the media URLs; everything below
# is read from the video page alone (pinned in smoke_test.py by comparing a
# real embed row with a real video-page row).
SOURCE_COLUMN = "data_source"
SOURCE_ONLY_FIELDS = (
    "hashtags",
    "mentions",
    "content_type",
    "image_count",
    "image_title",
    "digg_count",
    "comment_count",
    "share_count",
    "collect_count",
    "repost_count",
    "music_id",
    "music_title",
    "subtitle_languages",
    "is_ad",
    "is_aigc",
    "diversification_labels",
    "anchor_keywords",
)


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def diff_products(old: List[dict], new: List[dict]) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed = [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # A row whose SOURCE_COLUMN differs between runs is not comparable
        # on SOURCE_ONLY_FIELDS: an embed row leaves them null and a video-page row fills them, so every one of them would read as a change and none of it would be about the video. Reporting it as a change would be a
        # false alarm about the site; the other columns still compare fine.
        sources = ((before.get(SOURCE_COLUMN), after.get(SOURCE_COLUMN))
                   if SOURCE_COLUMN else (None, None))
        if sources[0] != sources[1] and any(f in field_changes
                                            for f in SOURCE_ONLY_FIELDS):
            source_part = {f: v for f, v in field_changes.items()
                           if f in SOURCE_ONLY_FIELDS}
            other_part = {f: v for f, v in field_changes.items()
                          if f not in SOURCE_ONLY_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                SOURCE_COLUMN: {"old": sources[0], "new": sources[1]},
                "changes": source_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _describe(p: dict) -> str:
    """The one or two columns that make an added/removed line readable."""
    return f"@{p.get('author_username') or '?'}"


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable across run kinds.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  {_describe(p)}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  {_describe(p)}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result["source_changed"]:
        src = c[SOURCE_COLUMN]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[{SOURCE_COLUMN} {src['old']!r} -> {src['new']!r}: "
              f"an embed-only row leaves these columns null and a video-page row fills them, so this is not a change in the video]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    The rows both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            # This tool's whole premise is one row per `sku`, diffed on
            # TRACKED_FIELDS. A mode that produces many rows per sku would
            # give a diff whose every line is an artefact of two rows
            # sharing an id, so it is refused outright rather than answered.
            # Every mode this repo has qualifies; the check is here so that
            # adding one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku, so there "
                f"is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). Rows from "
            f"different modes carry different columns, so "
            f"`added`/`removed` would describe the mode change rather "
            f"than the site.")

    # A SORT GUARD, family core. Where rows carry a `sort` column, two runs
    # under different orderings are different SAMPLES of a capped listing,
    # and diffing them reports the sampling as though the site had
    # changed. No tiktok-* row carries one today, so this guard is inert
    # here — kept rather than deleted so a route that gains an ordering is
    # guarded from its first run.
    #
    # `sort` is therefore a column rather than a sidecar field, so this
    # guard reads what is actually in the rows rather than trusting
    # metadata that a hand-edited file could contradict.
    sorts = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        rows, meta = _load(path), (_run_status(path)[1] or {})
        seen = {r.get("sort") for r in rows if isinstance(r, dict)}
        seen.discard(None)
        sorts[label] = (seen.pop() if len(seen) == 1
                        else meta.get("sort") or "?")
    if len(set(sorts.values())) > 1 and "?" not in sorts.values():
        problems.append(
            f"the two runs used different orderings ({sorts}). A run "
            f"that stops after N pages holds a different SET of rows under "
            f"each, so the diff would report the sampling "
            f"rather than the site.")

    # And whether either run is a SAMPLE, which changes what `removed` means.
    for label, path in (("--old", args.old), ("--new", args.new)):
        _, meta = _run_status(path)
        if (meta or {}).get("window_is_a_sample"):
            print(f"[i] {label} ({path}) holds each account's embed window, "
                  f"not its back catalogue — a complete run, and a sample. A "
                  f"`removed` line may mean the window moved on rather than "
                  f"that a video was deleted. CLAUDE.md §21: complete and "
                  f"exhaustive are different words.")

    if not problems:
        return True

    # A generic headline, because the reasons below are not only about
    # completeness: a mode mismatch and an ordering mismatch are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include rows that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two tiktok-video-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # `source_changed` is not a reason to fail: it means one row was read
    # from the embed window alone and the other from the video page, so the
    # columns only the video page fills differ. That says something about
    # our own two snapshots rather than about the video, and alerting on it
    # would train whoever reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
