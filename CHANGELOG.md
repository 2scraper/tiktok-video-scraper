# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) as
closely as a CLI toolkit can. A patch release means **fixes** — it does not
promise that every flag's default is frozen, and where a default does
change in one, the note leads with it.

## [Unreleased]

### Fixed

> **`diff_runs.py` compared almost nothing.** Its `TRACKED_FIELDS` were
> tiktok-profile-scraper's account columns (`follower_count`, `bio`,
> `is_seller`, …), 26 of which `Video` does not have, so a diff of two runs
> reported "0 changed" whenever only a count, a hashtag or the sound had
> changed. It now tracks this repo's own columns, and a `data_source`
> difference (an `--no-enrich` embed row against a full video-page row) is
> reported as `source_changed`, which is what the README already said.
> `smoke_test.py` now pins every tracked name against the dataclass and
> checks that a changed column is actually reported.

- **Donor prose removed from the shared core.** `output_writer.py`,
  `diff_runs.py`, the engines, `page_flow.py`, `smoke_test.py`,
  `.github/ci_checks.py` and the `Dockerfile` carried text from the repos
  this core was copied from — YouTube comment threads, `--sort top`,
  reply threads, job listings, "the business", `--mode comments --out
  software-engineer` — describing those sites as if they were this one.
  Rewritten from this repo's own README, code and fixtures, or deleted
  where there was no measured equivalent. Explicit sibling provenance
  ("measured on tiktok-profile-scraper's route", "a sibling repo
  (youtube-scraper) had…") is kept and now says whose it is.
- The engines' docstrings and the Scraper API client's challenge message
  said "the profile route" where this repo reads the embed and video
  pages; `--locale` help and the CSP note now say the measurement was
  tiktok-profile-scraper's.
- `smoke_test.py`'s docstring described the profile repo's fixtures; it
  now describes this repo's (`embeds`, `videos`), and `run_meta`'s test
  data is a `videos` run rather than a `profile` one.
- `.github/ci_checks.py` no longer exempts an `avatar_id` column this repo
  does not have from the credential scan.
- `.env.example` described the profile route and a handle-only `TIKTOK_URL`.

- `captcha_solver.py`'s docstring pointed at a "No DataDome solver" section
  that does not exist in this repo (it came with the copied core). Removed.

## [0.1.1] — 2026-09-23

> **Correction to v0.1.0.** Its `captcha_solver.py` docstring described a
> 2Captcha captcha-solving method for TikTok as available. That method is
> deprecated, and the text no longer offers it. The challenge policy for
> TikTok's slide puzzle now says `solve: False`, which matches what the
> code does: no solver for it is implemented.

## [0.1.0] — 2026-09-22

First release. Reads TikTok videos and their captions from the two routes
TikTok serves to anyone.

### The measurement this release exists to get right

**Never read a caption's hashtags or mentions out of the caption.** 85
entities from real videos, 2026-09-22, and either half alone is enough:

* `textExtra.start/end` index in **UTF-16 code units**. 27 of 85 came out
  mis-sliced under Python string indexing — one as `tRock 10.0 🗡` where
  the entity is `@ProjectRock`.
* the text after an `@` is a **display name, not a handle**. 14 of 85. A
  caption reading `@sadie` is a mention of `sad_i_e`, and `@sadie` is also
  a real, different account — so a regex over the caption does not fail, it
  attributes the mention to somebody else's live account.

`hashtags` and `mentions` come from TikTok's own fields. The offsets are
not used at all, and the suite asserts the parser contains no code that
slices the caption.

### Added

- `--mode videos` (an account's recent window) and `--mode video` (named
  videos), over four interchangeable paths: Playwright, Selenium,
  Puppeteer, and the 2Captcha Scraper API. Verified to produce identical
  rows.
- `--enrich` / `--no-enrich`. The embed route gives 10–12 videos with
  captions and media URLs in 0.98 s; enriching each from its own page costs
  5.92 s and adds the likes, comments, shares, date, sound, hashtags and
  caption tracks. On by default, because without it the rows are a caption
  and a rounded play count.
- **Photo posts.** TikTok photo mode publishes a carousel under the same
  `/video/{id}` URL with duration, width and height all 0 and no
  `playAddr`, which reads exactly like a broken video row. 2 of 29 posts
  measured. Those rows carry `content_type`, `image_count`, `image_urls`
  and `image_title` — the last being a separate string from the caption.
- **Caption tracks.** 6 of 11 videos on one account carry downloadable
  WebVTT. `subtitle_source` records how it was made; both values seen are
  machine-generated (`ASR`, `MT`).
- **Media URLs with their expiry.** `play_addr`, `download_addr`, the
  covers and the stills are all signed and re-minted per request, so
  `media_expires_at` is read out of the URL and `diff_runs.py` tracks none
  of the URLs themselves.
- `data_source` on every row (`embed` / `video_page` / `embed+video_page`),
  because the two sources carry different fields and a diff across them
  would otherwise report every absent column as a change.

### Deliberately not here

- **An account's back catalogue.** The paginated feed
  (`/api/post/item_list/`) answers HTTP 200 with `content-length: 0` to
  every client tried, with a correctly signed request. The embed route
  serves one fixed window and ignores `?page=` — page 2 and page 3 return
  the same videos and the payload still says page 1. `--pages` above 1 is
  refused, and every sidecar carries `window_is_a_sample` and
  `why_not_exhaustive`.
- **An author's follower count.** A video page carries `authorStatsV2` and
  on that route it is **rounded** — 79,700,000 where the account's own
  profile page publishes the exact figure. `tiktok-profile-scraper` reads
  the route where it is exact.
- **`aigc_description`.** TikTok publishes `AIGCDescription` on every item
  and it was empty on all 29 posts measured, while `IsAigc` was populated
  on all 29. The column was removed rather than shipped null on every row;
  the measurement is in `output_writer.py` so it can be added back.

### Found while building, and fixed

- An assembled engine carried **two copies of its whole shared layer** —
  ten functions, four hundred lines — with Python binding the second and
  discarding the first. Invisible to import, `--help`, `compileall`, the
  undefined-name walk, and to the engines agreeing with each other, because
  the copies were identical. Caught by a check counting solver call sites,
  which found four where the design has two. Both this repo and its sibling
  now assert that no module defines a name twice.
- The credential scan's 32-hex exemption was rewritten three times before
  landing on a rule that holds: a hex is exempt when it sits **inside a URL
  whose host TikTok owns**. Enumerating path shapes was losing —
  `/<hex>~tplv`, then `<host>/<hex>/`, then a `signature=` parameter, then
  a deeper path segment. The fixtures were also trimmed to the fields the
  parser reads, which removed TikTok's transcode metadata entirely rather
  than forgiving it.

### Known limitations

- The challenge-marker set is **not** verified against a page fetched over
  `--cdp-endpoint`; every Scraping Browser profile available while this
  repo was built had expired (`401 deny_no_user`). The suite records this
  as a SKIP rather than passing silently.
- Hashtag feeds and keyword search server-render **zero** videos and the
  XHR that fills them is refused, so those URLs are declined with that
  reason rather than fetched.
