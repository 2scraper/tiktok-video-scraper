#!/usr/bin/env python3
"""make_fixtures.py — build `fixtures_generated.json` from real captures.

Why fixtures are TRIMMED rather than whole pages
================================================
An embed page is ~294 KB and a video page ~393 KB, and almost all of it is
the app shell: translation tables, A/B assignments, and the session
material the page was fetched with (a csrf token, an odinId, a ttwid, CDN
signatures).

Every one of those lives OUTSIDE the two scopes this repo's parser reads —
`__DEFAULT_SCOPE__["webapp.video-detail"]` on a video page and the embed
state's own data node. So trimming to those scopes is not a scrubbing step
bolted on afterwards: it removes the session material as a side effect of
keeping only what is under test, which is the version of this that cannot
rot (CLAUDE.md §10).

What IS kept, deliberately
==========================
The signed media URLs — `playAddr`, `downloadAddr`, the covers, the
subtitle track and a photo post's stills — complete. `_expiry_from_url()`
reads each one's expiry stamp, and a fixture with the query strings
stripped would test neither that nor the `parse_qs` path.

Nothing here is a private individual. A TikTok video and its caption are a
public creator's published work, not a comment thread — which is the
distinction CLAUDE.md §10 draws, and the reason nothing is replaced with a
placeholder in this repo while a sibling replaces every commenter.

Verify a trimmed fixture parses identically to its untrimmed original
=====================================================================
`--verify` re-parses both and compares the rows field by field.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from output_writer import Video                                # noqa: E402
from product_parser import (VIDEO_DETAIL_SCOPE, parse_embed,   # noqa: E402
                            parse_video)
from tiktok_payload import (PayloadError, embed_node,          # noqa: E402
                            frontity_state, rehydration_scope)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures_generated.json")

# Each capture is here for a reason no other one covers — CLAUDE.md §15's
# "one dump teaches you one locale", applied to POST SHAPES.
EMBEDS = {
    "vid_embed_nasa.html": ("nasa", "an 11-video window, one of them a photo post"),
    "vid_embed_therock.html": ("therock", "a second account's window, for the count"),
}
VIDEOS = {
    "vid_video_plain.html": ("plain", "a video with no hashtags and no mentions"),
    "vid_video_entities.html": ("entities",
                                "hashtags AND a mention, with emoji in the "
                                "caption — the UTF-16 offset trap"),
    "vid_video_subtitles.html": ("subtitles", "a downloadable caption track"),
    "vid_photo_post.html": ("photo_post",
                            "TikTok photo mode: a 2-image carousel with its "
                            "own title, duration/width/height all 0 and no "
                            "playAddr"),
    "vid_video_anchor.html": ("anchor",
                              "a linked card on the video — an anchor, which "
                              "is how a TikTok Shop product or a CapCut "
                              "template attaches to a post"),
    "vid_video_missing.html": ("missing", "an id with no video behind it"),
}


# Trimmed to the FIELDS the parser reads, not just to the scope.
#
# The scope alone was the first version and it left the fixtures carrying
# TikTok's encoded transcode metadata — `bitrateInfo`, `volumeInfo`,
# `transcode_feature_id` — which nothing here reads and which is full of
# 32-hex strings this repo's own credential scan (rightly) flags.
#
# CLAUDE.md §24 asks the question in the right order: before exempting a
# value, check whether it is needed at all. It is not. Removing it is
# strictly better than forgiving it, because an exemption is a hole a real
# key could later hide in — and `--verify` proves the trim lost nothing by
# re-parsing both and comparing every column.
_ITEM_FIELDS = (
    "id", "desc", "createTime", "author", "stats", "statsV2", "music",
    "video", "textExtra", "challenges", "imagePost", "anchors", "isAd",
    "IsAigc", "AIGCDescription", "locationCreated", "diversificationLabels",
)
_VIDEO_FIELDS = (
    "id", "height", "width", "duration", "ratio", "definition", "format",
    "size", "bitrate", "playAddr", "downloadAddr", "cover", "dynamicCover",
    "subtitleInfos",
)
_AUTHOR_FIELDS = ("id", "uniqueId", "nickname", "secUid", "verified")
_MUSIC_FIELDS = ("id", "title", "authorName", "original", "duration")


def _keep(source: Any, fields_wanted) -> Dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {k: v for k, v in source.items() if k in fields_wanted}


def trim_video(html: str) -> Dict[str, Any]:
    scope = rehydration_scope(html)
    detail = scope.get(VIDEO_DETAIL_SCOPE)
    if detail is None:
        raise PayloadError(f"capture carries no {VIDEO_DETAIL_SCOPE!r}")
    info = detail.get("itemInfo") or {}
    item = info.get("itemStruct")
    if isinstance(item, dict):
        item = _keep(item, _ITEM_FIELDS)
        if "video" in item:
            item["video"] = _keep(item["video"], _VIDEO_FIELDS)
        if "author" in item:
            item["author"] = _keep(item["author"], _AUTHOR_FIELDS)
        if "music" in item:
            item["music"] = _keep(item["music"], _MUSIC_FIELDS)
        info = dict(info)
        info["itemStruct"] = item
    slim = {k: v for k, v in detail.items() if k != "itemInfo"}
    slim["itemInfo"] = info
    return {"__DEFAULT_SCOPE__": {VIDEO_DETAIL_SCOPE: slim}}


def trim_embed(html: str) -> Dict[str, Any]:
    node = embed_node(html)
    # Rebuilt under a fixed route key rather than the account's own, so a
    # fixture cannot accidentally test key reconstruction — `embed_node`
    # selects by SHAPE and this proves it.
    return {"source": {"data": {"/embed/fixture": node}}}


def as_video_page(payload: Dict[str, Any]) -> str:
    return ('<!DOCTYPE html><html><head><script id='
            '"__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script></head><body></body></html>")


def as_embed_page(payload: Dict[str, Any]) -> str:
    return ('<!DOCTYPE html><html><head><script id='
            '"__FRONTITY_CONNECT_STATE__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script></head><body></body></html>")


def build(capture_dir: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "_readme": (
            "Generated by make_fixtures.py from real captures. Each entry is "
            "one scope of one real TikTok page, trimmed from a ~300-400 KB "
            "capture. Session material lives outside those scopes and is "
            "therefore absent rather than redacted. Run `python3 "
            "make_fixtures.py --verify` to re-check that a trimmed fixture "
            "parses identically to its untrimmed original."),
        "embeds": {}, "videos": {},
    }
    missing = []
    for filename, (name, why) in EMBEDS.items():
        path = os.path.join(capture_dir, filename)
        if not os.path.exists(path):
            missing.append(filename)
            continue
        html = open(path, encoding="utf-8", errors="replace").read()
        out["embeds"][name] = {"why": why, "payload": trim_embed(html)}
    for filename, (name, why) in VIDEOS.items():
        path = os.path.join(capture_dir, filename)
        if not os.path.exists(path):
            missing.append(filename)
            continue
        html = open(path, encoding="utf-8", errors="replace").read()
        out["videos"][name] = {"why": why, "payload": trim_video(html)}
    if missing:
        print(f"[!] {len(missing)} capture(s) not found: {missing}",
              file=sys.stderr)
    return out


def verify(capture_dir: str) -> int:
    data = json.load(open(OUT, encoding="utf-8"))
    bad = 0
    for filename, (name, _why) in VIDEOS.items():
        path = os.path.join(capture_dir, filename)
        entry = data["videos"].get(name)
        if entry is None or not os.path.exists(path):
            print(f"  video/{name:12} SKIP")
            continue
        original = open(path, encoding="utf-8", errors="replace").read()
        trimmed = as_video_page(entry["payload"])
        a, da = parse_video(original, "u", "T", Video)
        b, db = parse_video(trimmed, "u", "T", Video)
        same = ([asdict(r) for r in a] == [asdict(r) for r in b]
                and da.get("status") == db.get("status"))
        print(f"  video/{name:12} {'OK' if same else 'DIFFERS'}  "
              f"({len(a)} row(s), status {da.get('status')})")
        bad += 0 if same else 1
    for filename, (handle, _why) in EMBEDS.items():
        name = EMBEDS[filename][0]
        path = os.path.join(capture_dir, filename)
        entry = data["embeds"].get(name)
        if entry is None or not os.path.exists(path):
            print(f"  embed/{name:12} SKIP")
            continue
        original = open(path, encoding="utf-8", errors="replace").read()
        trimmed = as_embed_page(entry["payload"])
        a, _ = parse_embed(original, handle, "T", Video)
        b, _ = parse_embed(trimmed, handle, "T", Video)
        same = [asdict(r) for r in a] == [asdict(r) for r in b]
        print(f"  embed/{name:12} {'OK' if same else 'DIFFERS'}  "
              f"({len(a)} row(s))")
        bad += 0 if same else 1
    return 1 if bad else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--captures", default="/home/petr/2scraper/captures/tiktok")
    p.add_argument("--verify", action="store_true")
    args = p.parse_args()
    if args.verify:
        sys.exit(verify(args.captures))
    data = build(args.captures)
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"[+] {len(data['embeds'])} embed + {len(data['videos'])} video "
          f"fixture(s) -> {OUT} ({os.path.getsize(OUT):,} bytes)")
