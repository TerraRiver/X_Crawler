"""GraphQL 接口 JSON 数据解析工具函数（无 Playwright 依赖，可直接用于单元测试）。"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# 接口 URL 匹配正则
# ---------------------------------------------------------------------------

TIMELINE_RE = re.compile(
    r"/graphql/[^/]+/(?:User(?:Originals|Reposts|Tweets|Replies|Posts|Media)|Profile)\w*Timeline\w*"
    r"|/graphql/[^/]+/UserTweets(?:AndReplies)?"
)
USER_PROFILE_RE = re.compile(r"/graphql/[^/]+/UserByScreenName")

_UNAVAILABLE_TYPENAMES = ("TweetTombstone", "TweetUnavailable", "TweetPreviewDisplay")

# ---------------------------------------------------------------------------
# 通用辅助函数
# ---------------------------------------------------------------------------


def dig(obj: Any, *keys: Any, default: Any = None) -> Any:
    """按路径安全读取嵌套字典或列表；遇到键不存在、值为 None 或类型不匹配时返回 `default`。"""
    cur = obj
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_twitter_date(raw_date: Any) -> str:
    """将 X 接口返回的 'Wed Oct 10 20:19:24 +0000 2018' 转换为 ISO 8601 UTC 时间字符串；无法解析时返回 ''。"""
    if not raw_date or not isinstance(raw_date, str):
        return ""
    try:
        dt = datetime.strptime(raw_date, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 推文与用户字段提取
# ---------------------------------------------------------------------------


def unwrap_tweet(result: Any) -> dict[str, Any] | None:
    """解包 TweetWithVisibilityResults 包装层，并过滤已删除或不可见的推文节点。"""
    if not isinstance(result, dict):
        return None
    typename = result.get("__typename")
    if typename == "TweetWithVisibilityResults":
        result = result.get("tweet")
    elif typename in _UNAVAILABLE_TYPENAMES:
        return None
    if not isinstance(result, dict) or not isinstance(result.get("legacy"), dict):
        return None
    return result


def tweet_id_of(tweet_obj: dict[str, Any]) -> str:
    return str(tweet_obj.get("rest_id") or dig(tweet_obj, "legacy", "id_str") or "")


def extract_screen_name(tweet_obj: dict[str, Any]) -> str:
    """从推文对象中提取作者用户名（兼容新旧两套 GraphQL 字段结构）。"""
    user_res = dig(tweet_obj, "core", "user_results", "result")
    return str(dig(user_res, "core", "screen_name") or dig(user_res, "legacy", "screen_name") or "")


def extract_full_text(tweet_obj: dict[str, Any]) -> str:
    """提取推文全文，长推文优先读取未截断的 note_tweet 字段，并还原 HTML 转义字符。"""
    note_text = dig(tweet_obj, "note_tweet", "note_tweet_results", "result", "text")
    if isinstance(note_text, str) and note_text:
        return html.unescape(note_text)
    raw_text = dig(tweet_obj, "legacy", "full_text") or dig(tweet_obj, "legacy", "text") or ""
    return html.unescape(str(raw_text))


def extract_user_profile_info(user_res: Any) -> dict[str, Any] | None:
    """从 GraphQL User 节点中提取发帖总数、粉丝数及个人简介等基本信息。"""
    if not isinstance(user_res, dict) or user_res.get("__typename") != "User":
        return None

    def first(*paths: tuple) -> Any:
        for path in paths:
            val = dig(user_res, *path)
            if val not in (None, ""):
                return val
        return None

    def count(new_path: tuple, legacy_key: str) -> Any:
        container = dig(user_res, *new_path[:-1])
        if isinstance(container, dict) and new_path[-1] in container:
            return container[new_path[-1]]
        return dig(user_res, "legacy", legacy_key)

    screen_name = first(("core", "screen_name"), ("legacy", "screen_name")) or ""
    total_tweets = count(("tweet_counts", "tweets"), "statuses_count")
    if total_tweets is None and not screen_name:
        return None

    return {
        "id": user_res.get("rest_id") or "",
        "screen_name": screen_name,
        "name": first(("core", "name"), ("legacy", "name")) or "",
        "description": first(("profile_bio", "description"), ("legacy", "description")) or "",
        "location": first(("location", "location"), ("legacy", "location")) or "",
        "created_at": parse_twitter_date(first(("core", "created_at"), ("legacy", "created_at"))),
        "total_tweets": total_tweets,
        "media_tweets": count(("tweet_counts", "media_tweets"), "media_count"),
        "followers": count(("relationship_counts", "followers"), "followers_count"),
        "following": count(("relationship_counts", "following"), "friends_count"),
    }


# ---------------------------------------------------------------------------
# 响应 JSON 遍历
# ---------------------------------------------------------------------------


def iter_tweet_result_nodes(node: Any) -> list[dict[str, Any]]:
    """递归遍历 GraphQL 响应，收集所有非推广（非 promoted）的 `tweet_results.result` 字典。"""
    found: list[dict[str, Any]] = []
    stack: list[Any] = [node]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            entry_id = str(obj.get("entryId", ""))
            if entry_id.startswith("promoted-") or "promotedMetadata" in obj:
                continue
            res = dig(obj, "tweet_results", "result")
            if isinstance(res, dict):
                found.append(res)
            stack.extend(reversed(list(obj.values())))
        elif isinstance(obj, list):
            stack.extend(reversed(obj))
    return found


def extract_bottom_cursor(node: Any) -> str | None:
    """从时间线 GraphQL 响应中提取向后翻页的 Bottom 游标值。"""
    stack: list[Any] = [node]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            if obj.get("cursorType") == "Bottom" and isinstance(obj.get("value"), str):
                return obj["value"]
            stack.extend(reversed(list(obj.values())))
        elif isinstance(obj, list):
            stack.extend(reversed(obj))
    return None


def has_bottom_terminator(node: Any) -> bool:
    """检查响应中是否包含 X 明确返回的时间线底部结束指令（TimelineTerminateTimeline / Bottom）。"""
    stack: list[Any] = [node]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            if obj.get("type") == "TimelineTerminateTimeline" and obj.get("direction") == "Bottom":
                return True
            stack.extend(obj.values())
        elif isinstance(obj, list):
            stack.extend(obj)
    return False


def has_timeline_instructions(node: Any) -> bool:
    """检查响应中是否包含实际的时间线 `instructions` 列表（排除空的或残缺的 `data` 对象）。"""
    stack: list[Any] = [node]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            if isinstance(obj.get("instructions"), list):
                return True
            stack.extend(obj.values())
        elif isinstance(obj, list):
            stack.extend(obj)
    return False


def graphql_error_text(payload: Any) -> str:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return ""
    return " ".join(str(e.get("message", "")) for e in errors if isinstance(e, dict))


def inject_cursor_into_url(url: str, cursor: str) -> str | None:
    """将请求 URL 查询参数中的 `variables.cursor` 改写为指定 `cursor`；若 URL 不含 variables 则返回 None。"""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    new_query = []
    replaced = False
    for key, value in query:
        if key == "variables" and not replaced:
            try:
                variables = json.loads(value)
            except ValueError:
                return None
            if not isinstance(variables, dict):
                return None
            variables["cursor"] = cursor
            value = json.dumps(variables, separators=(",", ":"), ensure_ascii=False)
            replaced = True
        new_query.append((key, value))
    if not replaced:
        return None
    encoded = urllib.parse.urlencode(new_query, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, parts.fragment))


# ---------------------------------------------------------------------------
# 推文缓存池与输出记录组装
# ---------------------------------------------------------------------------


def _basic_view(basic: dict[str, Any] | None) -> dict[str, Any] | None:
    if not basic:
        return None
    return {k: basic[k] for k in ("id", "author", "created_at", "text")}


class TweetPool:
    """记录所有已见过推文（含其他用户推文）的基础信息（id -> basic），用于补全回复原帖与引用推文上下文。"""

    def __init__(self) -> None:
        self._pool: dict[str, dict[str, Any]] = {}

    def __contains__(self, tweet_id: str) -> bool:
        return tweet_id in self._pool

    def __len__(self) -> int:
        return len(self._pool)

    def get(self, tweet_id: str | None) -> dict[str, Any] | None:
        return self._pool.get(tweet_id) if tweet_id else None

    def seed(self, records: Iterable[dict[str, Any]], default_author: str = "") -> None:
        for t in records:
            tid = str(t.get("id") or "")
            if tid and tid not in self._pool:
                self._pool[tid] = {
                    "id": tid,
                    "author": t.get("author") or default_author,
                    "created_at": t.get("created_at", ""),
                    "text": t.get("text", ""),
                }

    def register(self, raw: Any) -> dict[str, Any] | None:
        """注册单条推文（及其内嵌的引用推文与被转发原帖），返回其基础信息字典。"""
        tweet_obj = unwrap_tweet(raw)
        if not tweet_obj:
            return None
        tweet_id = tweet_id_of(tweet_obj)
        if not tweet_id:
            return None
        basic = {
            "id": tweet_id,
            "author": extract_screen_name(tweet_obj),
            "created_at": parse_twitter_date(dig(tweet_obj, "legacy", "created_at")),
            "text": extract_full_text(tweet_obj),
        }
        self._pool[tweet_id] = basic
        for nested in (
            dig(tweet_obj, "quoted_status_result", "result"),
            dig(tweet_obj, "legacy", "retweeted_status_result", "result"),
        ):
            if nested:
                self.register(nested)
        return basic

    def basic_of(self, raw: Any) -> dict[str, Any] | None:
        """查询原始推文节点在缓存池中的基础信息（若未注册则先注册）。"""
        tweet_obj = unwrap_tweet(raw)
        if not tweet_obj:
            return None
        return self.get(tweet_id_of(tweet_obj)) or self.register(tweet_obj)


def build_tweet_record(raw: Any, pool: TweetPool) -> dict[str, Any] | None:
    """根据单个推文节点构建标准化输出字典（区分原创、纯转推、回复与引用推文）。"""
    tweet_obj = unwrap_tweet(raw)
    if not tweet_obj:
        return None
    base = pool.basic_of(tweet_obj)
    if not base:
        return None

    legacy = tweet_obj.get("legacy") or {}
    text = base["text"]
    quoted_tweet = _basic_view(pool.basic_of(dig(tweet_obj, "quoted_status_result", "result")))
    original_author = ""
    original_id: str | None = None
    original_created_at: str | None = None
    reply_to: dict[str, Any] | None = None

    rt_raw = dig(legacy, "retweeted_status_result", "result")
    rt_obj = unwrap_tweet(rt_raw)
    if rt_raw is not None:
        type_ = "retweet"
        rt_base = pool.basic_of(rt_obj) if rt_obj else None
        if rt_base:
            original_author = rt_base["author"]
            original_id = rt_base["id"] or None
            original_created_at = rt_base["created_at"] or None
            if rt_base["text"]:
                text = rt_base["text"]
        if not quoted_tweet and rt_obj:
            quoted_tweet = _basic_view(pool.basic_of(dig(rt_obj, "quoted_status_result", "result")))
    else:
        reply_to_id = str(legacy.get("in_reply_to_status_id_str") or "")
        if reply_to_id or legacy.get("in_reply_to_user_id_str"):
            type_ = "reply"
            parent = pool.get(reply_to_id)
            reply_to = {
                "id": reply_to_id or None,
                "author": ((parent or {}).get("author") or legacy.get("in_reply_to_screen_name")) or None,
                "created_at": parent["created_at"] if parent else None,
                "text": parent["text"] if parent else None,
            }
        else:
            type_ = "tweet"

    return {
        "id": base["id"],
        "created_at": base["created_at"],
        "author": base["author"],
        "type": type_,
        "original_author": original_author,
        "original_id": original_id,
        "original_created_at": original_created_at,
        "text": text,
        "reply_to": reply_to,
        "quoted_tweet": quoted_tweet,
    }


@dataclass
class ParsedPage:
    records: list[dict[str, Any]] = field(default_factory=list)
    bottom_cursor: str | None = None
    has_timeline: bool = False  # 响应中包含实际的时间线 `instructions` 列表
    error_text: str = ""
    terminated_bottom: bool = False
    author_profile: dict[str, Any] | None = None

    @property
    def is_empty_timeline(self) -> bool:
        """表示一次无错误且不包含任何推文的正常时间线响应（到达时间线末尾或该栏目为空）。"""
        # GraphQL 报错常伴随 HTTP 200 和残缺的 "data" 字段，不能将带错误的响应视为“到底部”
        return not self.records and not self.error_text and (self.bottom_cursor is not None or self.has_timeline)

    @property
    def is_rate_limit_error(self) -> bool:
        return not self.records and "rate limit" in self.error_text.lower()


def parse_timeline_payload(
    payload: Any,
    pool: TweetPool,
    profile_screen_name: str | None = None,
) -> ParsedPage:
    """
    解析单页时间线 GraphQL 响应，提取推文记录列表（含所有作者）与翻页游标信息。
    若指定了 `profile_screen_name`，同时从推文内嵌的用户对象中提取该账号的资料。
    """
    page = ParsedPage()
    if not isinstance(payload, dict):
        return page
    page.has_timeline = has_timeline_instructions(payload.get("data"))
    page.error_text = graphql_error_text(payload)
    page.bottom_cursor = extract_bottom_cursor(payload)
    page.terminated_bottom = has_bottom_terminator(payload)

    raw_nodes = iter_tweet_result_nodes(payload)
    # 第一遍：先将本页所有推文注册进缓存池，确保同一页内的被回复原帖与引用推文无论先后顺序都能匹配
    for node in raw_nodes:
        pool.register(node)

    wanted = (profile_screen_name or "").lower()
    for node in raw_nodes:
        record = build_tweet_record(node, pool)
        if not record or not record["id"]:
            continue
        page.records.append(record)
        if wanted and page.author_profile is None and record["author"].lower() == wanted:
            user_res = dig(unwrap_tweet(node), "core", "user_results", "result")
            page.author_profile = extract_user_profile_info(user_res)
    return page
