"""Local Chrome persistent session, cookie import and login checks."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright

log = logging.getLogger(__name__)

LOGIN_WAIT_SECONDS = 180
_PROFILE_LOCK_HINTS = ("singletonlock", "processsingleton", "user data directory is already in use", "profile in use")


class BrowserLaunchError(RuntimeError):
    pass


class NotLoggedInError(RuntimeError):
    pass


def load_playwright_cookies(cookie_file: Path) -> list[dict[str, Any]]:
    """Convert a browser-extension cookie export (EditThisCookie / Cookie-Editor) into Playwright cookies."""
    with open(cookie_file, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError(f"{cookie_file} 顶层应为 Cookie 数组")

    cookies: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        raw_same_site = str(c.get("sameSite", "Lax")).lower()
        if raw_same_site in ("no_restriction", "none"):
            same_site = "None"
        elif raw_same_site == "strict":
            same_site = "Strict"
        else:
            same_site = "Lax"

        cookie: dict[str, Any] = {
            "name": c["name"],
            "value": str(c["value"]),
            "domain": c.get("domain", ".x.com"),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)) or same_site == "None",
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": same_site,
        }
        expiry = c.get("expirationDate") or c.get("expires")
        try:
            if expiry is not None and float(expiry) > 0:
                cookie["expires"] = float(expiry)
        except (TypeError, ValueError):
            pass
        cookies.append(cookie)
    return cookies


def _launch_kwargs(cfg: Config, user_data_dir: str) -> dict[str, Any]:
    args = ["--disable-blink-features=AutomationControlled"]
    if sys.platform.startswith("linux"):
        # Only needed in containers / CI; on desktop it triggers an "unsupported flag" infobar.
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    kwargs: dict[str, Any] = {
        "user_data_dir": user_data_dir,
        "headless": cfg.headless,
        "args": args,
        # Removes the "Chrome is being controlled by automated test software" infobar / automation switch
        "ignore_default_args": ["--enable-automation"],
        # Blocking service workers guarantees page.route() sees every GraphQL request (needed for resume)
        "service_workers": "block" if cfg.block_service_workers else "allow",
    }
    if cfg.headless:
        kwargs["viewport"] = {"width": 1280, "height": 900}
    else:
        kwargs["no_viewport"] = True  # use the real window size instead of an emulated viewport
    if cfg.proxy:
        kwargs["proxy"] = {"server": cfg.proxy}
    return kwargs


async def create_browser_session(pw: Playwright, cfg: Config) -> tuple[BrowserContext, Page]:
    """Launch local Google Chrome with a persistent profile. Returns (context, page)."""
    kwargs = _launch_kwargs(cfg, cfg.chrome_user_data_dir)
    if cfg.chrome_path and Path(cfg.chrome_path).exists():
        kwargs["executable_path"] = cfg.chrome_path
        browser_desc = f"本地 Google Chrome（{cfg.chrome_path}）"
    else:
        kwargs["channel"] = "chrome"
        browser_desc = "本地 Google Chrome（channel=chrome）"

    try:
        context = await pw.chromium.launch_persistent_context(**kwargs)
    except Exception as exc:
        msg = str(exc)
        if any(h in msg.lower() for h in _PROFILE_LOCK_HINTS):
            raise BrowserLaunchError(
                f"浏览器配置目录 {cfg.chrome_user_data_dir}/ 正被其他 Chrome 进程占用，请先关闭使用该目录的 Chrome 窗口后重试。"
            ) from exc
        # Never open a Chrome profile with bundled Chromium (version mismatch can corrupt it)
        fallback_dir = f"{cfg.chrome_user_data_dir.rstrip('/')}_chromium"
        log.warning("[!] 启动%s失败：%s", browser_desc, msg.splitlines()[0] if msg else exc)
        log.warning("[!] 回退为 Playwright Chromium，使用独立配置目录 %s/（需要重新登录）", fallback_dir)
        kwargs = _launch_kwargs(cfg, fallback_dir)
        try:
            context = await pw.chromium.launch_persistent_context(**kwargs)
        except Exception as exc2:
            raise BrowserLaunchError(
                f"回退启动 Chromium 也失败：{exc2}\n    可执行 `uv run playwright install chromium` 安装后重试。"
            ) from exc2
        browser_desc = "Playwright Chromium"

    log.info("[*] 已启动 %s%s", browser_desc, "（无头模式）" if cfg.headless else "")

    # Reuse the first tab and close any leftover tabs from previous sessions
    pages = list(context.pages)
    page = pages[0] if pages else await context.new_page()
    for extra in pages[1:]:
        try:
            await extra.close()
        except Exception as exc:
            log.debug("关闭残留标签页失败：%s", exc)
    return context, page


async def has_auth_cookie(context: BrowserContext) -> bool:
    """Check whether X's core `auth_token` session cookie exists and is non-empty."""
    try:
        cookies = await context.cookies(["https://x.com", "https://twitter.com"])
    except Exception as exc:
        log.debug("读取 Cookie 失败：%s", exc)
        return False
    return any(c.get("name") == "auth_token" and bool(c.get("value")) for c in cookies)


def is_login_path(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return path in ("/login", "/i/flow/login") or path.startswith("/i/flow/")


async def is_logged_in(page: Page) -> bool:
    """Verify login state using both URL path check and `auth_token` cookie presence."""
    if is_login_path(page.url):
        return False
    return await has_auth_cookie(page.context)


async def ensure_logged_in(context: BrowserContext, page: Page, cfg: Config) -> None:
    """Make sure the session is authenticated (cookie import or interactive login). Raises NotLoggedInError."""
    if not await has_auth_cookie(context) and cfg.cookie_file.exists():
        try:
            cookies = load_playwright_cookies(cfg.cookie_file)
        except (OSError, ValueError) as exc:
            raise NotLoggedInError(f"读取 Cookie 文件 {cfg.cookie_file} 失败：{exc}") from exc
        await context.add_cookies(cookies)
        log.info("[*] 已从 %s 注入 %d 条 Cookie", cfg.cookie_file, len(cookies))

    if await has_auth_cookie(context):
        log.info("[*] 检测到本地登录凭证（auth_token）")
        return

    if cfg.headless:
        raise NotLoggedInError("当前为无头模式且未检测到登录态，请先以有头模式运行完成登录，或提供有效的 browser_cookies.json")

    await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=30000)
    log.info(
        "[*] 当前未登录，请在打开的 Chrome 窗口中登录 X 账号（登录状态会保存在 %s/ 中，最多等待 %d 秒）...",
        cfg.chrome_user_data_dir,
        LOGIN_WAIT_SECONDS,
    )
    for _ in range(LOGIN_WAIT_SECONDS // 2):
        await asyncio.sleep(2.0)
        if await is_logged_in(page):
            log.info("[*] 登录验证成功")
            return
    raise NotLoggedInError("登录等待超时，请登录后重试或提供有效的 browser_cookies.json")
