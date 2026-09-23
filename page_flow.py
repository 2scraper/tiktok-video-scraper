"""page_flow.py — the retry / solve / blocked decision, as DATA.

Shared by all three browser engines and the HTTP path so they cannot
quietly disagree about whether a response is worth retrying, worth paying
a solver for, or worth reporting as a block. Three copies of that triage
drift, and the drift is silent: one engine reporting exit 3 where its twin
reports exit 0 on the same video (CLAUDE.md §1).

Policy and pure algorithms only. No JavaScript crosses this boundary —
Selenium's `execute_script` takes a function BODY with an explicit
`return` while Playwright and pyppeteer take `() => expr`, so a shared
module that carried a snippet would acquire one driver's dialect. What the
engines share here is a NAME for an operation and a decision about it.

TikTok answers a request six ways
---------------------------------
and five of them want a different response, which is why this module
exists on this site at all:

    content            an embed window with videos, or a video page with
                       an item on it
    video_unavailable  TikTok returned no video (a real answer)
    empty_success      HTTP 200, content-length 0 — a REFUSAL
    challenge          the slide-puzzle interstitial
    error              an HTTP status the site gave us
    parse_error        a page the site served that we failed to read — OUR bug

`empty_success` is the one a naive engine gets wrong, and it is the
defining shape of this site. Measured 2026-09-22 on the video-feed route
next door, every way it was asked — headless Chromium, headful Chromium, a
Windows user agent, after accepting the EU cookie consent, and through a
residential exit in Peru — TikTok answered:

    HTTP 200   content-type: application/json   content-length: 0

No status to key on, no body to parse, no marker to match. Folded into
"empty" it reads as an account with no data and the run reports exit 0;
named separately it rotates an exit and reports exit 3. This repo's own
route has not produced it, and it is carried anyway, because the cost of
being wrong about it is a silent wrong answer.

`video_unavailable` is the other one. A deleted video, a private account
and an id that never existed all answer HTTP 200 with a full app shell and
an empty `itemInfo` — a real answer to the question asked, not a block.
Reporting it as one sends a user rotating proxies over a video that is not
there.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from product_parser import (STATE_CHALLENGE, STATE_CONTENT,
                            STATE_EMPTY_SUCCESS, STATE_ERROR,
                            STATE_PARSE_ERROR, STATE_UNKNOWN,
                            STATE_WAF_CHALLENGE,
                            STATE_VIDEO_UNAVAILABLE, detect_page_state)

# ---------------------------------------------------------------------------
# Readiness — for the browser engines only
# ---------------------------------------------------------------------------
#
# The browser engines do not read the video out of the DOM — it is in
# the page SOURCE, in a script tag, server-rendered. So what they wait for
# is not a rendered grid but a document that has finished arriving. That is
# a much weaker requirement than most repos in this family have, and
# stating it here keeps an engine from growing a tile-counting wait that
# measures the wrong thing.
#
# A run that never sees the selector still works — the payload is parsed
# out of the source either way — so this is a wait, not a gate.
#
# `[data-e2e="browse-video"]` and `[data-e2e="video-desc"]` are the video
# page's own test ids, which are build artefacts and therefore listed
# because nothing more durable exists; `body` is the floor that always
# matches, which is why the minimum is 1 rather than the >1 CLAUDE.md §5
# requires of a LISTING. A video page holds exactly one video, and the
# embed window's payload is in the source, so "more than one match" is not
# a thing that needs waiting for here.
READY_SELECTOR = '[data-e2e="browse-video"], [data-e2e="video-desc"], body'
MIN_CARD_MATCHES = 1
CONTENT_TIMEOUT_MS = 30_000
READY_POLL_MS = 500


def ready_selector(mode: str = "videos") -> str:
    return READY_SELECTOR


def min_matches(mode: str = "videos") -> int:
    return MIN_CARD_MATCHES


def content_timeout_ms(mode: str = "videos") -> int:
    return CONTENT_TIMEOUT_MS


def wait_for_count(count: Callable[[str], int], selector: str, minimum: int,
                   timeout_ms: int = CONTENT_TIMEOUT_MS,
                   poll_ms: int = READY_POLL_MS) -> int:
    """Poll `count(selector)` until it reaches `minimum`, or time out.

    The caller passes a counting primitive rather than a snippet, and the
    primitive must be a protocol call — `querySelectorAll` through the
    driver — never an evaluated STRING. CLAUDE.md §18: a site whose
    Content-Security-Policy lacks `unsafe-eval` kills
    `wait_for_function`-style string evaluation. TikTok's CSP does carry
    `unsafe-eval` today (measured 2026-09-22), so this is insurance rather
    than a workaround — and it is also the only spelling all three drivers
    share, since Selenium takes a function BODY where the other two take
    `() => expr`.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    seen = 0
    while True:
        try:
            seen = count(selector)
        except Exception:
            seen = 0
        if seen >= minimum or time.time() >= deadline:
            return seen
        time.sleep(poll_ms / 1000.0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(html, status: Optional[int] = None, url: str = "",
             mode: str = "videos") -> str:
    """Name what TikTok answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every caller writes
    `classify(html, status, url)`. CLAUDE.md §17 records a sibling repo
    whose two engines called `classify(html, url=...)` against a callee
    taking `status` second, and both crashed on their FIRST fetch —
    invisible to import, `--help`, `compileall` and four hundred green
    offline assertions, because none of those calls a function the way a
    live run does. `smoke_test.py` binds every call site against this
    signature for exactly that reason.

    `status` is threaded through rather than dropped. It is the only
    signal a 503 or a 403 gives, and a classifier that never receives it
    has to guess from a body that may not exist — which, on this site, is
    literally the case: the refusal that matters here HAS no body.
    """
    return detect_page_state(html, status, url)


STATE_POLICY = {
    # A page with its video payload on it.
    STATE_CONTENT: {"retry": False, "solve": False, "blocked": False,
                    "parse": True},
    # TikTok returned no video. A real, complete answer to the question
    # asked — EXIT_NO_PRODUCTS, never EXIT_BLOCKED. Retrying it re-asks a
    # question the site has already answered, and rotating exits over it
    # spends a proxy budget on a video that is not there.
    #
    # `parse` is False because there is nothing to parse; the engine reads
    # the state itself to distinguish this from a failure when it counts
    # pages.
    STATE_VIDEO_UNAVAILABLE: {"retry": False, "solve": False, "blocked": False,
                              "parse": False},
    # HTTP 200 with a zero-length body — TikTok's silent refusal.
    #
    # `retry` True and `blocked` True: a different exit is the only thing
    # that has ever been worth trying against it. `solve` is False, and
    # that is the measured part rather than a default — this shape carries
    # no widget, no sitekey and no challenge page, so there is nothing for
    # a solver to solve and paying for one would be buying a request the
    # API will reject (CLAUDE.md §19: detected != paying).
    STATE_EMPTY_SUCCESS: {"retry": True, "solve": False, "blocked": True,
                          "parse": False},
    # The slide-puzzle interstitial. `solve` is False: this repo implements
    # no solver for ByteDance's puzzle, so a solve on this state would be a
    # promise with nothing behind it. `retry` and `blocked` are True: a
    # different profile is what has actually worked (tiktok-shop-scraper).
    STATE_CHALLENGE: {"retry": True, "solve": False, "blocked": True,
                      "parse": False},
    # TikTok's WAF interstitial: HTTP 200, 1,462 bytes, "Please wait...",
    # carrying a JavaScript challenge.
    #
    # `blocked` True is what makes `--transport auto` do the right thing:
    # the engines switch to a browser for any state that counts as
    # blocked, and a browser is the measured remedy — 3 of 3 cleared on
    # the very exits that refused a plain HTTP client.
    #
    # `solve` False, and that is measured rather than defaulted: the page
    # carries no widget, no sitekey and no captcha of any kind, so paying
    # a solver would buy a request the API cannot fulfil (CLAUDE.md §19:
    # detected != paying).
    STATE_WAF_CHALLENGE: {"retry": True, "solve": False, "blocked": True,
                          "parse": False},
    # An HTTP error that is not a recognised refusal — a 500, a gateway's
    # own page, a truncated body. A wait, not a spend.
    STATE_ERROR: {"retry": True, "solve": False, "blocked": False,
                  "parse": False},
    # A page the site plainly served, with its own assets all over it, that
    # this parser failed to read. OUR bug, and it gets its own name so it
    # cannot be reported as "no such video" — which would send the reader
    # to check the URL instead of the parser (CLAUDE.md §20).
    # One retry in case a response was truncated, and always worth a dump.
    STATE_PARSE_ERROR: {"retry": True, "solve": False, "blocked": False,
                        "parse": False},
    # Neither the site nor a recognised refusal — a proxy's own response,
    # Chromium's network-error page (which carries the site's hostname in
    # its title and would fool a title check), an upstream error.
    STATE_UNKNOWN: {"retry": True, "solve": False, "blocked": False,
                    "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by the
# engines rather than merely documented — setting it False really does stop
# the retry loop. (CLAUDE.md §17: a policy constant nothing reads is the
# same defect as dead code, and this family shipped one for months.)
RETRY_ON_BLOCKED = True

# Retries to spend on a refusal when there is no pool to rotate through.
# Without a pool every retry leaves from the same address that was just
# refused, so more than one is repetition rather than a second attempt.
BLOCK_RETRIES_WITHOUT_POOL = 1

# ---------------------------------------------------------------------------
# The solve budget
# ---------------------------------------------------------------------------
#
# One paid solve per page. CLAUDE.md §23 records that this constant has
# read like an enforced limit in every repo in this family and was not one:
# every engine calls the captcha handler TWICE per attempt — once before
# the response is classified, so a challenge is cleared before anything is
# judged, and once after, for the state that says the page really is gated
# — and only the second call was counted. Measured from an address where a
# challenge rendered on every fetch, one page bought THREE solves.
#
# So the budget is not a number engines are trusted to respect. It is a
# function both call sites go through, and `smoke_test.py` asserts that the
# number of call sites equals the number of guards equals the number of
# increments.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """One page's paid-solve allowance, shared by both call sites.

    `spend()` returns True at most SOLVES_PER_PAGE times and counts the
    spend itself, so neither caller can forget to.
    """

    def __init__(self, limit: int = SOLVES_PER_PAGE):
        self.limit = max(0, int(limit))
        self.spent = 0

    def may_spend(self) -> bool:
        return self.spent < self.limit

    def spend(self) -> bool:
        if not self.may_spend():
            return False
        self.spent += 1
        return True

    def __repr__(self) -> str:                # pragma: no cover - debugging
        return f"SolveBudget(spent={self.spent}/{self.limit})"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def pagination_is_addressable(url: str = "", mode: str = "video") -> bool:
    """Whether page N of this run can be fetched without walking to it.

    CLAUDE.md §18 says to ask this per ROUTE rather than per site, and on
    this repo the two modes answer DIFFERENTLY — which is exactly why the
    question is asked per mode rather than once per site.

      * `video` — YES. A "page" there is one VIDEO, and every video has a
        real, independent address that can be fetched without reading
        anything first. That is what the concurrency machinery needs.

      * `videos` — NO. An account's window is a single embed page, and
        which videos it names is unknowable until that page has been read.
        A second worker would have no address to fetch. The engine refuses
        `--concurrency` above 1 in that mode with exactly this reason
        rather than starting idle threads.

    The paginated feed that WOULD make an account addressable is
    `/api/post/item_list/`, and it answers HTTP 200 with a zero-length
    body to every client tried — so there is nothing to make addressable.
    """
    return mode == "video"


def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given what is known to exist.

    Here `pages_available` is the number of targets `--url` named, and
    capping at it matters: asking for the eleventh of ten videos is not an
    empty page, it is an index error waiting to happen.

    Neither route states a page count of its own. The embed window does not
    say how many videos the account has, and a video page is one video.
    """
    wanted = max(1, int(pages_requested or 1))
    if pages_available and pages_available > 0:
        return min(wanted, int(pages_available))
    return wanted


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (CLAUDE.md §7).
    """
    return 1 if cdp_endpoint else None


def concurrency_for_mode(mode: str, concurrency: int) -> int:
    """Workers this mode can actually use.

    `videos` walks one embed page before it knows any video's address, so
    a second worker would sit waiting for an address the first has not
    produced yet. Clamping here rather than starting idle threads keeps the
    run's own log honest about what it did.
    """
    if mode == "videos":
        return 1
    return max(1, int(concurrency or 1))


def sample_share(collected: int, total: Optional[int]) -> Optional[float]:
    """What fraction of an account's videos a run actually holds.

    CLAUDE.md §21: "complete" and "exhaustive" are different words, and on
    THIS route the gap is the whole story. TikTok's embed window is ten to
    twelve videos however many the account has posted — @khaby.lame's
    window is 10 of 1,353 — so a run of it is genuinely complete for the
    route and is well under 1% of the account. The sidecar carries the
    window size and the reason the rest is unreachable; this is the
    arithmetic when a caller knows the account's total.
    """
    if not total or total <= 0 or collected < 0:
        return None
    return round(100.0 * collected / total, 4)
