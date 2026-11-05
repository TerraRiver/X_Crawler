from __future__ import annotations

import asyncio
import html
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    async_playwright,
)

from stealth import (
    STEALTH_INIT_SCRIPT,
    human_idle_reading_gesture,
    human_move_and_click,
    human_reset_gaze,
    human_timeline_scroll,
    human_wait,
)

load_dotenv()


# --- Config ---
def _default_chrome_path() -> str | None:
    """Detect local Google Chrome executable path."""
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def _parse_date_bound(raw: str | None, is_end: bool = False) -> str | None:
    """Normalize YYYY-MM-DD (or YYYY/MM/DD or ISO 8601) into UTC ISO 8601 string."""
    if not raw or not raw.strip():
        return None
    val = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(val, fmt)
            suffix = "T23:59:59Z" if is_end else "T00:00:00Z"
            return dt.strftime("%Y-%m-%d") + suffix
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        print(f"[!] 环境变量时间格式无效：{val!r}（请使用 YYYY-MM-DD 格式，如 2025-01-01），已忽略该参数")
        return None


TARGET_USER          = os.getenv("TARGET_USER", "bhupen07").strip().lstrip("@").strip()
DATA_DIR             = Path(os.getenv("DATA_DIR", "data").strip() or "data")
COOKIE_FILE          = "browser_cookies.json"
PROXY                = os.getenv("PROXY") or None
CHROME_PATH          = os.getenv("CHROME_PATH") or _default_chrome_path()
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR", ".chrome_profile").strip()
HEADLESS_ENV         = os.getenv("HEADLESS", "").strip().lower()
SINCE_DATE_RAW       = os.getenv("SINCE_DATE", "").strip()
UNTIL_DATE_RAW       = os.getenv("UNTIL_DATE", "").strip()
SINCE_BOUND          = _parse_date_bound(SINCE_DATE_RAW, is_end=False)
UNTIL_BOUND          = _parse_date_bound(UNTIL_DATE_RAW, is_end=True)

# Per-user output directory: data/<TARGET_USER>/
USER_DATA_DIR = DATA_DIR / TARGET_USER
OUTPUT_FILE   = USER_DATA_DIR / "tweets.json"
PROFILE_FILE  = USER_DATA_DIR / "profile.json"
STATE_FILE    = USER_DATA_DIR / "state.json"

SCROLL_PAUSE  = 3.0   # seconds to wait for GraphQL response per scroll
MAX_NO_NEW    = 12    # stop after this many consecutive scrolls with no new timeline items
SCROLL_PIXELS = 2500
SAVE_INTERVAL = 10    # auto-save every N new qualifying tweets

_GRAPHQL_TIMELINE_RE = re.compile(
    r"/graphql/[^/]+/(?:User(?:Originals|Reposts|Tweets|Replies|Posts|Media)|Profile)\w*Timeline\w*"
    r"|/graphql/[^/]+/UserTweets(?:AndReplies)?"
)
_GRAPHQL_USER_PROFILE_RE = re.compile(r"/graphql/[^/]+/UserByScreenName")

# Lightweight in-page XHR hook to swap the first page's Bottom cursor when resuming from checkpoint
_XHR_CURSOR_HOOK_SCRIPT = """
(() => {
    if (window.__X_CRAWLER_HOOKED__) return;
    window.__X_CRAWLER_HOOKED__ = true;

    const TIMELINE_RE = /\\/graphql\\/[^/]+\\/(?:User(?:Originals|Reposts|Tweets|Replies|Posts|Media)|Profile)\\w*Timeline\\w*|\\/graphql\\/[^/]+\\/UserTweets(?:AndReplies)?/;
    const origOpen = XMLHttpRequest.prototype.open;
    const origRespTextDesc = Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype, 'responseText');
    const origRespDesc = Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype, 'response');

    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this.__x_url = typeof url === 'string' ? url : String(url);
        return origOpen.call(this, method, url, ...rest);
    };

    function swapBottomCursor(rawText, xhr) {
        if (xhr.__x_swapped_text) {
            return xhr.__x_swapped_text;
        }
        if (!window.__X_RESUME_CURSOR__ || !xhr.__x_url || !TIMELINE_RE.test(xhr.__x_url)) {
            return rawText;
        }
        if (window.__X_RESUME_ENDPOINT_SUBSTR__ && !xhr.__x_url.includes(window.__X_RESUME_ENDPOINT_SUBSTR__)) {
            return rawText;
        }
        try {
            const data = JSON.parse(rawText);
            const targetCursor = window.__X_RESUME_CURSOR__;
            let replaced = false;
            const walk = (obj) => {
                if (obj && typeof obj === 'object') {
                    if (obj.cursorType === 'Bottom' && typeof obj.value === 'string') {
                        obj.value = targetCursor;
                        replaced = true;
                    }
                    for (const k of Object.keys(obj)) {
                        walk(obj[k]);
                    }
                }
            };
            walk(data);
            if (replaced) {
                window.__X_RESUME_CURSOR__ = null;
                xhr.__x_swapped_text = JSON.stringify(data);
                xhr.__x_swapped_json = data;
                return xhr.__x_swapped_text;
            }
        } catch (e) {}
        return rawText;
    }

    if (origRespTextDesc && origRespTextDesc.get) {
        Object.defineProperty(XMLHttpRequest.prototype, 'responseText', {
            get: function() {
                const raw = origRespTextDesc.get.call(this);
                return typeof raw === 'string' ? swapBottomCursor(raw, this) : raw;
            }
        });
    }

    if (origRespDesc && origRespDesc.get) {
        Object.defineProperty(XMLHttpRequest.prototype, 'response', {
            get: function() {
                const raw = origRespDesc.get.call(this);
                if (typeof raw === 'string') {
                    return swapBottomCursor(raw, this);
                }
                if (this.__x_swapped_json) {
                    return this.__x_swapped_json;
                }
                if (raw && typeof raw === 'object' && window.__X_RESUME_CURSOR__ && this.__x_url && TIMELINE_RE.test(this.__x_url)) {
                    swapBottomCursor(JSON.stringify(raw), this);
                    if (this.__x_swapped_json) return this.__x_swapped_json;
                }
                return raw;
            }
        });
    }
})();
"""


# ---------------------------------------------------------------------------
# GraphQL Tweet, Cursor & Profile Parsers
# ---------------------------------------------------------------------------

