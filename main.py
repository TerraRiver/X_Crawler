import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

# --- Config ---
TARGET_USER       = os.getenv("TARGET_USER", "bhupen07")
COOKIE_FILE       = "browser_cookies.json"
PROXY             = os.getenv("PROXY") or None
# Set CHROME_REMOTE_URL to connect to Chrome running on Windows:
#   e.g. CHROME_REMOTE_URL=172.20.240.1:9222
CHROME_REMOTE_URL = os.getenv("CHROME_REMOTE_URL") or None

_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_FILE = f"data/{TARGET_USER}_{_ts}.json"

SCROLL_PAUSE  = 3.0   # seconds between scrolls
MAX_NO_NEW    = 10    # stop after this many consecutive scrolls with no new tweets
SCROLL_PIXELS = 2500
SAVE_INTERVAL = 10    # auto-save every N new qualifying tweets


# ---------------------------------------------------------------------------
# Driver setup
# ---------------------------------------------------------------------------

def _remote_chrome_version(host_port: str) -> str | None:
    """Query /json/version to get the Chrome version string (e.g. '131.0.6778.205')."""
    try:
        url = f"http://{host_port}/json/version"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        m = re.search(r"Chrome/(\d+\.\d+\.\d+\.\d+)", data.get("Browser", ""))
        return m.group(1) if m else None
    except Exception:
        return None


def make_driver() -> webdriver.Chrome:
    opts = Options()

    if CHROME_REMOTE_URL:
        # Connect to an existing Chrome instance launched with --remote-debugging-port.
        # Use Selenium Manager (built-in) to auto-download the matching chromedriver.
        opts.debugger_address = CHROME_REMOTE_URL
        version = _remote_chrome_version(CHROME_REMOTE_URL)
        if version:
            print(f"[*] 远程 Chrome 版本：{version}")
        driver = webdriver.Chrome(options=opts)
        print(f"[*] 已连接到远程 Chrome（{CHROME_REMOTE_URL}）")
        return driver

    # Local Chrome
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if PROXY:
        opts.add_argument(f"--proxy-server={PROXY}")
    if not os.environ.get("DISPLAY"):
        opts.add_argument("--headless=new")
        print("[*] 未检测到 DISPLAY，使用 headless 模式")

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
    except Exception:
        driver = webdriver.Chrome(options=opts)

    # Mask automation fingerprint
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    return driver


# ---------------------------------------------------------------------------
# Cookie injection
# ---------------------------------------------------------------------------

def _load_raw_cookies() -> list[dict]:
    with open(COOKIE_FILE, encoding="utf-8") as f:
        return json.load(f)


def inject_cookies(driver: webdriver.Chrome) -> None:
    raw = _load_raw_cookies()

    # Must be on x.com domain before setting cookies
    if "x.com" not in driver.current_url:
        driver.get("https://x.com")
        time.sleep(2)

    driver.delete_all_cookies()

    for c in raw:
        same_site = c.get("sameSite", "Lax")
        if same_site.lower() in ("unspecified", "no_restriction"):
            same_site = "Strict" if c.get("secure") else "Lax"

        cookie: dict = {
            "name":     c["name"],
            "value":    c["value"],
            "domain":   c.get("domain", ".x.com"),
            "path":     c.get("path", "/"),
            "secure":   c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": same_site,
        }
        if "expirationDate" in c:
            cookie["expiry"] = int(c["expirationDate"])

        try:
            driver.add_cookie(cookie)
        except Exception:
            pass  # ignore cookies that don't apply to this domain

    print(f"[*] 已注入 {len(raw)} 条 Cookie")


# ---------------------------------------------------------------------------
# Tweet parsing from DOM articles
# ---------------------------------------------------------------------------

_STATUS_RE = re.compile(r"x\.com/([^/]+)/status/(\d+)")


