"""product_parser.py — this IS the site.

Everything TikTok-shaped that is not one of the handful of named constants
in the engines lives here (CLAUDE.md §1). The generic reading of TikTok's
two structured payloads lives one file over, in `tiktok_payload.py`, which
is byte-identical across the tiktok-* repos and SHA-pinned by each one's
suite.

Two sources, two applications, two coverages
============================================
This repo reads videos, and TikTok publishes them in two places that
belong to two different front-end applications — CLAUDE.md §23's shape,
met again:

    /embed/@handle        __FRONTITY_CONNECT_STATE__   the embed app
        10-12 most recent videos. Each carries id, caption, playCount,
        three cover URLs and a direct playAddr. NOT likes, comments,
        shares, the date, the sound, the hashtags or the captions.

    /@handle/video/{id}   __UNIVERSAL_DATA_FOR_REHYDRATION__  the main app
        one video, complete: exact likes/comments/shares/collects, the
        creation time, the music, the entity list, the caption track, the
        AI and ad flags, and the media's own dimensions and byte size.

Both are served to a bare HTTP client from a datacentre address with no
key, no proxy and no account. Measured 2026-09-22 from Hetzner, Helsinki:
~294 KB for an embed page, ~392 KB for a video page, HTTP 200 for every
one of the 60-odd requests this file was written against.

The route that is NOT open, and why this repo exists separately
==============================================================
The video grid on a profile page is loaded by `/api/post/item_list/`, and
that answers HTTP 200 with `content-length: 0` to every client tried —
headless and headful Chromium, a Windows user agent, after accepting the
EU cookie consent, and through a residential exit in Peru, all with a
correctly signed request TikTok's own front end generated. So there is no
way to walk an account's full back catalogue from here, and this repo says
so rather than paginating into an empty answer.

`/embed/@handle` is the substitute, and it is a WINDOW rather than a feed:
`?page=2` returns the identical ten to twelve videos and the payload's own
`page` field stays 1. CLAUDE.md §23 — before trusting `?page=N`, fetch one
page past the end and read what comes back.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from tiktok_payload import (
    BOT_CHALLENGE_MARKERS,
    PayloadError,
    counts,
    decode_page,
    embed_node,
    is_empty_success,
    rehydration_scope,
)

logger = logging.getLogger("product_parser")

SOURCE = "tiktok.com"

CANONICAL_HOST = "www.tiktok.com"
ACCEPTED_HOSTS = ("www.tiktok.com", "tiktok.com", "m.tiktok.com", "vm.tiktok.com")

SIBLING_HOSTS = {
    "shop.tiktok.com": "a TikTok Shop page — see tiktok-shop-scraper",
    "seller.tiktok.com": "the TikTok Shop seller centre, which needs an account",
    "ads.tiktok.com": "TikTok's ads and Creative Center, which needs an account",
    "library.tiktok.com": "TikTok's Ad Library, which this family does not read yet",
}

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,24}$")
_PROFILE_PATH_RE = re.compile(r"^/@([A-Za-z0-9._]{1,24})/?$")
# A video id is a 19-digit snowflake. Anchored on the URL, which is a
# contract with search engines, rather than on any class in the rendered
# page (CLAUDE.md §4).
_VIDEO_PATH_RE = re.compile(r"^/@([A-Za-z0-9._]{1,24})/(?:video|photo)/(\d{15,21})/?$")
_VIDEO_ID_RE = re.compile(r"^\d{15,21}$")

VIDEO_DETAIL_SCOPE = "webapp.video-detail"


class NotAVideoUrl(ValueError):
    """A TikTok URL, but not one this repo reads — with the reason.

    CLAUDE.md §5: "is not a TikTok URL" is false for
    `https://www.tiktok.com/tag/nasa` and sends the reader hunting for a
    typo that is not there.
    """


def _refuse(url: str, host: str, path: str) -> "NotAVideoUrl":
    if host in SIBLING_HOSTS:
        return NotAVideoUrl(f"{url!r} is {SIBLING_HOSTS[host]}")
    if host not in ACCEPTED_HOSTS:
        return NotAVideoUrl(
            f"{url!r} is not on a TikTok host (got {host!r}); this scraper "
            f"reads {', '.join(ACCEPTED_HOSTS)}")
    if path.startswith("/tag/"):
        kind = ("a hashtag feed. TikTok server-renders no videos onto one — "
                "measured 0 video ids in the markup — and the XHR that "
                "fills it is refused, so this repo does not read them")
    elif path.startswith("/search"):
        kind = ("a search results page, which server-renders no videos and "
                "whose XHR is refused")
    elif path.startswith("/music/") or path.startswith("/sound/"):
        kind = "a sound page, which this repo does not read yet"
    elif path.startswith("/shop"):
        kind = "a TikTok Shop page — see tiktok-shop-scraper"
    elif path in ("/", "/explore", "/foryou"):
        kind = "a TikTok feed page, not a video or an account"
    else:
        kind = "neither an account nor a video"
    return NotAVideoUrl(
        f"{url!r} is {kind}; this scraper reads an account "
        "(https://www.tiktok.com/@handle) or one video "
        "(https://www.tiktok.com/@handle/video/1234567890123456789)")


def parse_target(raw: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Any input a user might type, to ("account"|"video", handle, video_id).

    Accepts a handle, an @handle, a profile URL, a video URL, or a bare
    19-digit video id. A bare id is accepted with NO handle, because
    TikTok serves `/@anything/video/{id}` — the handle in a video URL is
    decoration and the id is the address. Verified rather than assumed:
    see `video_url`.
    """
    s = (raw or "").strip()
    if not s:
        raise NotAVideoUrl("empty target")
    if _VIDEO_ID_RE.match(s):
        return "video", None, s
    if "://" not in s and "/" not in s:
        return "account", normalise_handle(s), None
    if "://" not in s:
        s = "https://" + s
    parts = urlsplit(s)
    host = (parts.netloc or "").lower().split(":")[0]
    path = parts.path or "/"
    m = _VIDEO_PATH_RE.match(path)
    if m and host in ACCEPTED_HOSTS:
        return "video", m.group(1), m.group(2)
    m = _PROFILE_PATH_RE.match(path)
    if m and host in ACCEPTED_HOSTS:
        return "account", m.group(1), None
    raise _refuse(raw, host, path)


