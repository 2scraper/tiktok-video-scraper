"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers used by all three engines and the
HTTP path.

One mode, one row shape
-----------------------
    --mode profile   a TikTok account's public statistics and settings

This repo reads one KIND of thing, so it carries one dataclass. Its two
siblings — tiktok-video-scraper and tiktok-shop-scraper — read different
kinds and carry their own, and all three keep the family prefix
`source, scraped_at, url, sku, title` byte-identical and first
(CLAUDE.md §9). `mode` is recorded in the run sidecar anyway, so a
consumer holding three files from three repos can tell them apart without
knowing which repo wrote which.

`sku` is the id, as everywhere in this family: here the account's
permanent numeric id, not its @handle. TikTok lets a user change the
handle and publishes `uniqueIdModifyTime` to say when they last did, so a
diff joined on the handle would report a rename as a deletion plus an
arrival.

Why every count carries its provenance
--------------------------------------
TikTok publishes each account statistic TWICE and the two disagree.
Measured 2026-09-22 over 15 captured profiles:

    account        stats.followerCount   statsV2.followerCount   error
    @tiktok                  95,900,000              95,856,713   +43,287
    @khaby.lame             163,000,000             162,986,107   +13,893
    @nasa                     1,800,000               1,785,290   +14,710
    @charlidamelio          160,200,000             160,206,612   -6,612
    @zachking                86,900,000              86,900,407      -407

`stats` is rounded to three significant figures and rounds BOTH WAYS, so a
consumer cannot correct for it and cannot even tell which direction to
distrust. `statsV2` is exact. Every row records which object it was read
from in `stats_source`, and `diff_runs.py` reports a count difference that
comes with a `stats_source` difference as `source_changed` rather than as
the account having changed — the same argument this family makes for
`price_source` on its shops.

The counts also MOVE. Nine refetches of one profile within an hour on
2026-09-22 returned 1,785,290 … 1,785,310 followers, all different, none
wrong. So a canary asserts a floor and a range, never an equality, and two
runs of this scraper differing by a few hundred followers is the site
working rather than a bug.