def parse_article(article) -> dict | None:
    try:
        # --- Tweet ID and potential original author ---
        links = article.find_elements(By.CSS_SELECTOR, "a[href*='/status/']")
        tweet_id = None
        author_from_link = ""
        for link in links:
            href = link.get_attribute("href") or ""
            m = _STATUS_RE.search(href)
            if m:
                author_from_link = m.group(1)
                tweet_id = m.group(2)
                break
        if not tweet_id:
            return None

        # --- Timestamp ---
        time_els = article.find_elements(By.TAG_NAME, "time")
        created_at = time_els[0].get_attribute("datetime") if time_els else ""

        # --- Tweet text ---
        text_els = article.find_elements(
            By.CSS_SELECTOR, "[data-testid='tweetText']"
        )
        text = "\n".join(el.text for el in text_els) if text_els else ""

        # --- Retweet detection ---
        # X shows "[Name] reposted" above retweets in a socialContext element
        social_els = article.find_elements(
            By.CSS_SELECTOR, "[data-testid='socialContext']"
        )
        is_retweet = any(
            "repost" in el.text.lower() or "retweeted" in el.text.lower()
            for el in social_els
        )

        # Original author: the account whose tweet was retweeted
        original_author = author_from_link if is_retweet else ""

        # --- Reply detection ---
        # A retweet takes priority: even if the retweeted tweet is a reply,
        # we classify it as a retweet, not a reply.
        if is_retweet:
            is_reply = False
        else:
            article_text = article.text
            # X shows different text depending on interface language
            text_match = any(kw in article_text for kw in ("Replying to", "回复", "In reply to"))
            # DOM fallback: look for the "Replying to" indicator element
            dom_match = bool(article.find_elements(
                By.XPATH,
                ".//*[contains(@data-testid,'reply') or "
                "contains(text(),'Replying to') or contains(text(),'回复')]"
            ))
            is_reply = text_match or dom_match

        if is_retweet:
            type_ = "retweet"
        elif is_reply:
            type_ = "reply"
        else:
            type_ = "tweet"

        return {
            "id":              tweet_id,
            "created_at":      created_at,
            "type":            type_,
            "original_author": original_author,
            "text":            text,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scroll-based crawler
# ---------------------------------------------------------------------------

def wait_for_tweets(driver: webdriver.Chrome, timeout: float = 15) -> bool:
    """Wait until at least one tweet article appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        arts = driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
        if arts:
            return True
        time.sleep(1)
    return False


def is_logged_in(driver: webdriver.Chrome) -> bool:
    return "login" not in driver.current_url and "i/flow" not in driver.current_url


def crawl_page(
    driver: webdriver.Chrome,
    url: str,
    label: str,
    sink: dict,
    tweet_filter,
) -> None:
    """Navigate to url, scroll, filter tweets into sink, and auto-save periodically."""
    print(f"\n[*] 前往 {url}")
    driver.get(url)
    time.sleep(4)

    if not is_logged_in(driver):
        print("[!] 检测到未登录状态，请检查 Cookie。")
        return

    if not wait_for_tweets(driver):
        print(f"[!] {label} 页面未找到推文，跳过。")
        return

    seen_ids: set[str] = set()   # all article IDs parsed (pre-filter)
    last_saved = len(sink)
    no_new = 0
    page = 0
    while no_new < MAX_NO_NEW:
        page += 1
        articles = driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
        new_count = 0
        for art in articles:
            tweet = parse_article(art)
            if not tweet or not tweet["id"]:
                continue
            if tweet["id"] in seen_ids:
                continue
            seen_ids.add(tweet["id"])
            if tweet_filter(tweet) and tweet["id"] not in sink:
                sink[tweet["id"]] = tweet
                new_count += 1

        if new_count:
            no_new = 0
            print(f"  [{label}] 滚动 {page}，新增 {new_count}，累计 {len(sink)} 条")
            if len(sink) - last_saved >= SAVE_INTERVAL:
                _save_intermediate(sink)
                last_saved = len(sink)
        else:
            no_new += 1

        # Check for end-of-timeline indicators
        body_text = driver.find_element(By.TAG_NAME, "body").text
        if any(s in body_text for s in ("You've reached the end", "没有更多推文", "到达末尾")):
            print(f"  [{label}] 检测到时间线末尾。")
            break

        driver.execute_script(f"window.scrollBy(0, {SCROLL_PIXELS})")
        time.sleep(SCROLL_PAUSE)

    print(f"  [{label}] 完成，sink 累计 {len(sink)} 条")


# ---------------------------------------------------------------------------
# Type selection
# ---------------------------------------------------------------------------

def select_types() -> list[str]:
    options = {
        "1": "tweets",
        "2": "replies",
    }
    print("\n请选择要抓取的内容类型（输入编号，多选用空格分隔，直接回车 = 全部）：")
    print("  1. 推文 + 转推")
    print("  2. 回复")
    print("  3. 全部（推文 + 转推 + 回复）")

    raw = input("\n选项：").strip()

    if not raw or raw == "3":
        return ["all"]

    selected = []
    for ch in raw.split():
        if ch in options and options[ch] not in selected:
            selected.append(options[ch])
            print(f"  ✓ 已选：{options[ch]}")
        else:
            print(f"  [!] 无效选项 {ch!r}，已忽略")

    return selected if selected else ["all"]


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def _write(tweets_dict: dict) -> list:
    Path("data").mkdir(exist_ok=True)
    result = sorted(tweets_dict.values(), key=lambda t: t["created_at"], reverse=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def _save_intermediate(tweets_dict: dict) -> None:
    result = _write(tweets_dict)
    print(f"  [自动保存] {len(result)} 条 → {OUTPUT_FILE}")


def save(tweets_dict: dict) -> None:
    result = _write(tweets_dict)
    print(f"\n[*] 已保存 {len(result)} 条推文至 {OUTPUT_FILE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    selected = select_types()
    print()

    driver = make_driver()
    try:
        # Inject cookies (skip if using remote Chrome that already has a session)
        if not CHROME_REMOTE_URL:
            inject_cookies(driver)
            driver.get("https://x.com/home")
            time.sleep(4)
            if not is_logged_in(driver):
                print("[!] 登录失败，请更新 browser_cookies.json")
                return
            print("[*] 登录成功")
        else:
            print("[*] 使用已有浏览器会话，跳过 Cookie 注入")

        all_tweets: dict[str, dict] = {}

        if selected == ["all"]:
            # Replies tab contains everything: tweets, retweets, replies.
            # Crawl it once and keep all content with type annotation.
            crawl_page(
                driver,
                f"https://x.com/{TARGET_USER}/with_replies",
                "全部",
                all_tweets,
                lambda t: True,
            )
        else:
            if "tweets" in selected:
                crawl_page(
                    driver,
                    f"https://x.com/{TARGET_USER}",
                    "推文+转推",
                    all_tweets,
                    lambda t: t["type"] in ("tweet", "retweet"),
                )
            if "replies" in selected:
                crawl_page(
                    driver,
                    f"https://x.com/{TARGET_USER}/with_replies",
                    "回复",
                    all_tweets,
                    lambda t: t["type"] == "reply",
                )

        save(all_tweets)
        print("[+] 完成。")

    finally:
        if not CHROME_REMOTE_URL:
            driver.quit()
        else:
            print("[*] 远程 Chrome 保持运行，不关闭")


if __name__ == "__main__":
    main()
