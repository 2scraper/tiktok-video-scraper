# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count and lists any group it skipped because an engine
library is absent.

**The suite must pass with no engine installed at all.** CI installs only the
core requirements, so any import of `playwright_scraper`, `puppeteer_scraper`
or `selenium_scraper` in a check sits inside `try/except ImportError` with the
skip recorded. If the suite fails on a clean clone, that is itself the bug —
say so.

## Never commit a credential

`.env` and every `.env.*` variant except `.env.example` are in `.gitignore`.
Keep them there.

The engines mask `user:pass@` in their own log lines, but three things are
**not** masked: raw page dumps (`--dump-html`), the Scraper API's `x-debug`
response header, and your shell history. Before pasting output into an issue
or a PR, replace keys, proxy passwords and full `ws://user:pass@host:9222`
endpoints with `***`.

CI fails the build if something credential-shaped is committed. That is a
backstop, not a review.

## Reporting a site change

This repo reads TikTok videos, captions and media links from `www.tiktok.com/embed/@handle and /@handle/video/{id}`, which is both routes are served to a bare HTTP client from a datacentre address — no key, no proxy, no account.

The parser reads one structured source and never the rendered DOM:

    `__FRONTITY_CONNECT_STATE__` on the embed page, `__UNIVERSAL_DATA_FOR_REHYDRATION__["webapp.video-detail"]` on a video page

So a site change almost always shows up as that source moving or its shape
changing, and the most useful thing a report can carry is the source itself
from a dump — there is an issue template for exactly that.

## What the checks pin, and why

Each of these cost real time when it was found, and the offline suite pins it
so a PR that undoes one fails rather than silently regressing:

- **Never read a caption's entities out of the caption.** `textExtra.start/end` index in UTF-16 code units (27 of 85 entities mis-sliced by Python indexing), and the text after an `@` is a display name, not a handle — "@sadie" is a mention of `sad_i_e`, and `@sadie` is a different real account. The suite asserts the parser contains no code that slices the caption.

- **An account's window is ten to twelve videos, and that is all.** The embed route ignores `?page=` and its payload keeps saying `page: 1`; the paginated feed answers HTTP 200 with a zero-length body. `--pages` above 1 is refused, and every sidecar carries `window_is_a_sample`.

- **A photo post is not a broken video row.** Duration, width and height all 0 and no `playAddr`. Named from TikTok's own `imagePost` key, never from the zeroed fields — a threshold on those would call a 0-second video a photo.

- **An author's follower count is rounded on a video page.** `authorStatsV2` there says 79,700,000 where the profile page publishes the exact figure, so this repo carries no author counts at all.

- **Every media URL expires.** `media_expires_at` is read out of the URL, and `diff_runs.py` tracks none of the URLs — they would otherwise report every video as changed on every run.

Before adding a challenge marker, count it on a page you **know** was served.
A marker that matches every page is worse than no marker.

## Before a release

```bash
python3 smoke_test.py
python3 .github/ci_checks.py --history-check
```

The second applies the credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. A commit on top cannot reach what a
published tag already holds.

The canary is **not** gated on a secret: both routes are served to a bare GitHub runner, so it runs a real enriched scrape daily and is expected GREEN.

## Pull requests

Add a check for the behaviour you are changing. `smoke_test.py` is a single
file of plain functions; copy the nearest existing check and edit it. Keep the
three engines identical above their driver layer — a check compares their
public surfaces and flag sets in both directions.