Everything below the dataclass is row-class-agnostic: pass `row_cls` so an
empty CSV still gets the right header for the mode that produced it.
"""

import contextlib
import csv
import json
import os
import tempfile
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The site a row came from. TikTok serves the same account on
# `www.tiktok.com`, `m.tiktok.com` and the `vm.tiktok.com` short links,
# and all of them redirect into one canonical `/@handle` path; this column
# names the SITE, and which host a URL was given as is recoverable from
# `url`.
SOURCE_DEFAULT = "tiktok.com"


def utc_now() -> str:
    """The run's timestamp, as a UTC ISO-8601 string with a `Z`.

    One helper so every row in a run can be given the SAME stamp by the
    caller rather than each row calling the clock. Rows from one page that
    disagree in `scraped_at` by a few milliseconds make a diff noisier for
    no information — and on this site the stamp does more work than usual,
    because every derived comment date is measured backwards from it.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Video:
    """One TikTok video.

    The family prefix — `source`, `scraped_at`, `url`, `sku`, `title` — is
    byte-identical and in this order across every repo in the family
    (CLAUDE.md §9). `sku` is the video id, which TikTok never reuses and
    never lets a creator change. `title` is the caption, because a TikTok
    video has no title of its own and the caption is what a CSV of ten
    thousand videos is read by; `description` carries the same text and is
    kept so a consumer that expects one name or the other finds it.
    """

    source: str = SOURCE_DEFAULT
    scraped_at: str = ""
    url: Optional[str] = None
    # The video id.
    sku: Optional[str] = None
    # The caption, same value as `description`.
    title: Optional[str] = None

    # ---- what it is -------------------------------------------------------
    video_id: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[str] = None

    # ---- who made it ------------------------------------------------------
    # The handle, and the permanent numeric id beside it — a creator can
    # change the handle, so a cross-run join uses the id.
    author_username: Optional[str] = None
    author_id: Optional[str] = None
    author_nickname: Optional[str] = None
    author_sec_uid: Optional[str] = None
    author_verified: Optional[bool] = None
    #
    # `author_follower_count` is deliberately ABSENT. A video page does
    # carry `authorStatsV2`, and on this route it is ROUNDED — measured
    # 2026-09-22 on @therock's page: "79700000" followers and "685700000"
    # likes, three significant figures, where that account's own profile
    # page publishes the exact figures. So the one place those numbers are
    # trustworthy is tiktok-profile-scraper, and carrying a rounded copy
    # here under an authoritative-looking name would be worse than not
    # carrying one.

    # ---- engagement -------------------------------------------------------
    # From `statsV2` where it exists, `stats` otherwise; `stats_source`
    # says which. Unlike a profile's counts, these two objects AGREE on a
    # video page in every capture measured — but `playCount` is rounded by
    # TikTok in BOTH of them, which is why it is called out below.
    play_count: Optional[int] = None
    digg_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None
    collect_count: Optional[int] = None
    repost_count: Optional[int] = None
    stats_source: Optional[str] = None

    # ---- what kind of post it is --------------------------------------------
    # "video" or "photo". TikTok's photo mode publishes a CAROUSEL of
    # stills under the same `/video/{id}` URL, with `duration`, `width`
    # and `height` all 0 and no `playAddr` at all — so a row for one looks
    # like a broken video row unless the kind is named. 2 of 29 posts
    # measured 2026-09-22 across three accounts were photo posts.
    #
    # Detected by the presence of TikTok's own `imagePost` key, which is a
    # structural signal rather than a threshold on the zeroed fields
    # (CLAUDE.md §17: order signals by how much they prove).
    content_type: Optional[str] = None
    image_count: Optional[int] = None
    image_urls: Optional[List[str]] = None
    # A photo post carries its OWN title, separate from the caption. On the
    # post measured, `desc` was "Roman's first images will be so large..."
    # and this was "What's next for NASA's Roman Space Telescope?" — two
    # different strings, both authored.
    image_title: Optional[str] = None

    # ---- the media --------------------------------------------------------
    # All zero on a photo post; see `content_type`.
    duration_seconds: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    ratio: Optional[str] = None
    definition: Optional[str] = None
    video_format: Optional[str] = None
    video_size_bytes: Optional[int] = None
    bitrate: Optional[int] = None

    # Direct media URLs, and they are SIGNED and PERISHABLE — the same
    # shape as a profile's avatar. `media_expires_at` is read out of the
    # URL's own expiry parameter so a consumer storing one knows how long
    # it has. Competitors sell these as a premium; TikTok publishes them.
    play_addr: Optional[str] = None
    download_addr: Optional[str] = None
    cover_url: Optional[str] = None
    dynamic_cover_url: Optional[str] = None
    media_expires_at: Optional[str] = None

    # ---- the sound --------------------------------------------------------
    music_id: Optional[str] = None
    music_title: Optional[str] = None
    music_author: Optional[str] = None
    music_is_original: Optional[bool] = None
    music_duration_seconds: Optional[int] = None

    # ---- what the caption points at ----------------------------------------
    # Read from TikTok's OWN entity list, never by slicing the caption.
    # Two measurements on 85 entities from real videos, 2026-09-22, and
    # each one on its own is enough to make text extraction wrong:
    #
    #   * the offsets index in UTF-16 code units. 27 of 85 entities came
    #     out mis-sliced under Python string indexing — one of them as
    #     'tRock 10.0 🗡' where the entity is '@ProjectRock';
    #
    #   * the text after an @ is a DISPLAY NAME, not a handle. A caption
    #     reading "@sadie" is a mention of the account `sad_i_e` — and
    #     `@sadie` is ALSO a real, different account. So a regex over the
    #     caption does not fail, it attributes the mention to somebody
    #     else's live account. 14 of 85.
    hashtags: Optional[List[str]] = None
    mentions: Optional[List[str]] = None

    # ---- captions ----------------------------------------------------------
    # TikTok publishes downloadable WebVTT for many videos — 6 of 11 on one
    # account measured 2026-09-22. `subtitle_source` is TikTok's own word
    # for how the caption was made, and both values seen so far are
    # machine-generated: "ASR" (speech recognition) and "MT" (machine
    # translation). Worth knowing before quoting one as a creator's words.
    # The URL is signed and perishable like every other TikTok media link.
    subtitle_languages: Optional[List[str]] = None
    subtitle_url: Optional[str] = None
    subtitle_format: Optional[str] = None
    subtitle_source: Optional[str] = None

    # ---- what TikTok says about it -----------------------------------------
    is_ad: Optional[bool] = None
    is_aigc: Optional[bool] = None
    #
    # `aigc_description` was here and is GONE. TikTok publishes
    # `AIGCDescription` on every item and it was EMPTY on all 29 posts
    # measured across three accounts on 2026-09-22, while `IsAigc` was
    # populated on all 29. CLAUDE.md §9: a column null on every row of
    # every run should not exist, and removing it needs the measurement
    # written down so somebody can add it back with a better one — most
    # likely from an account that actually labels AI content.
    location_created: Optional[str] = None
    diversification_labels: Optional[List[str]] = None
    # Linked cards on the video — a CapCut template, a TikTok Shop product.
    # Their `type` is TikTok's own integer and is passed through rather than
    # mapped to words this repo would have to invent.
    anchor_keywords: Optional[List[str]] = None
    anchor_types: Optional[List[int]] = None

    # ---- provenance ---------------------------------------------------------
    # "embed" | "video_page" | "embed+video_page". The two sources carry
    # DIFFERENT fields, so a diff between a fast run and an enriched run
    # would otherwise report every absent column as a change.
    data_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None