def normalise_handle(raw: str) -> str:
    s = (raw or "").strip().lstrip("@")
    if not _HANDLE_RE.match(s):
        raise NotAVideoUrl(
            f"{raw!r} is not a TikTok handle: TikTok allows letters, digits, "
            "underscore and dot, up to 24 characters")
    return s


def embed_url(handle: str) -> str:
    return f"https://{CANONICAL_HOST}/embed/@{normalise_handle(handle)}"


def video_url(video_id: str, handle: Optional[str] = None) -> str:
    """The canonical page URL for a video.

    `@tiktok` is used when no handle is known. That is not a guess: TikTok
    serves a video page for ANY handle in the path and answers with the
    real author in the payload — measured 2026-09-22 across the ids this
    parser was written against. The emitted row's `url` is rebuilt from
    the author the payload states, so a row never carries the placeholder.
    """
    if not _VIDEO_ID_RE.match(str(video_id)):
        raise NotAVideoUrl(f"{video_id!r} is not a TikTok video id "
                           "(TikTok's are 19 digits)")
    return f"https://{CANONICAL_HOST}/@{handle or 'tiktok'}/video/{video_id}"


def page_url(url: str, page: int) -> str:
    """The embed route does not paginate, and saying so is the point.

    Measured 2026-09-22: `?page=2` and `?page=3` on `/embed/@nasa` return
    the identical eleven videos AND the payload's own `page` field stays 1
    — the server stating outright which page it served. CLAUDE.md §23
    records two other sites in this family that answer an out-of-range page
    with page 1, and a run that trusted its own request re-collected it for
    as long as it was asked to and reported success.
    """
    if page == 1:
        return url
    raise NotAVideoUrl(
        "the TikTok embed route serves one fixed window of recent videos "
        "and ignores ?page= — measured: page 2 and page 3 return the same "
        "videos and the payload still says page 1. --pages above 1 is "
        "refused rather than silently re-collecting them")


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

