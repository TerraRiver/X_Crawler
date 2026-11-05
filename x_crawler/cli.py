"""Command-line entry point: configuration, interactive prompts and orchestration of tab crawls."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .config import ALL_TYPES, Config, ConfigError, build_config
from .storage import CheckpointState, Storage, StorageError

log = logging.getLogger("x_crawler")

TYPE_PROMPT_OPTIONS = {"1": "posts", "2": "reposts", "3": "replies"}


def setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("x_crawler").setLevel(logging.DEBUG if verbose else logging.INFO)


def select_types() -> tuple[str, ...]:
    print("\n请选择要抓取的内容类型（输入编号，多选用空格分隔，直接回车 = 全部）：")
    print("  1. 原创推文 Posts（含纯原创推文与带评论引用推文）")
    print("  2. 纯转推 Reposts（直接一键转发他人的推文）")
    print("  3. 回复 Replies（含被回复原帖与引用推文上下文）")
    print("  4. 全部（依次进入 Posts → Reposts → Replies 三个子页面抓取）")
    raw = input("\n选项：").strip()
    if not raw or raw == "4":
        return ALL_TYPES

    selected: list[str] = []
    for ch in raw.split():
        key = TYPE_PROMPT_OPTIONS.get(ch)
        if key and key not in selected:
            selected.append(key)
            print(f"  ✓ 已选：{key}")
        else:
            print(f"  [!] 无效选项 {ch!r}，已忽略")
    return tuple(t for t in ALL_TYPES if t in selected) or ALL_TYPES


def select_resume_mode(
    cfg: Config,
    storage: Storage,
    existing_count: int,
    state: CheckpointState,
    active_labels: list[str],
) -> str:
    """Returns 'resume' | 'from_top' | 'restart'."""
    has_cursor = any(lbl in state.cursors for lbl in active_labels)
    has_partial = bool(state.completed_tabs) and any(lbl not in state.completed_tabs for lbl in active_labels)
    can_resume = has_cursor or has_partial

    if cfg.mode:
        return cfg.mode
    if existing_count == 0 and not can_resume:
        return "from_top"

    if has_cursor:
        hint = "，且存在上次中断的翻页游标"
    elif has_partial:
        hint = f"，已完成【{'、'.join(sorted(state.completed_tabs))}】抓取"
    else:
        hint = ""
    print(f"\n[*] 检测到 @{cfg.target_user} 本地已有 {existing_count} 条推文记录（{storage.tweets_file}{hint}）：")
    if can_resume:
        print("  1. 断点续传（从上次中断位置继续，已完成的栏目自动跳过，默认）")
        print("  2. 从头扫描（从最新推文开始向下滚动，自动合并已有数据）")
        print("  3. 清空重抓（将本地记录与断点移入 backups/ 后完全重新开始）")
        raw = input("\n续传选项（直接回车 = 1）：").strip()
        return {"2": "from_top", "3": "restart"}.get(raw, "resume")
    print("  1. 从头扫描并合并（保留已有数据，从最新推文开始向下滚动，默认）")
    print("  2. 清空重抓（将本地记录移入 backups/ 后完全重新开始）")
    raw = input("\n选项（直接回车 = 1）：").strip()
    return "restart" if raw == "2" else "from_top"


@dataclass
class RunPlan:
    storage: Storage
    sink: dict[str, dict[str, Any]]
    state: CheckpointState
    selected: tuple[str, ...]
    mode: str


def prepare(cfg: Config) -> RunPlan:
    """Synchronous pre-browser phase: prompts, load data & checkpoint, apply the chosen mode."""
    from .crawler import TABS

    storage = Storage(cfg.data_dir, cfg.target_user)
    selected = cfg.types or select_types()
    sink = storage.load_tweets()  # raises StorageError on corrupt data (never overwrite it)
    state = storage.load_state(cfg.since_bound, cfg.until_bound)

    active_labels = [t.label for t in TABS if t.key in selected]
    mode = select_resume_mode(cfg, storage, len(sink), state, active_labels)
    if mode == "restart":
        backup = storage.backup_and_reset()
        sink.clear()
        state = CheckpointState()
        log.info("[*] 已清空本地数据与断点%s。", f"（原文件已备份至 {backup}）" if backup else "")
    elif mode == "from_top":
        for lbl in active_labels:
            state.cursors.pop(lbl, None)
            state.completed_tabs.discard(lbl)
    return RunPlan(storage, sink, state, selected, mode)


async def run(cfg: Config, plan: RunPlan) -> int:
    # Heavy imports are deferred so `--help` / config errors don't require Playwright
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    from .browser import BrowserLaunchError, NotLoggedInError, create_browser_session, ensure_logged_in
    from .collector import TimelineCollector
    from .crawler import TABS, CrawlOutcome, crawl_tab

    storage, sink, state, selected, mode = plan.storage, plan.sink, plan.state, plan.selected, plan.mode
    tabs = [t for t in TABS if t.key in selected]
    crawl_all = set(selected) == set(ALL_TYPES)
    exit_code = 0

    async with async_playwright() as pw:
        try:
            context, page = await create_browser_session(pw, cfg)
        except BrowserLaunchError as exc:
            log.error("[!] %s", exc)
            return 2

        collector = TimelineCollector(cfg.target_user, storage, sink, state, cfg.since_bound, cfg.until_bound)
        page.on("response", collector.handle_response)
        try:
            await ensure_logged_in(context, page, cfg)
            shown_banner = False
            for spec in tabs:
                if mode == "resume" and spec.label in state.completed_tabs and spec.label not in state.cursors:
                    log.info("\n[*] [%s] 上次已完整抓取，跳过（如需重新扫描请选择“从头扫描”）", spec.label)
                    continue
                try:
                    outcome = await crawl_tab(
                        page,
                        collector,
                        cfg,
                        spec,
                        crawl_all=crawl_all,
                        resume_cursor=state.cursors.get(spec.label) if mode == "resume" else None,
                        show_banner=not shown_banner,
                    )
                except PlaywrightError as exc:
                    # e.g. browser window closed / navigation crashed: keep the checkpoint, stop gracefully
                    log.error("[!] [%s] 浏览器操作异常，已保留断点：%s", spec.label, str(exc).splitlines()[0] if str(exc) else exc)
                    exit_code = 1
                    break
                shown_banner = shown_banner or bool(collector.user_profile)
                if not outcome.is_complete:
                    exit_code = 1
                if outcome is CrawlOutcome.RATE_LIMITED:
                    log.warning("[!] 为保护账号，本次运行提前结束，请稍后再试。")
                    break
            log.info("[+] 运行结束。")
        except NotLoggedInError as exc:
            log.error("[!] %s", exc)
            exit_code = 2
        finally:
            # Always persist collected tweets & checkpoint, even on Ctrl+C / errors
            collector.flush_reply_contexts()
            if not sink:
                log.info("\n[!] 本次未采集到任何符合条件的推文，未生成文件。")
            elif collector.dirty or not storage.tweets_file.exists():
                try:
                    count = collector.export()
                    log.info("\n[*] 已保存共 %d 条推文至 %s", count, storage.tweets_file)
                except OSError as exc:
                    log.error("[!] 导出 %s 失败（数据仍保存在 %s 中）：%s", storage.tweets_file, storage.journal_file, exc)
            else:
                log.info("\n[*] 本次没有新增或更新的推文，%s 保持不变（共 %d 条）", storage.tweets_file, len(sink))
            if state.cursors or state.completed_tabs:
                collector.persist_state()
            if collector.pending_reply_count:
                log.info("[*] 有 %d 条回复的被回复原帖未出现在接口数据中（可能已删除或不可见）", collector.pending_reply_count)
            try:
                await context.close()
            except Exception as exc:
                log.debug("关闭浏览器失败：%s", exc)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    try:
        cfg = build_config(argv)
    except ConfigError as exc:
        print(f"[!] 配置错误：{exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.verbose)

    try:
        plan = prepare(cfg)
    except StorageError as exc:
        log.error("[!] %s", exc)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n[!] 已取消。")
        return 130

    try:
        return asyncio.run(run(cfg, plan))
    except KeyboardInterrupt:
        # run()'s finally block has already exported data & checkpoint
        print("\n[!] 用户中断运行。已采集的数据与断点均已保存，可选择“断点续传”继续。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