# The family name. CLAUDE.md §9 makes `Product` the schema every repo in
# this family exports, and several shared checks import it by that name.
Product = Video


# Row classes by --mode, so an engine maps its mode to a schema in one
# place.
ROW_CLASS_BY_MODE = {"videos": Video, "video": Video}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py.
#
# A profile run emits one row per account and an account id is globally
# unique, so a drop during dedupe means the same handle was asked for
# twice in one run — worth a log line, never worth silently absorbing.
UNIQUE_BY_SKU_MODES = ("videos", "video")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a repeated page then re-parses without duplicating its rows into the
    final output.

    On YouTube a drop here is unexpected but not impossible, which is why
    the count is logged rather than quietly applied. Sixty consecutive
    pages of one video returned 1,200 comment ids and 1,200 distinct ones
    on 2026-09-21, so adjacent pages do not overlap by design.

    What CAN produce a duplicate is the ranking moving underneath a long
    run: `--sort top` is a live relevance ordering, and a comment that
    gains likes between page 3 and page 30 can be served twice. That is a
    fact about the site worth seeing in a log rather than silently
    absorbing, and it is also the reason a long run is a sample rather than
    a snapshot.
    The function stays regardless — it is the backstop that keeps the output
    clean, and "should never fire" is a poor reason to remove a guard that
    costs one pass over a list.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    All three of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