_STAT_KEYS = ("playCount", "diggCount", "commentCount", "shareCount",
              "collectCount", "repostCount")


def _ts_to_iso(value: Any) -> Optional[str]:
    """Unix seconds to an ISO-8601 instant in UTC. 0 means "never", not 1970."""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _clean(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return None


def _expiry_from_url(url: Optional[str]) -> Optional[str]:
    """The `x-expires` / `expire` stamp a signed TikTok CDN URL carries."""
    if not url:
        return None
    try:
        q = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    for key in ("x-expires", "expire", "UrlExpire"):
        raw = (q.get(key) or [None])[0]
        if raw and str(raw).isdigit():
            return _ts_to_iso(int(raw))
    return None


# ---------------------------------------------------------------------------
# The caption's entities
# ---------------------------------------------------------------------------
#
# Read from TikTok's OWN fields, never by slicing the caption. Two
# independent reasons, both measured on 85 entities from real videos on
# 2026-09-22, and either alone is enough:
#
#   1. `start` and `end` index in UTF-16 CODE UNITS, not Python
#      characters. 27 of 85 came out mis-sliced under Python indexing —
#      one of them as 'tRock 10.0 🗡' where the entity is '@ProjectRock'.
#      CLAUDE.md §10 records the same trap on another site in this family.
#
#   2. The text after an `@` is a DISPLAY NAME, not a handle. A caption
#      reading "@sadie" is a mention of the account `sad_i_e` — and
#      `@sadie` is ALSO a real, different account, which this parser
#      checked: both resolve, with different ids. So a regex over the
#      caption does not fail loudly, it attributes the mention to somebody
#      else's live account. 14 of 85.
#
# `hashtagName` and `userUniqueId` are what TikTok means, so they are what
# this reads. The offsets are not used at all.


def caption_entities(item: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(hashtags, mentions) for one video item, deduplicated, in order."""
    hashtags: List[str] = []
    mentions: List[str] = []
    for entry in item.get("textExtra") or []:
        if not isinstance(entry, dict):
            continue
        tag = _clean(entry.get("hashtagName"))
        user = _clean(entry.get("userUniqueId"))
        if tag and tag not in hashtags:
            hashtags.append(tag)
        if user and user not in mentions:
            mentions.append(user)
    # `challenges` is TikTok's own hashtag list and is sometimes fuller
    # than textExtra's; merged rather than chosen between, because the two
    # disagree in both directions on real videos.
    for challenge in item.get("challenges") or []:
        if isinstance(challenge, dict):
            tag = _clean(challenge.get("title"))
            if tag and tag not in hashtags:
                hashtags.append(tag)
    return hashtags, mentions


def subtitle_track(item: Dict[str, Any]) -> Dict[str, Any]:
    """The best available caption track, and what TikTok says about it.

    `Source` is TikTok's own word for how the caption was made. Two values
    measured on 2026-09-22: "ASR" (automatic speech recognition) and "MT"
    (machine translation). Both are machine-generated, and a consumer
    quoting a caption as the creator's own words should know that — so the
    column exists rather than being flattened into a URL. A value this
    parser has not seen is passed through unchanged rather than mapped.
    """
    tracks = ((item.get("video") or {}).get("subtitleInfos")) or []
    tracks = [t for t in tracks if isinstance(t, dict)]
    if not tracks:
        return {}
    languages = [_clean(t.get("LanguageCodeName")) for t in tracks]
    languages = [lang for lang in languages if lang]
    # Prefer a track that is NOT machine-generated when one exists.
    # Measured values so far are "ASR" and "MT", both machine-made; the
    # allowlist is written as "not one of the known machine sources"
    # rather than as "== creator", so an unanticipated value reads as
    # possibly-human and is surfaced rather than silently dropped.
    machine = {"MT", "ASR"}
    best = next((t for t in tracks if _clean(t.get("Source")) not in machine),
                tracks[0])
    url = _clean(best.get("Url"))
    return {
        "subtitle_languages": languages or None,
        "subtitle_url": url,
        "subtitle_format": _clean(best.get("Format")),
        "subtitle_source": _clean(best.get("Source")),
        "subtitle_expires_at": _expiry_from_url(url)
                               or _ts_to_iso(best.get("UrlExpire")),
    }


# ---------------------------------------------------------------------------
# The two parsers
# ---------------------------------------------------------------------------

def parse_embed(html: Any, handle: str, scraped_at: str,
                row_cls: Any) -> Tuple[List[Any], Dict[str, Any]]:
    """An embed page to rows — the account's recent window.

    Ten to twelve videos, measured across seven accounts. Each row is
    marked `data_source="embed"` because it carries a strict SUBSET of a
    video page's columns, and `diff_runs.py` reports a difference that
    comes with a `data_source` difference as `source_changed` rather than
    as the site having changed (CLAUDE.md §9).
    """
    node = embed_node(html)
    diag: Dict[str, Any] = {
        "page_said": node.get("page"),
        "is_error": node.get("isError"),
        "playlist_type": node.get("playlistType"),
    }
    info = node.get("userInfo") or {}
    author = _clean(info.get("uniqueId")) or handle
    rows = []
    for position, entry in enumerate(node.get("videoList") or [], start=1):
        if not isinstance(entry, dict):
            continue
        vid = _clean(entry.get("id"))
        if not vid:
            continue
        play = _clean(entry.get("playAddr"))
        cover = _clean(entry.get("coverUrl"))
        rows.append(row_cls(
            source=SOURCE, scraped_at=scraped_at,
            url=video_url(vid, author), sku=vid,
            title=_clean(entry.get("desc")),
            video_id=vid, description=_clean(entry.get("desc")),
            author_username=author,
            author_id=_clean(info.get("id")),
            author_nickname=_clean(info.get("nickname")),
            author_verified=_bool(info.get("verified")),
            # The embed's playCount is TikTok's rounded figure and there is
            # no second object here to prefer, so `stats_source` says
            # "embed" rather than claiming statsV2.
            play_count=_int(entry.get("playCount")),
            stats_source="embed",
            width=_int(entry.get("width")), height=_int(entry.get("height")),
            ratio=_clean(entry.get("ratio")),
            play_addr=play, cover_url=cover,
            dynamic_cover_url=_clean(entry.get("dynamicCoverUrl")),
            media_expires_at=_expiry_from_url(play) or _expiry_from_url(cover),
            data_source="embed", page=1, position=position,
        ))
    diag["count"] = len(rows)
    return rows, diag


def image_post(item: Dict[str, Any]) -> Dict[str, Any]:
    """A photo post's stills, or an empty dict for a video.

    TikTok's photo mode publishes a carousel under the same `/video/{id}`
    URL. The item then has `duration`, `width` and `height` all 0 and no
    `playAddr`, which reads exactly like a broken video row — so the KIND
    is named from TikTok's own `imagePost` key rather than inferred from
    the zeroed fields. CLAUDE.md §17: order signals by how much they
    prove, and a key the site itself sets proves more than a threshold.

    Each image carries a list of equivalent CDN URLs (different hosts,
    different formats). The first is taken; they are all signed and all
    perish together.
    """
    post = item.get("imagePost")
    if not isinstance(post, dict):
        return {}
    urls: List[str] = []
    for image in post.get("images") or []:
        if not isinstance(image, dict):
            continue
        candidates = ((image.get("imageURL") or {}).get("urlList")) or []
        first = next((_clean(c) for c in candidates if _clean(c)), None)
        if first:
            urls.append(first)
    return {
        "content_type": "photo",
        "image_count": len(post.get("images") or []) or None,
        "image_urls": urls or None,
        "image_title": _clean(post.get("title")),
    }


def parse_video(html: Any, url: str, scraped_at: str,
                row_cls: Any) -> Tuple[List[Any], Dict[str, Any]]:
    """One video page to at most one row."""
    scope = rehydration_scope(html)
    detail = scope.get(VIDEO_DETAIL_SCOPE)
    diag: Dict[str, Any] = {"scope_present": detail is not None}
    if detail is None:
        diag["scopes"] = sorted(k for k in scope if "i18n" not in k)
        raise PayloadError(
            f"page carried no {VIDEO_DETAIL_SCOPE!r} scope; it is not a video "
            f"page (scopes present: {diag['scopes']})")

    status_code = detail.get("statusCode")
    diag["status_code"] = status_code
    diag["status_msg"] = detail.get("statusMsg") or None
    item = (detail.get("itemInfo") or {}).get("itemStruct")
    if not isinstance(item, dict) or not item.get("id"):
        diag["status"] = "unavailable"
        return [], diag
    diag["status"] = "ok"

    author = item.get("author") or {}
    handle = _clean(author.get("uniqueId"))
    values, stats_source = counts(item, _STAT_KEYS)
    video = item.get("video") or {}
    music = item.get("music") or {}
    hashtags, mentions = caption_entities(item)
    subs = subtitle_track(item)
    anchors = [a for a in (item.get("anchors") or []) if isinstance(a, dict)]
    images = image_post(item)

    play = _clean(video.get("playAddr"))
    download = _clean(video.get("downloadAddr"))
    desc = _clean(item.get("desc"))
    vid = _clean(item.get("id"))

    row = row_cls(
        source=SOURCE, scraped_at=scraped_at,
        url=video_url(vid, handle), sku=vid, title=desc,

        video_id=vid, description=desc,
        created_at=_ts_to_iso(item.get("createTime")),

        author_username=handle,
        author_id=_clean(author.get("id")),
        author_nickname=_clean(author.get("nickname")),
        author_sec_uid=_clean(author.get("secUid")),
        author_verified=_bool(author.get("verified")),

        play_count=values.get("playCount"),
        digg_count=values.get("diggCount"),
        comment_count=values.get("commentCount"),
        share_count=values.get("shareCount"),
        collect_count=values.get("collectCount"),
        repost_count=values.get("repostCount"),
        stats_source=stats_source,

        duration_seconds=_int(video.get("duration")),
        width=_int(video.get("width")), height=_int(video.get("height")),
        ratio=_clean(video.get("ratio")),
        definition=_clean(video.get("definition")),
        video_format=_clean(video.get("format")),
        video_size_bytes=_int(video.get("size")),
        bitrate=_int(video.get("bitrate")),

        play_addr=play, download_addr=download,
        cover_url=_clean(video.get("cover")),
        dynamic_cover_url=_clean(video.get("dynamicCover")),
        media_expires_at=_expiry_from_url(play) or _expiry_from_url(download),

        music_id=_clean(music.get("id")),
        music_title=_clean(music.get("title")),
        music_author=_clean(music.get("authorName")),
        music_is_original=_bool(music.get("original")),
        music_duration_seconds=_int(music.get("duration")),

        hashtags=hashtags or None,
        mentions=mentions or None,

        subtitle_languages=subs.get("subtitle_languages"),
        subtitle_url=subs.get("subtitle_url"),
        subtitle_format=subs.get("subtitle_format"),
        subtitle_source=subs.get("subtitle_source"),

        content_type=images.get("content_type", "video"),
        image_count=images.get("image_count"),
        image_urls=images.get("image_urls"),
        image_title=images.get("image_title"),

        is_ad=_bool(item.get("isAd")),
        is_aigc=_bool(item.get("IsAigc")),
        location_created=_clean(item.get("locationCreated")),
        diversification_labels=[d for d in
                                (item.get("diversificationLabels") or [])
                                if isinstance(d, str)] or None,
        anchor_keywords=[_clean(a.get("keyword")) for a in anchors
                         if _clean(a.get("keyword"))] or None,
        anchor_types=[_int(a.get("type")) for a in anchors
                      if _int(a.get("type")) is not None] or None,

        data_source="video_page", page=1, position=1,
    )
    return [row], diag


def merge_rows(base: Any, detail: Any) -> Any:
    """An embed row enriched with its video page's own row.

    The video page is a SUPERSET on every column both carry, so the detail
    row wins wherever it has a value — except `position`, which describes
    the embed window's order and is the only thing the detail page cannot
    know.
    """
    if detail is None:
        return base
    if base is None:
        return detail
    detail.position = base.position
    detail.page = base.page
    detail.data_source = "embed+video_page"
    return detail


# ---------------------------------------------------------------------------
# Refusal detection
# ---------------------------------------------------------------------------

SITE_ASSET_MARKERS = ("ttwstatic.com", "tiktokcdn.com", "tiktokcdn-eu.com")
MIN_ASSET_REFERENCES = 2


def asset_reference_count(html: Any) -> int:
    text = decode_page(html)
    return sum(text.count(m) for m in SITE_ASSET_MARKERS)


def challenge_markers_present(html: Any) -> List[str]:
    text = decode_page(html)
    return [m for m in BOT_CHALLENGE_MARKERS if m in text]


STATE_CONTENT = "content"
# The video or the account is gone, private, or was never there.
STATE_VIDEO_UNAVAILABLE = "video_unavailable"
STATE_EMPTY_SUCCESS = "empty_success"
STATE_CHALLENGE = "challenge"
STATE_ERROR = "error"
STATE_PARSE_ERROR = "parse_error"
STATE_UNKNOWN = "unknown"


def detect_page_state(html: Any, status: Optional[int] = None,
                      url: str = "") -> str:
    """Name what TikTok answered with.

    The argument ORDER is the contract: every caller writes
    `detect_page_state(html, status, url)` — CLAUDE.md §17, and
    `smoke_test.py` binds every call site against this signature.

    The CHECK order is by how much each signal PROVES, not by how cheap it
    is (§17's classification-order trap). The payload's own verdict
    outranks any threshold.
    """
    if is_empty_success(status, html):
        return STATE_EMPTY_SUCCESS

    text = decode_page(html)
    if status is not None and status >= 400:
        return STATE_ERROR
    if challenge_markers_present(text):
        return STATE_CHALLENGE

    # The embed application first — it is a different payload and a video
    # page will not have one.
    try:
        node = embed_node(text)
    except PayloadError:
        node = None
    if node is not None:
        if node.get("isError"):
            return STATE_VIDEO_UNAVAILABLE
        return STATE_CONTENT if node.get("videoList") else STATE_VIDEO_UNAVAILABLE

    try:
        scope = rehydration_scope(text)
    except PayloadError:
        scope = None
    if scope is not None:
        detail = scope.get(VIDEO_DETAIL_SCOPE)
        if detail is not None:
            item = (detail.get("itemInfo") or {}).get("itemStruct")
            if isinstance(item, dict) and item.get("id"):
                return STATE_CONTENT
            return STATE_VIDEO_UNAVAILABLE
        return STATE_UNKNOWN

    if asset_reference_count(text) >= MIN_ASSET_REFERENCES:
        return STATE_PARSE_ERROR
    return STATE_UNKNOWN
