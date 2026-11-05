"""子页面切换、滚动抓取循环、429 限流等待与基于 page.route 的断点续传。"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .browser import NotLoggedInError, is_logged_in
from .collector import TimelineCollector
from .config import Config
from .parser import inject_cursor_into_url
from .stealth import (
    human_idle_reading_gesture,
    human_move_and_click,
    human_reset_gaze,
    human_timeline_scroll,
    human_wait,
)

if TYPE_CHECKING:
    from playwright.async_api import Page, Route

log = logging.getLogger(__name__)

# --- 滚动与限流运行参数 ---
SCROLL_PAUSE = 3.0  # 每次向下滚动后等待 GraphQL 响应的最长秒数
MAX_NO_NEW = 12  # 连续多少次滚动无任何新节点后停止当前栏目（此时视为异常中断，不标记完成）
SCROLL_PIXELS = 2500
EXPORT_INTERVAL = 200  # 每累计写入多少条中断日志后自动导出一次 tweets.json
INITIAL_RESPONSE_TIMEOUT = 12.0
SINCE_STOP_STREAK = 5  # 连续遇到多少条早于 SINCE_DATE 的目标推文后停止（避免被单条置顶旧推文提前截断）

MAX_RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BASE_WAIT = 60.0
RATE_LIMIT_MAX_WAIT = 20 * 60.0

TWEET_SELECTOR = "article[data-testid='tweet']"
RETRY_BUTTON_SELECTOR = "button:has-text('Retry'), button:has-text('重试')"


# ---------------------------------------------------------------------------
# 子页面配置表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TabSpec:
    key: str  # 命令行参数中的类型名称
    label: str  # 终端显示名称，同时作为 state.json 中的断点键名
    path_suffix: str  # "" | "/reposts" | "/with_replies"
    tweet_type: str  # 单独抓取该栏目时保留的推文 type 值
    endpoint_re: re.Pattern[str]

    def url(self, user: str) -> str:
        return f"https://x.com/{user}{self.path_suffix}"

    def href(self, user: str) -> str:
        return f"/{user}{self.path_suffix}"


TABS: tuple[TabSpec, ...] = (
    TabSpec(
        "posts",
        "原创推文(Posts)",
        "",
        "tweet",
        re.compile(r"/graphql/[^/]+/(?:User(?:Originals|Tweets|Posts)|Profile)\w*Timeline\w*|/graphql/[^/]+/UserTweets(?:\?|$)"),
    ),
    TabSpec("reposts", "纯转推(Reposts)", "/reposts", "retweet", re.compile(r"/graphql/[^/]+/UserRepostsTimeline\w*")),
    TabSpec(
        "replies",
        "回复(Replies)",
        "/with_replies",
        "reply",
        re.compile(r"/graphql/[^/]+/User(?:RepliesTimeline\w*|TweetsAndReplies)"),
    ),
)
TABS_BY_KEY = {t.key: t for t in TABS}


class CrawlOutcome(enum.Enum):
    REACHED_BOTTOM = "已到达时间线最底部"
    REACHED_SINCE = "已滚动至起始日期之前"
    RESUME_AT_END = "断点游标已位于时间线末尾"
    EMPTY_TAB = "该栏目暂无帖子"
    STALLED = "连续多次滚动无新数据（可能是网络或接口异常），已保留断点"
    RATE_LIMITED = "多次触发频率限制，已保留断点"
    NO_DATA = "未捕获到任何推文数据，已保留断点"

    @property
    def is_complete(self) -> bool:
        return self in (CrawlOutcome.REACHED_BOTTOM, CrawlOutcome.REACHED_SINCE, CrawlOutcome.RESUME_AT_END, CrawlOutcome.EMPTY_TAB)


class RateLimitExhausted(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 429 频率限制等待与恢复
# ---------------------------------------------------------------------------


def compute_rate_limit_wait(reset_epoch: float | None, attempt: int, now: float | None = None) -> float:
    """
    计算触发 HTTP 429 后的等待秒数：优先采用响应头 `x-rate-limit-reset` 的重置时间戳（加随机缓冲），
    否则按 60s、120s、240s 翻倍递增等待，单次不超过 RATE_LIMIT_MAX_WAIT。
    """
    now = time.time() if now is None else now
    if reset_epoch and reset_epoch > now:
        wait = reset_epoch - now + random.uniform(5, 20)
    else:
        wait = RATE_LIMIT_BASE_WAIT * (2 ** max(0, attempt - 1)) + random.uniform(0, 15)
    return min(wait, RATE_LIMIT_MAX_WAIT)


async def _sleep_with_progress(seconds: float, label: str) -> None:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if remaining > 60:
            log.info("  [%s] 限流等待中，剩余约 %d 秒...", label, int(remaining))
        await asyncio.sleep(min(60.0, remaining))


async def _click_retry_if_present(page: Page, desc: str) -> bool:
    retry_btn = page.locator(RETRY_BUTTON_SELECTOR)
    try:
        if await retry_btn.count() == 0:
            return False
        await human_move_and_click(page, retry_btn.first, desc)
        await human_reset_gaze(page, "重试后视线复位")
        return True
    except Exception as exc:
        log.debug("点击重试按钮失败：%s", exc)
        return False


async def recover_from_rate_limit(page: Page, collector: TimelineCollector, label: str, attempt: int) -> None:
    wait = compute_rate_limit_wait(collector.rate_limit_reset, attempt)
    log.warning(
        "  [!] [%s] 触发 X 接口频率限制（第 %d/%d 次），等待 %.0f 秒后重试...",
        label,
        attempt,
        MAX_RATE_LIMIT_RETRIES,
        wait,
    )
    collector.clear_rate_limit()
    collector.response_event.clear()
    await _sleep_with_progress(wait, label)

    clicked = await _click_retry_if_present(page, "限流恢复重试按钮")
    if not clicked and not collector.seen_timeline_ids:
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            log.debug("限流恢复刷新页面失败：%s", exc)
    try:
        await asyncio.wait_for(collector.response_event.wait(), timeout=15.0)
        await asyncio.sleep(1.0)
    except TimeoutError:
        pass


# ---------------------------------------------------------------------------
# 基于 page.route 的断点游标注入
# ---------------------------------------------------------------------------


class ResumeInjector:
    """
    通过 Playwright `page.route` 拦截当前子页面的首个时间线 GraphQL 请求，将其中的 `variables.cursor`
    改写为上次保存的断点游标，使页面直接从中断位置继续加载，无需向页面注入任何脚本。
    """

    def __init__(self, page: Page, endpoint_re: re.Pattern[str], cursor: str, on_applied: Callable[[], None]) -> None:
        self.page = page
        self.endpoint_re = endpoint_re
        self.cursor = cursor
        self.on_applied = on_applied
        self.applied = False
        self._installed = False

    async def install(self) -> None:
        await self.page.route(self.endpoint_re, self._handle)
        self._installed = True

    async def uninstall(self) -> None:
        if not self._installed:
            return
        self._installed = False
        try:
            await self.page.unroute(self.endpoint_re, self._handle)
        except Exception as exc:
            log.debug("移除断点路由失败：%s", exc)

    async def _handle(self, route: Route) -> None:
        request = route.request
        new_url = None
        if not self.applied and request.method == "GET":
            new_url = inject_cursor_into_url(request.url, self.cursor)
        if new_url is None:
            await route.continue_()
            return
        self.applied = True
        self.on_applied()
        log.info("  [断点续传] 已将翻页游标注入首个时间线请求，直接从上次中断位置继续")
        await route.continue_(url=new_url)


# ---------------------------------------------------------------------------
# 终端账号信息面板
# ---------------------------------------------------------------------------


def print_profile_banner(cfg: Config, profile: dict[str, Any] | None, existing_count: int, output_file: Any) -> None:
    if not profile:
        return
    name = profile.get("name") or ""
    lines = [f"目标账号：@{profile.get('screen_name') or cfg.target_user}{f' ({name})' if name else ''}"]
    total, media = profile.get("total_tweets"), profile.get("media_tweets")
    if isinstance(total, int):
        lines.append(f"帖子总数：{total:,} 条" + (f"（含媒体帖 {media:,} 条）" if isinstance(media, int) else ""))
    followers, following = profile.get("followers"), profile.get("following")
    if isinstance(followers, int) or isinstance(following, int):
        lines.append(f"社交数据：关注 {following or 0:,}  |  粉丝 {followers or 0:,}")
    if cfg.since_bound or cfg.until_bound:
        lines.append(f"时间范围：{cfg.since_raw or '最早'}  →  {cfg.until_raw or '最新'}")
    lines.append(f"本地已存：{existing_count:,} 条（{output_file}）")
    log.info("  ┌──────────────────────────────────────────────────────────")
    for line in lines:
        log.info("  │ %s", line)
    log.info("  └──────────────────────────────────────────────────────────")


# ---------------------------------------------------------------------------
# 单个子页面抓取流程
# ---------------------------------------------------------------------------


def _on_user_profile(page_url: str, user: str) -> bool:
    path = urllib.parse.urlparse(page_url).path.rstrip("/").lower()
    u = f"/{user.lower()}"
    return path == u or path.startswith(u + "/")


async def _wait_response(collector: TimelineCollector, timeout: float) -> bool:
    try:
        await asyncio.wait_for(collector.response_event.wait(), timeout=timeout)
        return True
    except TimeoutError:
        return False


async def _click_tab(page: Page, collector: TimelineCollector, cfg: Config, spec: TabSpec) -> bool:
    """当已处于目标用户主页且目标标签尚未选中时，滚回顶部并点击对应标签切换子页面。"""
    tab = page.locator(f'[role="tablist"] [role="tab"][href="{spec.href(cfg.target_user)}" i]')
    try:
        if await tab.count() == 0 or await tab.first.get_attribute("aria-selected") == "true":
            return False
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(random.uniform(0.4, 0.7))
        collector.reset_progress()
        await human_move_and_click(page, tab.first, f"顶部【{spec.label}】标签页")
        return True
    except Exception as exc:
        log.debug("点击标签页失败：%s", exc)
        return False


def _got_any_response(collector: TimelineCollector) -> bool:
    return bool(collector.seen_timeline_ids) or collector.empty_timeline_responses > 0


async def _open_tab(page: Page, collector: TimelineCollector, cfg: Config, spec: TabSpec) -> None:
    url = spec.url(cfg.target_user)
    switched = False
    if _on_user_profile(page.url, cfg.target_user):
        switched = await _click_tab(page, collector, cfg, spec)
        if switched:
            log.info("\n[*] 切换至子页面：%s（【%s】）", url, spec.label)
    if not switched:
        log.info("\n[*] 前往 %s（【%s】）", url, spec.label)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            # 页面文档加载慢不代表后台时间线接口失败，后续由响应等待逻辑判断是否成功捕获数据
            log.warning("  [!] 打开 %s 超时或失败：%s", url, str(exc).splitlines()[0] if str(exc) else exc)

    await _wait_response(collector, INITIAL_RESPONSE_TIMEOUT)

    # 回退处理 1：点击顶部标签未触发对应接口请求时，直接通过 URL 导航
    if switched and not _got_any_response(collector) and not collector.rate_limited:
        collector.response_event.clear()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _wait_response(collector, INITIAL_RESPONSE_TIMEOUT)
        except Exception as exc:
            log.debug("直接导航至 %s 失败：%s", url, exc)

    # 回退处理 2：直接访问子页面 URL 却落回默认标签页时，补点一次目标标签
    if not _got_any_response(collector) and not collector.rate_limited:
        collector.response_event.clear()
        if await _click_tab(page, collector, cfg, spec):
            await _wait_response(collector, INITIAL_RESPONSE_TIMEOUT)


def _make_filter(spec: TabSpec, crawl_all: bool) -> Callable[[dict[str, Any]], bool]:
    if crawl_all:
        return lambda _: True
    return lambda t: t.get("type") == spec.tweet_type


async def crawl_tab(
    page: Page,
    collector: TimelineCollector,
    cfg: Config,
    spec: TabSpec,
    *,
    crawl_all: bool,
    resume_cursor: str | None,
    show_banner: bool,
) -> CrawlOutcome:
    """
    抓取单个子页面。仅在明确到达时间线底部、到达起始日期或栏目为空时才标记该栏目完成；
    其他中断情况均保留断点游标供下次续传。未登录时抛出 NotLoggedInError。
    """
    collector.begin_tab(spec.label, spec.endpoint_re, _make_filter(spec, crawl_all), hold_cursor=bool(resume_cursor))
    injector: ResumeInjector | None = None
    if resume_cursor:

        def _applied() -> None:
            collector.hold_cursor = False

        injector = ResumeInjector(page, spec.endpoint_re, resume_cursor, _applied)
        await injector.install()

    initial_count = len(collector.sink)
    try:
        outcome = await _crawl_tab_inner(page, collector, cfg, spec, injector, show_banner, initial_count)
    finally:
        if injector:
            await injector.uninstall()
        collector.hold_cursor = False

    if outcome.is_complete:
        collector.mark_tab_complete(spec.label)
        log.info(
            "  [%s] 完成（%s），本次新增 %d 条，本地累计 %d 条",
            spec.label,
            outcome.value,
            len(collector.sink) - initial_count,
            len(collector.sink),
        )
    else:
        collector.persist_state()
        log.warning("  [!] [%s] 未完成：%s。下次选择“断点续传”可继续。", spec.label, outcome.value)
    return outcome


async def _crawl_tab_inner(
    page: Page,
    collector: TimelineCollector,
    cfg: Config,
    spec: TabSpec,
    injector: ResumeInjector | None,
    show_banner: bool,
    initial_count: int,
) -> CrawlOutcome:
    label = spec.label
    await _open_tab(page, collector, cfg, spec)

    rl_attempts = 0
    while collector.rate_limited:
        rl_attempts += 1
        if rl_attempts > MAX_RATE_LIMIT_RETRIES:
            return CrawlOutcome.RATE_LIMITED
        await recover_from_rate_limit(page, collector, label, rl_attempts)

    if not await is_logged_in(page):
        raise NotLoggedInError("检测到未登录状态（缺少有效登录凭证或被跳转到登录流程），请检查 Cookie 或浏览器会话。")

    await human_reset_gaze(page, f"进入【{label}】页面视线复位")
    await human_wait(page, 900, 1800, f"在【{label}】首屏停留浏览")

    if show_banner:
        print_profile_banner(cfg, collector.user_profile, initial_count, collector.storage.tweets_file)

    if injector and not injector.applied:
        log.warning(
            "  [!] [%s] 未能注入断点游标（未拦截到时间线请求），将从顶部开始扫描；原断点游标会保留直到本栏目完成。",
            label,
        )

    if not collector.seen_timeline_ids:
        if collector.empty_timeline_responses >= 1:
            return CrawlOutcome.RESUME_AT_END if injector and injector.applied else CrawlOutcome.EMPTY_TAB
        try:
            await page.wait_for_selector(TWEET_SELECTOR, timeout=8000)
            await asyncio.sleep(1.5)
        except Exception:
            return CrawlOutcome.NO_DATA

    first_tweet = page.locator(TWEET_SELECTOR).first
    if await first_tweet.count() > 0:
        await human_idle_reading_gesture(page, first_tweet, text_len=160, elapsed_sec=1.5)

    prev_sink = len(collector.sink)
    prev_seen = len(collector.seen_timeline_ids)
    log.info(
        "  [%s] 初始加载完成：本次新增 %d 条，本地累计 %d 条（本页扫描节点 %d 个）", label, prev_sink - initial_count, prev_sink, prev_seen
    )
    _maybe_export(collector)

    if cfg.since_bound and collector.consecutive_older_than_since >= SINCE_STOP_STREAK:
        return CrawlOutcome.REACHED_SINCE

    no_new = 0
    step = 0
    fast_scroll = False
    while no_new < MAX_NO_NEW:
        step += 1
        collector.response_event.clear()
        await human_timeline_scroll(page, target_distance=SCROLL_PIXELS, fast_mode=fast_scroll)

        if await _wait_response(collector, SCROLL_PAUSE):
            pause_ms = random.randint(350, 750) if fast_scroll else random.randint(900, 1700)
            await page.wait_for_timeout(pause_ms)

        was_rate_limited = collector.rate_limited
        if was_rate_limited:
            rl_attempts += 1
            if rl_attempts > MAX_RATE_LIMIT_RETRIES:
                return CrawlOutcome.RATE_LIMITED
            await recover_from_rate_limit(page, collector, label, rl_attempts)

        new_qualifying = len(collector.sink) - prev_sink
        new_seen = len(collector.seen_timeline_ids) - prev_seen
        prev_sink, prev_seen = len(collector.sink), len(collector.seen_timeline_ids)

        if new_qualifying > 0 or new_seen > 0:
            no_new = 0
            rl_attempts = 0
            fast_scroll = new_qualifying == 0
            if new_qualifying > 0:
                log.info("  [%s] 滚动 %d，新增 %d 条，累计 %d 条", label, step, new_qualifying, len(collector.sink))
            else:
                log.info(
                    "  [%s] 滚动 %d，本页无新目标推文（跳过重复/范围外/他人节点 %d 个），累计 %d 条",
                    label,
                    step,
                    new_seen,
                    len(collector.sink),
                )
            _maybe_export(collector)
            if cfg.since_bound and collector.consecutive_older_than_since >= SINCE_STOP_STREAK:
                return CrawlOutcome.REACHED_SINCE
            continue

        if was_rate_limited:
            continue
        if injector and injector.applied and step <= 2 and collector.empty_timeline_responses >= 1:
            return CrawlOutcome.RESUME_AT_END
        if collector.timeline_terminated or collector.empty_timeline_responses >= 2:
            return CrawlOutcome.REACHED_BOTTOM

        no_new += 1
        # 当页面因瞬时接口错误出现“Something went wrong / Retry”按钮时，自动点击重试
        if await _click_retry_if_present(page, "时间线异常重试按钮"):
            await asyncio.sleep(2.0)

    # X 到达时间线最底部时常只返回一次无错误空页随后不再发起请求，此时视为正常触底；
    # 若全程未收到任何空页或结束标记（如网络断开或持续报错），视为异常卡住并保留断点游标。
    if collector.timeline_terminated or collector.empty_timeline_responses >= 1:
        return CrawlOutcome.REACHED_BOTTOM
    return CrawlOutcome.STALLED


def _maybe_export(collector: TimelineCollector) -> None:
    if collector.unexported < EXPORT_INTERVAL:
        return
    try:
        count = collector.export()
        log.info("  [自动保存] 累计 %d 条 → %s", count, collector.storage.tweets_file)
    except OSError as exc:
        log.error("[!] 导出 %s 失败（数据仍保存在中断日志中）：%s", collector.storage.tweets_file, exc)