@contextlib.contextmanager
def _atomic(path: str, newline: Optional[str] = None):
    """Write to a temporary file beside `path`, then rename over it.

    Every write here replaces a file a previous run may have left, and the
    invariant this module exists to protect is that a bad run never
    destroys last night's good data (`save` refuses to overwrite with an
    empty result for the same reason). Writing in place gives that up at
    the worst moment: a kill, a full disk or a crash halfway through
    `json.dump` leaves a TRUNCATED file where a complete one was, and the
    sidecar beside it still describes the old, good run.

    `os.replace` is atomic on POSIX and on Windows, so a reader sees
    either the whole previous file or the whole new one and never half of
    either. The temporary file is created in the SAME directory, because a
    rename across filesystems is not atomic and would silently degrade to
    a copy.

    `fsync` before the rename is what makes that true after a power loss
    rather than only after a crash — without it the rename can reach the
    disk before the bytes do.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline=newline, dir=directory,
        prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        # Leave the destination untouched. A failed write must not be
        # visible at all, which is the whole point of writing aside.
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def write_json(rows: Sequence[Any], path: str) -> None:
    with _atomic(path) as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Video) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with _atomic(path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On YouTube this code does NOT cover the two states that look like it and
# are not. A video whose comments are TURNED OFF answers HTTP 200 with a
# comment section holding a message instead of a token: the request was
# served exactly as asked, the answer is that there are no comments, and
# that is EXIT_NO_PRODUCTS. A video that does not exist or has been taken
# down answers 200 with `backgroundPromoRenderer` and no comment section at
# all — also not a block. Reporting either as blocked sends a user hunting
# for a proxy problem that does not exist.
#
# What EXIT_BLOCKED would mean here is largely unmeasured, and saying so is
# more use than inventing a description. Measured 2026-09-21 from a bare
# Finnish datacentre address with no proxy and no key: 60 consecutive
# InnerTube pages, all HTTP 200, no refusal of any kind. Every candidate
# text marker counted on known-good captures fired on GOOD pages —
# `consent.youtube.com` 4 times, `botguard` 13, `recaptcha` once — so none
# of them is carried (CLAUDE.md §18).
#
# What IS carried is the HTTP status (401/403/429) and the two sentences
# YouTube is documented to use when it demands a sign-in. Neither sentence
# has been observed from here, and they are marked unverified in
# product_parser rather than described as measured. If a run reports exit
# 3, the saved debug payload is the evidence, and it is new.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    # Atomic for the same reason the row files are, and one reason more:
    # this file is what a consumer branches on, so a truncated sidecar is
    # worse than none at all — it parses as far as it parses and then
    # raises, next to data that is perfectly fine.
    with _atomic(path) as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "videos", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listings run, a job run or a
    careers run, and those populate different columns — a listings row has
    the site's `domain` and its slot counters, a job row has the company
    website and a currency, a careers row has a department and an Ashby
    apply URL. `diff_runs.py` refuses a pair whose modes or sources differ,
    which matters more here than on most sites in this family: a careers run
    and a marketplace run have NO ids in common at all, so a diff of the two
    would report every row as both added and removed.

    `source` is `youtube.com` on every row of every run. The site answers
    on four hosts and this column names the SITE rather than the host, so
    one value covers all of them; which host a URL was given as is
    recoverable from `url`. It is kept because consumers read these columns
    by name across the family.

    `extra` carries facts about the run that are not about any single row,
    and on this site the most important one is how small a run is. YouTube
    states its own total in the comment section header — 2,457,619 on the
    video used for this repo's fixtures — so `extra` records
    `total_comments`, `comments_collected` and the percentage between them,
    plus `sort`, the `client_version` the run spoke and whether replies
    were expanded.

    That is the only honest way to say what a run holds, because "complete"
    and "exhaustive" come apart badly here (CLAUDE.md §21). A 5-page run
    fetched every page it was asked for and is genuinely `complete`. It is
    also 100 comments out of two and a half million, which is 0.004% — and
    nothing in the row count reveals that.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # Named "products" even though these are job listings, and kept that
        # way deliberately: every repo in this family writes this key, and a
        # consumer reading several of them reads one sidecar shape.
        # quora-scraper made the same call for answers. The row TYPE is
        # `mode` plus `source`, which are right beside it.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Video) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue.
#
# On YouTube the third signal is the strongest one available, and it is
# neither of those: the site hands out the NEXT page's token inside the
# page it just served. There is no `?page=N` to construct and no selector
# to go stale — a response either carries a continuation token or it is the
# last page, and "pagination_exhausted" means the site said so itself.
#
# That is also why this repo cannot plan page URLs ahead (CLAUDE.md §7):
# page 5's token is unknowable until page 4 has been read, so a comments
# run is strictly sequential and `--concurrency` above 1 is refused for it
# with that reason. `--mode video` parallelises across VIDEOS instead,
# which is the unit that actually has independent addresses.
#
# "page_cap_reached" fires when `--pages` runs out with the site still
# offering more, which is the normal end of a run here.
# "page_echo_mismatch" is carried for the family's shared vocabulary and
# cannot fire: this site is never asked for a page by number, so it has no
# number to echo back.
#
# "single_page_route" is what a `--mode video` run reports: one video is
# one response, and there is no second page of it to miss.
#
# Note what it does NOT mean on this site: a `complete` comments run holds
# every page it asked for, which is almost never every comment the video
# has. CLAUDE.md §21 — complete and exhaustive are different words — and
# the sidecar records the site's own total beside the collected count so a
# consumer is not left inferring one from the other.
# `video_unavailable` is in this set, and it has to be. It describes a run
# that ASKED and got a real answer — TikTok returned no video for the id,
# or no videos for the account — so the honest code for a zero-row run is
# 4 ("we asked, and the answer was nothing") and not 5 ("we never got the
# content").
#
# `empty_success` is deliberately NOT in it. That one is TikTok's HTTP 200
# with a zero-length body, which looks like an answer and is a refusal; a
# run that saw only those got nothing and must say so.
#
# Left out, they cost exactly that: measured 2026-09-22, both returned
# exit 5, where the same two URLs returned 4 the day before. The exit-5
# rule that introduced it is right and stays — it keys on `not complete`
# rather than on a list of failure names, precisely so a new reason cannot
# fall silently through to "the catalogue is empty". What was wrong was
# this set, which described only the ways a PAGINATION LOOP can end and
# not the ways a site can answer.
#
# The lesson generalises past these two names: when a rule keys on "is
# this reason complete", every reason has to be classified, including the
# ones that are complete answers about an empty result. A reason nobody
# added here defaults to "we failed", which is the opposite of silent but
# is still wrong.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "page_cap_reached", "page_echo_mismatch",
                         "single_page_route", "video_unavailable")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "videos", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence, and the
    # second half is the fix. `stop_reason` is a NAMED LIST, and a named
    # list cannot cover a failure that was recorded somewhere else — which
    # is exactly what happened: a run whose top-level pages all arrived
    # stops for `page_cap_reached`, a COMPLETE reason, while
    # `pages_failed` holds the reply threads that did not. Measured before
    # the fix: `finish_run(rows, stop_reason="page_cap_reached",
    # pages_failed=[3, 7])` returned exit 0 with `status: complete` and a
    # two-entry `pages_failed` in the same sidecar — a file that
    # contradicts itself, and a pipeline that branches on `status` reading
    # a short run as a whole one.
    #
    # Same shape as the exit-code unification this file already carries: a
    # rule keyed on a list of names has a hole for every name nobody added
    # to it, so key on the thing that is actually true instead. If a page
    # failed, the run is not complete, whatever it stopped for.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Video)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
