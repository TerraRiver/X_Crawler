"""Runtime configuration: environment variables (.env) + command-line arguments."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
ALL_TYPES: tuple[str, ...] = ("posts", "reposts", "replies")
MODES: tuple[str, ...] = ("resume", "from_top", "restart")

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


class ConfigError(ValueError):
    """Raised for invalid user configuration (fail fast instead of silently ignoring)."""


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


def _parse_bool(raw: str | None, name: str) -> bool | None:
    val = (raw or "").strip().lower()
    if not val:
        return None
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    raise ConfigError(f"{name} 的取值无效：{raw!r}（应为 true / false）")


def resolve_timezone(name: str | None) -> tzinfo | None:
    """Return a tzinfo for an IANA name ('Asia/Shanghai', 'UTC'); None means system local time."""
    if not name or not name.strip():
        return None
    name = name.strip()
    if name.upper() == "UTC":
        return UTC
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError / ValueError
        raise ConfigError(f"DATE_TZ 时区名称无效：{name!r}（示例：Asia/Shanghai、UTC）") from exc


def _localize(dt: datetime, tz: tzinfo | None) -> datetime:
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=tz) if tz is not None else dt.astimezone()  # naive -> system local tz


def parse_date_bound(raw: str | None, is_end: bool = False, tz: tzinfo | None = None) -> str | None:
    """
    Normalize YYYY-MM-DD / YYYY/MM/DD / ISO 8601 into a UTC ISO 8601 string.
    Date-only values are interpreted in `tz` (default: system local time zone) and cover the whole day.
    """
    if not raw or not raw.strip():
        return None
    val = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            day = datetime.strptime(val, fmt)
        except ValueError:
            continue
        if is_end:
            day = day.replace(hour=23, minute=59, second=59)
        return _localize(day, tz).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", val):
        raise ConfigError(f"日期不存在：{val!r}（格式正确，但该月没有这一天，例如 6 月只有 30 天）")
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"时间格式无效：{val!r}（请使用 YYYY-MM-DD 格式，如 2025-01-01）") from exc
    return _localize(dt, tz).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_username(raw: str) -> str:
    user = (raw or "").strip().lstrip("@").strip()
    if not USERNAME_RE.fullmatch(user):
        raise ConfigError(f"TARGET_USER 无效：{raw!r}（X 用户名只能包含字母、数字、下划线，长度 1~15）")
    return user


def parse_types(raw: str | None) -> tuple[str, ...] | None:
    if raw is None or not raw.strip():
        return None
    items: list[str] = []
    for part in re.split(r"[,\s]+", raw.strip().lower()):
        if not part:
            continue
        if part == "all":
            return ALL_TYPES
        if part not in ALL_TYPES:
            raise ConfigError(f"--types 中存在无效类型：{part!r}（可选：{', '.join(ALL_TYPES)}, all）")
        if part not in items:
            items.append(part)
    # Keep canonical crawl order
    return tuple(t for t in ALL_TYPES if t in items) or None


@dataclass(frozen=True)
class Config:
    target_user: str
    data_dir: Path
    cookie_file: Path
    proxy: str | None
    chrome_path: str | None
    chrome_user_data_dir: str
    headless: bool
    block_service_workers: bool
    since_raw: str
    until_raw: str
    since_bound: str | None
    until_bound: str | None
    types: tuple[str, ...] | None = None  # None -> ask interactively
    mode: str | None = None  # None -> ask interactively when needed
    verbose: bool = False

    @property
    def user_dir(self) -> Path:
        return self.data_dir / self.target_user


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="x-crawler",
        description="基于 Playwright + GraphQL 拦截的 X（Twitter）用户时间线采集工具。命令行参数优先于 .env。",
    )
    p.add_argument("-u", "--user", help="目标用户名（覆盖 TARGET_USER）")
    p.add_argument("--since", help="起始日期 YYYY-MM-DD（覆盖 SINCE_DATE）")
    p.add_argument("--until", help="截止日期 YYYY-MM-DD（覆盖 UNTIL_DATE）")
    p.add_argument("--tz", help="日期所用时区，如 Asia/Shanghai、UTC（覆盖 DATE_TZ，默认系统时区）")
    p.add_argument("--types", help="抓取类型，逗号分隔：posts,reposts,replies 或 all（不填则交互选择）")
    p.add_argument("--mode", choices=MODES, help="已有数据时的处理方式（不填则交互选择）")
    p.add_argument("--data-dir", help="数据目录（覆盖 DATA_DIR）")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None, help="是否无头模式")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志（含鼠标/滚轮模拟细节）")
    return p


def build_config(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Build configuration from CLI args (highest priority) and environment variables."""
    env = os.environ if env is None else env
    args = build_arg_parser().parse_args(argv)

    def env_str(name: str, default: str = "") -> str:
        return (env.get(name) or default).strip()

    target_user = normalize_username(args.user or env_str("TARGET_USER", "bhupen07"))
    tz = resolve_timezone(args.tz or env_str("DATE_TZ"))
    since_raw = (args.since if args.since is not None else env_str("SINCE_DATE")).strip()
    until_raw = (args.until if args.until is not None else env_str("UNTIL_DATE")).strip()
    since_bound = parse_date_bound(since_raw, is_end=False, tz=tz)
    until_bound = parse_date_bound(until_raw, is_end=True, tz=tz)
    if since_bound and until_bound and since_bound > until_bound:
        raise ConfigError(f"起始日期 {since_raw} 晚于截止日期 {until_raw}")

    headless = args.headless
    if headless is None:
        headless = _parse_bool(env.get("HEADLESS"), "HEADLESS")
    if headless is None:
        headless = bool(sys.platform.startswith("linux") and not env.get("DISPLAY"))

    block_sw = _parse_bool(env.get("BLOCK_SERVICE_WORKERS"), "BLOCK_SERVICE_WORKERS")

    return Config(
        target_user=target_user,
        data_dir=Path(args.data_dir or env_str("DATA_DIR", "data") or "data"),
        cookie_file=Path(env_str("COOKIE_FILE", "browser_cookies.json")),
        proxy=env_str("PROXY") or None,
        chrome_path=env_str("CHROME_PATH") or _default_chrome_path(),
        chrome_user_data_dir=env_str("CHROME_USER_DATA_DIR", ".chrome_profile") or ".chrome_profile",
        headless=headless,
        block_service_workers=True if block_sw is None else block_sw,
        since_raw=since_raw,
        until_raw=until_raw,
        since_bound=since_bound,
        until_bound=until_bound,
        types=parse_types(args.types),
        mode=args.mode,
        verbose=args.verbose,
    )