def _parse_twitter_date(raw_date: str) -> str:
    """Convert X's 'Wed Oct 10 20:19:24 +0000 2018' into ISO 8601 UTC string."""
    if not raw_date:
        return ""
    try:
        dt = datetime.strptime(raw_date, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return raw_date


def _unwrap_tweet(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Unwrap TweetWithVisibilityResults and filter out unavailable/tombstone objects."""
    if not isinstance(result, dict):
        return None
    typename = result.get("__typename")
    if typename == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    elif typename in ("TweetTombstone", "TweetUnavailable", "TweetPreviewDisplay"):
        return None
    if not isinstance(result, dict) or "legacy" not in result:
        return None
    return result


def _extract_screen_name(tweet_obj: dict[str, Any]) -> str:
    """Extract author screen_name across both legacy and newer X GraphQL schemas."""
    user_res = (
        tweet_obj.get("core", {})
        .get("user_results", {})
        .get("result", {})
    )
    if not isinstance(user_res, dict):
        return ""
    return (
        user_res.get("core", {}).get("screen_name")
        or user_res.get("legacy", {}).get("screen_name")
        or ""
    )


def _extract_full_text(tweet_obj: dict[str, Any]) -> str:
    """Extract full text, preferring un-truncated note_tweet for long posts."""
    note_text = (
        tweet_obj.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
        .get("text")
    )
    if note_text:
        return html.unescape(note_text)
    legacy = tweet_obj.get("legacy", {})
    raw_text = legacy.get("full_text") or legacy.get("text") or ""
    return html.unescape(raw_text)


def _iter_tweet_result_nodes(node: Any) -> list[dict[str, Any]]:
    """Recursively collect all non-promoted `tweet_results.result` dicts in a GraphQL payload."""
    found: list[dict[str, Any]] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            # Skip promoted / ad entries
            entry_id = str(obj.get("entryId", ""))
            if entry_id.startswith("promoted-") or "promotedMetadata" in obj:
                return

            if "tweet_results" in obj and isinstance(obj["tweet_results"], dict):
                res = obj["tweet_results"].get("result")
                if isinstance(res, dict):
                    found.append(res)

            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(node)
    return found


def _extract_bottom_cursor(node: Any) -> str | None:
    """Extract the pagination Bottom cursor value from a GraphQL timeline payload."""
    if isinstance(node, dict):
        if node.get("cursorType") == "Bottom" and isinstance(node.get("value"), str):
            return node["value"]
        for v in node.values():
            res = _extract_bottom_cursor(v)
            if res:
                return res
    elif isinstance(node, list):
        for item in node:
            res = _extract_bottom_cursor(item)
            if res:
                return res
    return None


def _extract_user_profile_info(user_res: dict[str, Any]) -> dict[str, Any] | None:
    """Extract total post counts and profile metadata from a GraphQL User object."""
    if not isinstance(user_res, dict) or user_res.get("__typename") != "User":
        return None
    core = user_res.get("core", {})
    legacy = user_res.get("legacy", {})
    tweet_counts = user_res.get("tweet_counts", {})
    rel_counts = user_res.get("relationship_counts", {})
    bio_obj = user_res.get("profile_bio", {})
    loc_obj = user_res.get("location", {})

    screen_name = core.get("screen_name") or legacy.get("screen_name") or ""
    name = core.get("name") or legacy.get("name") or ""
    created_at = _parse_twitter_date(core.get("created_at") or legacy.get("created_at") or "")
    description = bio_obj.get("description") or legacy.get("description") or ""
    location = loc_obj.get("location") or legacy.get("location") or ""

    total_tweets = (
        tweet_counts.get("tweets")
        if "tweets" in tweet_counts
        else legacy.get("statuses_count")
    )
    media_tweets = (
        tweet_counts.get("media_tweets")
        if "media_tweets" in tweet_counts
        else legacy.get("media_count")
    )
    followers = (
        rel_counts.get("followers")
        if "followers" in rel_counts
        else legacy.get("followers_count")
    )
    following = (
        rel_counts.get("following")
        if "following" in rel_counts
        else legacy.get("friends_count")
    )

    if total_tweets is None and not screen_name:
        return None

    return {
        "id": user_res.get("rest_id") or "",
        "screen_name": screen_name,
        "name": name,
        "description": description,
        "location": location,
        "created_at": created_at,
        "total_tweets": total_tweets,
        "media_tweets": media_tweets,
        "followers": followers,
        "following": following,
    }


class TimelineCollector:
    """Intercepts X GraphQL timeline responses, resolves reply/quote contexts, and manages resume cursors."""

    def __init__(self, target_user: str, sink: dict[str, dict[str, Any]]) -> None:
        self.target_user = target_user.lower()
        self.sink = sink
        # Pool of all tweets seen in GraphQL responses (id -> basic tweet info) for reply context lookup
        self.tweet_pool: dict[str, dict[str, Any]] = {}
        # Set of all timeline tweet IDs processed in the current crawl session
        self.seen_timeline_ids: set[str] = set()
        self.tweet_filter: Callable[[dict[str, Any]], bool] = lambda _: True
        self.response_event = asyncio.Event()
        self.rate_limited = False
        self.user_profile: dict[str, Any] | None = None
        if PROFILE_FILE.exists():
            try:
                with open(PROFILE_FILE, encoding="utf-8") as f:
                    loaded_prof = json.load(f)
                if isinstance(loaded_prof, dict) and loaded_prof.get("screen_name", "").lower() == self.target_user:
                    self.user_profile = loaded_prof
            except Exception:
                pass

        # Checkpoint / cursor management
        self.current_label: str = ""
        self.expected_endpoint_re: re.Pattern[str] | None = None
        self.skip_first_cursor_save: bool = False
        self.cursors: dict[str, str] = {}
        self.completed_tabs: set[str] = set()
        self.empty_timeline_responses: int = 0
        self.consecutive_older_than_since: int = 0

        # Pre-populate tweet_pool with any existing loaded tweets
        for t in sink.values():
            if t.get("id"):
                self.tweet_pool[t["id"]] = {
                    "id": t["id"],
                    "author": t.get("author", target_user),
                    "created_at": t.get("created_at", ""),
                    "text": t.get("text", ""),
                }

    def _register_in_pool(self, tweet_obj: dict[str, Any]) -> dict[str, Any] | None:
        """Parse basic fields of a tweet object and store it in tweet_pool."""
        unwrapped = _unwrap_tweet(tweet_obj)
        if not unwrapped:
            return None

        # Fallback: extract target user profile info from tweet's user_results if not yet populated
        if not self.user_profile:
            user_res = unwrapped.get("core", {}).get("user_results", {}).get("result", {})
            info = _extract_user_profile_info(user_res)
            if info and info.get("screen_name", "").lower() == self.target_user:
                self.user_profile = info
                save_profile_info(info)

        legacy = unwrapped.get("legacy", {})
        tweet_id = str(unwrapped.get("rest_id") or legacy.get("id_str") or "")
        if not tweet_id:
            return None

        author = _extract_screen_name(unwrapped)
        raw_date = unwrapped.get("core", {}).get("created_at") or legacy.get("created_at", "")
        created_at = _parse_twitter_date(raw_date)
        text = _extract_full_text(unwrapped)

        basic = {
            "id": tweet_id,
            "author": author,
            "created_at": created_at,
            "text": text,
        }
        self.tweet_pool[tweet_id] = basic

        # Also register nested quoted or retweeted tweets into the pool
        quoted_raw = unwrapped.get("quoted_status_result", {}).get("result")
        if quoted_raw:
            self._register_in_pool(quoted_raw)
        rt_raw = legacy.get("retweeted_status_result", {}).get("result")
        if rt_raw:
            self._register_in_pool(rt_raw)

        return basic

    def _parse_single_tweet(self, raw_result: dict[str, Any]) -> dict[str, Any] | None:
        """Parse a full tweet node including retweet, quote, and reply metadata."""
        tweet_obj = _unwrap_tweet(raw_result)
        if not tweet_obj:
            return None

        base = self._register_in_pool(tweet_obj)
        if not base:
            return None

        legacy = tweet_obj.get("legacy", {})
        tweet_id = base["id"]
        author = base["author"]
        created_at = base["created_at"]
        text = base["text"]

        # 1. Quoted tweet context (if any)
        quoted_tweet: dict[str, Any] | None = None
        quoted_raw = tweet_obj.get("quoted_status_result", {}).get("result")
        if quoted_raw:
            q_base = self._register_in_pool(quoted_raw)
            if q_base:
                quoted_tweet = {
                    "id": q_base["id"],
                    "author": q_base["author"],
                    "created_at": q_base["created_at"],
                    "text": q_base["text"],
                }

        # 2. Retweet detection & full original text extraction
        rt_raw = legacy.get("retweeted_status_result", {}).get("result")
        if rt_raw:
            rt_unwrapped = _unwrap_tweet(rt_raw)
            rt_base = self._register_in_pool(rt_raw)
            type_ = "retweet"
            original_author = rt_base["author"] if rt_base else ""
            if rt_base and rt_base["text"]:
                text = rt_base["text"]
            # If the retweeted status itself quoted another tweet, preserve that quote context
            if not quoted_tweet and rt_unwrapped:
                rt_quoted_raw = rt_unwrapped.get("quoted_status_result", {}).get("result")
                if rt_quoted_raw:
                    rq_base = self._register_in_pool(rt_quoted_raw)
                    if rq_base:
                        quoted_tweet = {
                            "id": rq_base["id"],
                            "author": rq_base["author"],
                            "created_at": rq_base["created_at"],
                            "text": rq_base["text"],
                        }
            reply_to = None
        else:
            original_author = ""
            # 3. Reply detection & parent tweet lookup
            reply_to_id = str(legacy.get("in_reply_to_status_id_str") or "")
            reply_to_author = legacy.get("in_reply_to_screen_name") or ""
            if reply_to_id or legacy.get("in_reply_to_user_id_str"):
                type_ = "reply"
                parent = self.tweet_pool.get(reply_to_id)
                reply_to = {
                    "id": reply_to_id or None,
                    "author": (parent["author"] if parent and parent.get("author") else reply_to_author) or None,
                    "created_at": parent["created_at"] if parent else None,
                    "text": parent["text"] if parent else None,
                }
            else:
                type_ = "tweet"
                reply_to = None

        return {
            "id": tweet_id,
            "created_at": created_at,
            "author": author,
            "type": type_,
            "original_author": original_author,
            "text": text,
            "reply_to": reply_to,
            "quoted_tweet": quoted_tweet,
        }

    def resolve_missing_reply_contexts(self) -> None:
        """Backfill reply_to text/created_at if parent tweets were parsed after the reply."""
        for tweet in self.sink.values():
            reply_to = tweet.get("reply_to")
            if isinstance(reply_to, dict) and reply_to.get("id") and not reply_to.get("text"):
                parent = self.tweet_pool.get(reply_to["id"])
                if parent:
                    reply_to["author"] = parent["author"] or reply_to.get("author")
                    reply_to["created_at"] = parent["created_at"]
                    reply_to["text"] = parent["text"]

    async def handle_response(self, response: Response) -> None:
        """Playwright passive response listener for profile metadata and timeline GraphQL responses."""
        # 1. User profile metadata (UserByScreenName)
        if _GRAPHQL_USER_PROFILE_RE.search(response.url) and response.status == 200:
            try:
                payload = await response.json()
                user_res = payload.get("data", {}).get("user", {}).get("result", {})
                info = _extract_user_profile_info(user_res)
                if info and info.get("screen_name", "").lower() == self.target_user:
                    self.user_profile = info
                    save_profile_info(info)
            except Exception:
                pass
            return

        # 2. Timeline tweets (UserOriginalsTimeline / UserRepostsTimeline / UserRepliesTimeline / etc.)
        if not _GRAPHQL_TIMELINE_RE.search(response.url):
            return
        if self.expected_endpoint_re and not self.expected_endpoint_re.search(response.url):
            return

        if response.status == 429:
            self.rate_limited = True
            self.response_event.set()
            return

        if response.status != 200:
            return

        try:
            payload = await response.json()
        except Exception:
            return

        raw_nodes = _iter_tweet_result_nodes(payload)
        bottom_cursor = _extract_bottom_cursor(payload)
        if not raw_nodes:
            if isinstance(payload.get("errors"), list) and "data" not in payload:
                err_str = " ".join(str(e.get("message", "")) for e in payload["errors"] if isinstance(e, dict))
                if "rate limit" in err_str.lower():
                    self.rate_limited = True
            elif bottom_cursor is not None or "data" in payload:
                self.empty_timeline_responses += 1
            self.skip_first_cursor_save = False
            self.response_event.set()
            return

        self.empty_timeline_responses = 0
        if bottom_cursor and self.current_label:
            if self.skip_first_cursor_save:
                # Don't overwrite the saved checkpoint cursor with page 1's cursor before jumping
                self.skip_first_cursor_save = False
            else:
                self.cursors[self.current_label] = bottom_cursor
                save_checkpoint_state(self.cursors, self.completed_tabs)

        # Pass 1: Register all tweets in this payload into tweet_pool so parent tweets are available
        for node in raw_nodes:
            self._register_in_pool(node)

        # Pass 2: Parse full tweet records and keep only those authored by TARGET_USER
        for node in raw_nodes:
            parsed = self._parse_single_tweet(node)
            if not parsed or not parsed["id"]:
                continue

            self.seen_timeline_ids.add(parsed["id"])

            # Strictly ensure the tweet was posted/retweeted/replied by the target user
            if parsed["author"].lower() != self.target_user:
                continue

            created_at = parsed.get("created_at") or ""
            if SINCE_BOUND and created_at and created_at < SINCE_BOUND:
                self.consecutive_older_than_since += 1
                continue
            if created_at:
                self.consecutive_older_than_since = 0

            if UNTIL_BOUND and created_at and created_at > UNTIL_BOUND:
                continue

            if self.tweet_filter(parsed) and parsed["id"] not in self.sink:
                self.sink[parsed["id"]] = parsed

        self.resolve_missing_reply_contexts()
        self.response_event.set()


# ---------------------------------------------------------------------------
# Browser & Cookie Management
# ---------------------------------------------------------------------------

def _load_playwright_cookies() -> list[dict[str, Any]]:
    with open(COOKIE_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    cookies: list[dict[str, Any]] = []
    for c in raw:
        if not c.get("name") or c.get("value") is None:
            continue

        raw_same_site = str(c.get("sameSite", "Lax")).lower()
        if raw_same_site == "no_restriction" or raw_same_site == "none":
            same_site = "None"
        elif raw_same_site == "strict":
            same_site = "Strict"
        else:
            same_site = "Lax"

        secure = bool(c.get("secure", False)) or (same_site == "None")

        cookie: dict[str, Any] = {
            "name": c["name"],
            "value": str(c["value"]),
            "domain": c.get("domain", ".x.com"),
            "path": c.get("path", "/"),
            "secure": secure,
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": same_site,
        }
        expiry = c.get("expirationDate") or c.get("expires")
        if expiry is not None:
            try:
                exp_val = float(expiry)
                if exp_val > 0:
                    cookie["expires"] = exp_val
            except (TypeError, ValueError):
                pass

        cookies.append(cookie)
    return cookies


def _is_headless_mode() -> bool:
    if HEADLESS_ENV in ("true", "1", "yes"):
        return True
    if HEADLESS_ENV in ("false", "0", "no"):
        return False
    return bool(sys.platform.startswith("linux") and not os.environ.get("DISPLAY"))


async def create_browser_session(pw) -> tuple[BrowserContext, Page]:
    """
    Create a local Chrome persistent session.
    Returns (context, page).
    """
    headless = _is_headless_mode()
    if headless:
        print("[*] 使用 Headless（无头）模式启动本地浏览器")

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": CHROME_USER_DATA_DIR,
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if PROXY:
        launch_kwargs["proxy"] = {"server": PROXY}

    if CHROME_PATH and Path(CHROME_PATH).exists():
        launch_kwargs["executable_path"] = CHROME_PATH
        print(f"[*] 已启动本地 Google Chrome：{CHROME_PATH}")
    else:
        launch_kwargs["channel"] = "chrome"
        print("[*] 已启动本地 Google Chrome")

    try:
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)
    except Exception:
        launch_kwargs.pop("executable_path", None)
        launch_kwargs.pop("channel", None)
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)
        print("[*] 已回退启动 Playwright Chromium")

    await context.add_init_script(STEALTH_INIT_SCRIPT)
    await context.add_init_script(_XHR_CURSOR_HOOK_SCRIPT)

    # Reuse the first tab and close any leftover tabs from previous sessions
    if context.pages:
        page = context.pages[0]
        for extra_p in context.pages[1:]:
            try:
                await extra_p.close()
            except Exception:
                pass
    else:
        page = await context.new_page()

    return context, page


async def has_auth_cookie(context: BrowserContext) -> bool:
    """Check whether X's core `auth_token` session cookie exists and is non-empty."""
    try:
        cookies = await context.cookies(["https://x.com", "https://twitter.com"])
        return any(c.get("name") == "auth_token" and bool(c.get("value")) for c in cookies)
    except Exception:
        return False


async def is_logged_in(page: Page) -> bool:
    """Verify login state using both URL path check and `auth_token` cookie presence."""
    parsed_path = urllib.parse.urlparse(page.url).path.rstrip("/")
    if parsed_path in ("/login", "/i/flow/login") or parsed_path.startswith("/i/flow/"):
        return False
    return await has_auth_cookie(page.context)


# ---------------------------------------------------------------------------
# Scroll & Crawl Loop
# ---------------------------------------------------------------------------

def _print_user_profile_banner(profile: dict[str, Any] | None, existing_count: int) -> None:
    if not profile:
        return
    screen_name = profile.get("screen_name") or TARGET_USER
    name = profile.get("name") or ""
    total_tweets = profile.get("total_tweets")
    media_tweets = profile.get("media_tweets")
    followers = profile.get("followers")
    following = profile.get("following")

    name_part = f" ({name})" if name else ""
    print(f"  ┌──────────────────────────────────────────────────────────")
    print(f"  │ 目标账号：@{screen_name}{name_part}")
    if total_tweets is not None:
        media_part = f"（含媒体帖 {media_tweets:,} 条）" if media_tweets is not None else ""
        print(f"  │ 帖子总数：{total_tweets:,} 条{media_part}")
    if followers is not None or following is not None:
        print(f"  │ 社交数据：关注 {following or 0:,}  |  粉丝 {followers or 0:,}")
    if SINCE_BOUND or UNTIL_BOUND:
        since_str = SINCE_DATE_RAW or "最早"
        until_str = UNTIL_DATE_RAW or "最新"
        print(f"  │ 时间范围：{since_str}  →  {until_str}")
    print(f"  │ 本地已存：{existing_count:,} 条（{OUTPUT_FILE}）")
    print(f"  └──────────────────────────────────────────────────────────")


async def _recover_from_rate_limit(
    page: Page,
    collector: TimelineCollector,
    label: str,
) -> None:
    """Wait 30s on HTTP 429 rate limit, then click Retry or reload and wait for GraphQL response."""
    print(f"  [!] [{label}] 触发 X 接口频率限制 (429)，暂停 30 秒后重试...")
    collector.rate_limited = False
    collector.response_event.clear()
    await asyncio.sleep(30)

    retry_btn = page.locator("button:has-text('Retry'), button:has-text('重试')")
    clicked = False
    if await retry_btn.count() > 0:
        try:
            await human_move_and_click(page, retry_btn.first, "限流恢复重试按钮")
            await human_reset_gaze(page, "重试后视线复位")
            clicked = True
        except Exception:
            pass

    if not clicked and not collector.seen_timeline_ids:
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

    try:
        await asyncio.wait_for(collector.response_event.wait(), timeout=15.0)
        await asyncio.sleep(1.0)
    except asyncio.TimeoutError:
        pass


_POSTS_TIMELINE_RE = re.compile(
    r"/graphql/[^/]+/(?:User(?:Originals|Tweets|Posts)|Profile)\w*Timeline\w*|/graphql/[^/]+/UserTweets(?:\?|$)"
)
_REPOSTS_TIMELINE_RE = re.compile(r"/graphql/[^/]+/UserRepostsTimeline\w*")
_REPLIES_TIMELINE_RE = re.compile(r"/graphql/[^/]+/User(?:RepliesTimeline\w*|TweetsAndReplies)")


async def crawl_page(
    page: Page,
    collector: TimelineCollector,
    url: str,
    label: str,
    tweet_filter: Callable[[dict[str, Any]], bool],
    resume_cursor: str | None = None,
    show_profile_banner: bool = True,
    tab_href: str | None = None,
    endpoint_re: re.Pattern[str] | None = None,
    endpoint_substr: str = "",
) -> None:
    """Navigate to url (or click profile tab), intercept GraphQL responses while scrolling, and auto-save."""
    collector.current_label = label
    collector.expected_endpoint_re = endpoint_re
    collector.tweet_filter = tweet_filter
    collector.seen_timeline_ids.clear()
    collector.response_event.clear()
    collector.rate_limited = False
    collector.empty_timeline_responses = 0
    collector.consecutive_older_than_since = 0
    collector.skip_first_cursor_save = bool(resume_cursor)

    # Set or clear the in-page XHR resume cursor & endpoint filter before navigating or clicking tab
    init_js = (
        f"window.__X_RESUME_CURSOR__ = {json.dumps(resume_cursor)};"
        f"window.__X_RESUME_ENDPOINT_SUBSTR__ = {json.dumps(endpoint_substr)};"
    )
    await page.add_init_script(init_js)
    try:
        await page.evaluate(init_js)
    except Exception:
        pass

    last_saved = len(collector.sink)
    initial_sink_count = len(collector.sink)

    # Check if we are already on the target user's profile and can switch tabs via biomechanical click
    switched_via_tab = False
    current_path = urllib.parse.urlparse(page.url).path.rstrip("/").lower()
    if tab_href and current_path.startswith(f"/{TARGET_USER.lower()}"):
        tab_locator = page.locator(f'[role="tablist"] [role="tab"][href="{tab_href}" i]')
        if await tab_locator.count() > 0:
            is_selected = await tab_locator.first.get_attribute("aria-selected")
            if is_selected != "true":
                print(f"\n[*] 切换至子页面：{url}（【{label}】）")
                try:
                    await page.evaluate("window.scrollTo(0, 0)")
                    await asyncio.sleep(0.5)
                    collector.seen_timeline_ids.clear()
                    collector.response_event.clear()
                    collector.empty_timeline_responses = 0
                    await page.evaluate(init_js)
                    await human_move_and_click(page, tab_locator.first, f"顶部【{label}】标签页")
                    switched_via_tab = True
                except Exception:
                    switched_via_tab = False

    if not switched_via_tab:
        print(f"\n[*] 前往 {url}（【{label}】）")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

    # Wait for initial GraphQL timeline response
    try:
        await asyncio.wait_for(collector.response_event.wait(), timeout=12.0)
    except asyncio.TimeoutError:
        pass

    # Fallback: if tab click did not trigger response, navigate directly to URL
    if switched_via_tab and not collector.seen_timeline_ids and collector.empty_timeline_responses == 0:
        try:
            collector.response_event.clear()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.wait_for(collector.response_event.wait(), timeout=12.0)
        except Exception:
            pass

    # If direct URL navigation landed on the default tab instead of the requested tab, click the tab
    if tab_href and not collector.seen_timeline_ids and collector.empty_timeline_responses == 0:
        tab_locator = page.locator(f'[role="tablist"] [role="tab"][href="{tab_href}" i]')
        if await tab_locator.count() > 0:
            is_selected = await tab_locator.first.get_attribute("aria-selected")
            if is_selected != "true":
                try:
                    await page.evaluate("window.scrollTo(0, 0)")
                    await asyncio.sleep(0.4)
                    collector.response_event.clear()
                    await page.evaluate(init_js)
                    await human_move_and_click(page, tab_locator.first, f"顶部【{label}】标签页")
                    await asyncio.wait_for(collector.response_event.wait(), timeout=12.0)
                except Exception:
                    pass

    if collector.rate_limited:
        await _recover_from_rate_limit(page, collector, label)

    await human_reset_gaze(page, f"进入【{label}】页面视线复位")
    await human_wait(page, 900, 1800, f"模拟真人在【{label}】首屏停留浏览")

    if not await is_logged_in(page):
        print("[!] 检测到未登录状态（缺少有效登录凭证或跳转到登录流），请检查 Cookie 或浏览器会话。")
        return

    if show_profile_banner and collector.user_profile:
        _print_user_profile_banner(collector.user_profile, initial_sink_count)

    if not collector.seen_timeline_ids:
        if collector.empty_timeline_responses >= 1:
            print(f"  [{label}] 该栏目下暂无帖子（0 条），已标记完成。")
            collector.completed_tabs.add(label)
            collector.cursors.pop(label, None)
            save_checkpoint_state(collector.cursors, collector.completed_tabs)
            return
        # Give the page slightly more time if network is slow
        try:
            await page.wait_for_selector("article[data-testid='tweet']", timeout=8000)
            await asyncio.sleep(1.5)
        except Exception:
            print(f"[!] {label} 页面未捕获到任何推文数据，跳过。")
            return

    # Simulate initial reading gesture on the first visible tweet card
    first_tweet = page.locator("article[data-testid='tweet']").first
    if await first_tweet.count() > 0:
        await human_idle_reading_gesture(page, first_tweet, text_len=160, elapsed_sec=1.5)

    prev_sink_count = len(collector.sink)
    prev_seen_count = len(collector.seen_timeline_ids)
    initial_added = prev_sink_count - initial_sink_count
    no_new = 0
    step = 0
    fast_scroll = False

    print(
        f"  [{label}] 初始加载完成：本次新增 {initial_added} 条，本地累计 {prev_sink_count} 条"
        f"（本页扫描节点 {prev_seen_count} 个）"
    )
    if resume_cursor:
        print(f"  [断点续传] 已注入上次中断的翻页游标，滚动到底部后将直接从断点位置继续向后抓取")

    if len(collector.sink) - last_saved >= SAVE_INTERVAL:
        _save_intermediate(collector.sink)
        last_saved = len(collector.sink)

    if SINCE_BOUND and collector.consecutive_older_than_since >= 5:
        print(f"  [{label}] 首屏已到达起始日期 ({SINCE_DATE_RAW}) 之前，当前子页面抓取结束")
        collector.completed_tabs.add(label)
        collector.cursors.pop(label, None)
        save_checkpoint_state(collector.cursors, collector.completed_tabs)
        return

    while no_new < MAX_NO_NEW:
        step += 1
        collector.response_event.clear()

        # Biomechanical wheel scrolling instead of mechanical window.scrollBy
        await human_timeline_scroll(page, target_distance=SCROLL_PIXELS, fast_mode=fast_scroll)

        try:
            await asyncio.wait_for(collector.response_event.wait(), timeout=SCROLL_PAUSE)
            # Human-like variable reading pause between scroll batches
            pause_ms = int(450 + (step % 3) * 150) if fast_scroll else int(1000 + (step % 3) * 280)
            await page.wait_for_timeout(pause_ms)
        except asyncio.TimeoutError:
            pass

        # Immediately handle HTTP 429 rate limit before counting no_new or clicking Retry
        was_rate_limited = collector.rate_limited
        if was_rate_limited:
            await _recover_from_rate_limit(page, collector, label)

        new_qualifying = len(collector.sink) - prev_sink_count
        new_seen = len(collector.seen_timeline_ids) - prev_seen_count
        prev_sink_count = len(collector.sink)
        prev_seen_count = len(collector.seen_timeline_ids)

        if new_qualifying > 0 or new_seen > 0:
            no_new = 0
            if new_qualifying > 0:
                fast_scroll = False
                print(
                    f"  [{label}] 滚动 {step}，新增 {new_qualifying} 条，累计 {len(collector.sink)} 条"
                )
            else:
                fast_scroll = True
                print(
                    f"  [{label}] 滚动 {step}，本页无新目标推文（已跳过重复/范围外/他人节点 {new_seen} 个），累计 {len(collector.sink)} 条"
                )
            if len(collector.sink) - last_saved >= SAVE_INTERVAL:
                _save_intermediate(collector.sink)
                last_saved = len(collector.sink)
            if SINCE_BOUND and collector.consecutive_older_than_since >= 5:
                print(f"  [{label}] 已滚动至起始日期 ({SINCE_DATE_RAW}) 之前，当前子页面抓取结束")
                break
        elif not was_rate_limited:
            if resume_cursor and step <= 2 and collector.empty_timeline_responses >= 1:
                print(f"  [断点续传] 上次保存的游标已到达【{label}】时间线最底部，本栏无需继续翻页")
                break
            if collector.empty_timeline_responses >= 2:
                print(f"  [{label}] 已到达时间线最底部")
                break

            no_new += 1
            # Click Retry button with Bezier curve trajectory if X timeline encountered a transient error
            retry_btn = page.locator("button:has-text('Retry'), button:has-text('重试')")
            if await retry_btn.count() > 0:
                try:
                    await human_move_and_click(page, retry_btn.first, "时间线异常重试按钮")
                    await human_reset_gaze(page, "重试后视线复位")
                    await asyncio.sleep(2.0)
                except Exception:
                    pass

    # Mark tab as completed and clear its exhausted cursor so future resume runs don't inject an EOF cursor
    collector.completed_tabs.add(label)
    collector.cursors.pop(label, None)
    save_checkpoint_state(collector.cursors, collector.completed_tabs)

    collector.resolve_missing_reply_contexts()
    print(f"  [{label}] 完成，本地累计 {len(collector.sink)} 条推文")


# ---------------------------------------------------------------------------
# Checkpoint, Persistence & Interactive Selection
# ---------------------------------------------------------------------------

def load_existing_data() -> dict[str, dict[str, Any]]:
    """
    Load previously saved tweets from `data/<TARGET_USER>/tweets.json`,
    automatically migrating any legacy `data/<TARGET_USER>*.json` files.
    """
    merged: dict[str, dict[str, Any]] = {}

    # 1. Load from `data/<TARGET_USER>/tweets.json` if exists
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                items = json.load(f)
            if isinstance(items, list):
                for t in items:
                    if isinstance(t, dict) and t.get("id"):
                        merged[str(t["id"])] = t
        except Exception as e:
            print(f"[!] 读取已有数据文件 {OUTPUT_FILE} 失败：{e}")

    # 2. Migrate any legacy files in `data/` root (`data/<TARGET_USER>.json` or `data/<TARGET_USER>_YYYYMMDD_HHMMSS.json`)
    if DATA_DIR.exists():
        legacy_re = re.compile(rf"^{re.escape(TARGET_USER)}(?:_\d{{8}}_\d{{6}})?\.json$")
        legacy_files = sorted(
            p for p in DATA_DIR.iterdir() if p.is_file() and legacy_re.fullmatch(p.name)
        )
        migrated_any = False
        for lf in legacy_files:
            try:
                with open(lf, encoding="utf-8") as f:
                    items = json.load(f)
                if isinstance(items, list):
                    for t in items:
                        if isinstance(t, dict) and t.get("id"):
                            merged[str(t["id"])] = t
                lf.unlink()
                migrated_any = True
            except Exception:
                pass
        if migrated_any and merged:
            _write(merged)
            print(f"[*] 已将旧路径数据文件自动迁移至 {OUTPUT_FILE}（共 {len(merged)} 条）")

    return merged


def load_checkpoint_state() -> tuple[dict[str, str], set[str]]:
    """Load saved pagination cursors and completed tabs from `data/<TARGET_USER>/state.json`."""
    # Migrate legacy `data/.<TARGET_USER>_state.json` if present
    legacy_state = DATA_DIR / f".{TARGET_USER}_state.json"
    if legacy_state.exists() and not STATE_FILE.exists():
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            legacy_state.rename(STATE_FILE)
        except Exception:
            pass

    if not STATE_FILE.exists():
        return {}, set()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        saved_since = data.get("since_date")
        saved_until = data.get("until_date")
        if (saved_since or SINCE_BOUND or saved_until or UNTIL_BOUND) and (
            saved_since != SINCE_BOUND or saved_until != UNTIL_BOUND
        ):
            print("[*] 检测到 .env 时间范围配置变更，已重置子页面翻页断点状态。")
            return {}, set()
        raw_cursors = data.get("cursors", {})
        cursors = dict(raw_cursors) if isinstance(raw_cursors, dict) else {}
        raw_completed = data.get("completed_tabs", [])
        completed = set(raw_completed) if isinstance(raw_completed, list) else set()
        return cursors, completed
    except Exception:
        return {}, set()


def _atomic_write_json(target_path: Path, data: Any) -> None:
    """Write JSON atomically via a temporary file to prevent corruption on Ctrl+C interrupt."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(target_path)


def save_checkpoint_state(cursors: dict[str, str], completed_tabs: set[str] | None = None) -> None:
    """Persist pagination cursors and completed tabs to `data/<TARGET_USER>/state.json`."""
    payload = {
        "target_user": TARGET_USER,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since_date": SINCE_BOUND,
        "until_date": UNTIL_BOUND,
        "cursors": cursors,
        "completed_tabs": sorted(completed_tabs) if completed_tabs else [],
    }
    try:
        _atomic_write_json(STATE_FILE, payload)
    except Exception:
        pass


def save_profile_info(profile: dict[str, Any]) -> None:
    """Persist target user profile info to `data/<TARGET_USER>/profile.json`."""
    payload = {
        **profile,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        _atomic_write_json(PROFILE_FILE, payload)
    except Exception:
        pass


def select_types() -> list[str]:
    options = {
        "1": "posts",
        "2": "reposts",
        "3": "replies",
    }
    print("\n请选择要抓取的内容类型（输入编号，多选用空格分隔，直接回车 = 全部）：")
    print("  1. 原创推文 Posts（含纯原创推文与带评论引用推文）")
    print("  2. 纯转推 Reposts（直接一键转发他人的推文）")
    print("  3. 回复 Replies（含被回复原帖与引用推文上下文）")
    print("  4. 全部（依次进入 Posts → Reposts → Replies 三个子页面抓取）")

    raw = input("\n选项：").strip()

    if not raw or raw == "4":
        return ["posts", "reposts", "replies"]

    selected = []
    for ch in raw.split():
        if ch in options and options[ch] not in selected:
            selected.append(options[ch])
            print(f"  ✓ 已选：{options[ch]}")
        else:
            print(f"  [!] 无效选项 {ch!r}，已忽略")

    return selected if selected else ["posts", "reposts", "replies"]


def select_resume_mode(
    existing_count: int,
    has_cursor: bool,
    completed_tabs: set[str],
    active_labels: list[str],
) -> str:
    """
    Ask user how to proceed when local data or checkpoint cursor exists.
    Returns 'resume' | 'from_top' | 'restart'.
    """
    has_partial_completion = bool(completed_tabs) and any(lbl not in completed_tabs for lbl in active_labels)
    can_resume = has_cursor or has_partial_completion

    if existing_count == 0 and not can_resume:
        return "from_top"

    if has_cursor:
        status_hint = "，且存在上次中断的翻页游标"
    elif has_partial_completion:
        done_str = "、".join(sorted(completed_tabs))
        status_hint = f"，已完成【{done_str}】抓取"
    else:
        status_hint = ""

    print(f"\n[*] 检测到 @{TARGET_USER} 本地已有 {existing_count} 条推文记录（{OUTPUT_FILE}{status_hint}）：")
    if can_resume:
        print("  1. 断点续传（直接从上次中断位置继续往后抓取，已完成的栏目自动跳过，默认）")
        print("  2. 从头扫描（从最新推文开始向下滚动，自动合并已有数据）")
        print("  3. 清空重抓（删除本地已有记录与断点，完全重新开始）")
        raw = input("\n续传选项（直接回车 = 1）：").strip()
        if raw == "2":
            return "from_top"
        if raw == "3":
            return "restart"
        return "resume"
    else:
        print("  1. 从头扫描并合并（保留已有数据，从最新推文开始向下滚动，默认）")
        print("  2. 清空重抓（删除本地已有记录，完全重新开始）")
        raw = input("\n选项（直接回车 = 1）：").strip()
        if raw == "2":
            return "restart"
        return "from_top"


def _write(tweets_dict: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result = sorted(tweets_dict.values(), key=lambda t: t.get("created_at", ""), reverse=True)
    _atomic_write_json(OUTPUT_FILE, result)
    return result


def _save_intermediate(tweets_dict: dict[str, dict[str, Any]]) -> None:
    result = _write(tweets_dict)
    print(f"  [自动保存] 累计 {len(result)} 条 → {OUTPUT_FILE}")


def save(tweets_dict: dict[str, dict[str, Any]]) -> None:
    if not tweets_dict:
        print("\n[!] 本次未采集到任何符合条件的推文，跳过生成文件。")
        return
    result = _write(tweets_dict)
    print(f"\n[*] 已保存共 {len(result)} 条推文至 {OUTPUT_FILE}")


# ---------------------------------------------------------------------------
# Main Entrypoint
# ---------------------------------------------------------------------------

async def async_main() -> None:
    selected = select_types()

    all_tweets = load_existing_data()
    saved_cursors, completed_tabs = load_checkpoint_state()

    # Migrate legacy checkpoint keys to the 3-tab naming scheme
    legacy_key_map = {
        "全部": "回复(Replies)",
        "回复": "回复(Replies)",
        "推文+转推": "原创推文(Posts)",
    }
    for old_key, new_key in legacy_key_map.items():
        if old_key in saved_cursors:
            saved_cursors.setdefault(new_key, saved_cursors.pop(old_key))
        if old_key in completed_tabs:
            completed_tabs.discard(old_key)
            completed_tabs.add(new_key)

    tab_label_map = {
        "posts": "原创推文(Posts)",
        "reposts": "纯转推(Reposts)",
        "replies": "回复(Replies)",
    }
    active_labels = [tab_label_map[k] for k in selected if k in tab_label_map]
    has_relevant_cursor = any(lbl in saved_cursors for lbl in active_labels)

    mode = select_resume_mode(len(all_tweets), has_relevant_cursor, completed_tabs, active_labels)
    if mode == "restart":
        all_tweets.clear()
        saved_cursors.clear()
        completed_tabs.clear()
        if OUTPUT_FILE.exists():
            OUTPUT_FILE.unlink()
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        print("[*] 已清空本地历史数据与断点记录。")
    elif mode == "from_top":
        for lbl in active_labels:
            saved_cursors.pop(lbl, None)
            completed_tabs.discard(lbl)

    print()

    async with async_playwright() as pw:
        context, page = await create_browser_session(pw)
        collector = TimelineCollector(TARGET_USER, all_tweets)
        collector.cursors = dict(saved_cursors)
        collector.completed_tabs = set(completed_tabs)

        # Passively listen to native browser responses (100% compatible with Cloudflare & X ServiceWorker)
        page.on("response", collector.handle_response)

        try:
            # Only inject browser_cookies.json if the persistent profile does not already have an active auth_token
            if not await has_auth_cookie(context) and Path(COOKIE_FILE).exists():
                cookies = _load_playwright_cookies()
                await context.add_cookies(cookies)
                print(f"[*] 已从 {COOKIE_FILE} 注入 {len(cookies)} 条 Cookie")

            if await has_auth_cookie(context):
                print("[*] 检测到本地有效登录凭证（auth_token）")
            else:
                if _is_headless_mode():
                    print(
                        "[!] 当前处于无头模式 (HEADLESS=true) 且未检测到有效登录态，"
                        "请先以有头模式运行完成登录或提供有效的 browser_cookies.json"
                    )
                    return
                await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=30000)
                print(
                    "[*] 当前未登录，请在打开的本地 Chrome 窗口中登录 X 账号"
                    f"（登录状态会自动保存在 {CHROME_USER_DATA_DIR}/ 中）..."
                )
                # Wait up to 3 minutes for user to complete login interactively
                for _ in range(90):
                    await asyncio.sleep(2.0)
                    if await is_logged_in(page):
                        break
                else:
                    print("[!] 登录等待超时，请登录后重试或提供有效的 browser_cookies.json")
                    return
                print("[*] 登录验证成功")

            crawl_all = set(selected) == {"posts", "reposts", "replies"}
            shown_banner = False

            if "posts" in selected:
                lbl = "原创推文(Posts)"
                if mode == "resume" and lbl in collector.completed_tabs and lbl not in saved_cursors:
                    print(f"\n[*] [{lbl}] 上次已完整抓取至时间线最底部，跳过重复扫描（如需重新扫描请选择“从头扫描”）")
                else:
                    await crawl_page(
                        page,
                        collector,
                        f"https://x.com/{TARGET_USER}",
                        lbl,
                        (lambda _: True) if crawl_all else (lambda t: t["type"] == "tweet"),
                        resume_cursor=saved_cursors.get(lbl) if mode == "resume" else None,
                        show_profile_banner=not shown_banner,
                        tab_href=f"/{TARGET_USER}",
                        endpoint_re=_POSTS_TIMELINE_RE,
                        endpoint_substr="Originals",
                    )
                    if collector.user_profile:
                        shown_banner = True

            if "reposts" in selected:
                lbl = "纯转推(Reposts)"
                if mode == "resume" and lbl in collector.completed_tabs and lbl not in saved_cursors:
                    print(f"\n[*] [{lbl}] 上次已完整抓取至时间线最底部，跳过重复扫描（如需重新扫描请选择“从头扫描”）")
                else:
                    await crawl_page(
                        page,
                        collector,
                        f"https://x.com/{TARGET_USER}/reposts",
                        lbl,
                        (lambda _: True) if crawl_all else (lambda t: t["type"] == "retweet"),
                        resume_cursor=saved_cursors.get(lbl) if mode == "resume" else None,
                        show_profile_banner=not shown_banner,
                        tab_href=f"/{TARGET_USER}/reposts",
                        endpoint_re=_REPOSTS_TIMELINE_RE,
                        endpoint_substr="Reposts",
                    )
                    if collector.user_profile:
                        shown_banner = True

            if "replies" in selected:
                lbl = "回复(Replies)"
                if mode == "resume" and lbl in collector.completed_tabs and lbl not in saved_cursors:
                    print(f"\n[*] [{lbl}] 上次已完整抓取至时间线最底部，跳过重复扫描（如需重新扫描请选择“从头扫描”）")
                else:
                    await crawl_page(
                        page,
                        collector,
                        f"https://x.com/{TARGET_USER}/with_replies",
                        lbl,
                        (lambda _: True) if crawl_all else (lambda t: t["type"] == "reply"),
                        resume_cursor=saved_cursors.get(lbl) if mode == "resume" else None,
                        show_profile_banner=not shown_banner,
                        tab_href=f"/{TARGET_USER}/with_replies",
                        endpoint_re=_REPLIES_TIMELINE_RE,
                        endpoint_substr="Replies",
                    )
                    if collector.user_profile:
                        shown_banner = True

            print("[+] 完成。")

        finally:
            # Always persist collected tweets & latest cursor state even if interrupted via Ctrl+C
            if all_tweets:
                collector.resolve_missing_reply_contexts()
                save(all_tweets)
            if collector.cursors or collector.completed_tabs:
                save_checkpoint_state(collector.cursors, collector.completed_tabs)

            try:
                await context.close()
            except Exception:
                pass


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n[!] 用户中断运行，已保存当前采集进度与断点。")


if __name__ == "__main__":
    main()
