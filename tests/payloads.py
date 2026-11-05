"""构建用于单元测试的合成 X GraphQL 响应数据（结构与真实 UserTweets / UserTweetsAndReplies 响应一致）。"""

from __future__ import annotations

from typing import Any


def user(screen_name: str, *, new_schema: bool = True, rest_id: str = "1", **counts: Any) -> dict[str, Any]:
    if new_schema:
        return {
            "__typename": "User",
            "rest_id": rest_id,
            "core": {"screen_name": screen_name, "name": screen_name.title(), "created_at": "Fri May 14 18:06:19 +0000 2010"},
            "legacy": {"statuses_count": counts.get("statuses_count", 100), "media_count": 5},
            "relationship_counts": {"followers": counts.get("followers", 10), "following": 20},
        }
    return {
        "__typename": "User",
        "rest_id": rest_id,
        "legacy": {"screen_name": screen_name, "name": screen_name, "statuses_count": 100, "followers_count": 10},
    }


def tweet(
    tweet_id: str,
    author: str,
    text: str = "hello",
    created: str = "Wed Oct 10 20:19:24 +0000 2018",
    *,
    reply_to: tuple[str, str] | None = None,
    quoted: dict[str, Any] | None = None,
    retweeted: dict[str, Any] | None = None,
    note: str | None = None,
    visibility_wrapped: bool = False,
) -> dict[str, Any]:
    legacy: dict[str, Any] = {"id_str": tweet_id, "full_text": text, "created_at": created}
    if reply_to:
        legacy["in_reply_to_status_id_str"] = reply_to[0]
        legacy["in_reply_to_screen_name"] = reply_to[1]
        legacy["in_reply_to_user_id_str"] = "999"
    if retweeted is not None:
        legacy["retweeted_status_result"] = {"result": retweeted}
    obj: dict[str, Any] = {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {"user_results": {"result": user(author)}},
        "legacy": legacy,
    }
    if quoted is not None:
        obj["quoted_status_result"] = {"result": quoted}
    if note:
        obj["note_tweet"] = {"note_tweet_results": {"result": {"text": note}}}
    if visibility_wrapped:
        return {"__typename": "TweetWithVisibilityResults", "tweet": obj}
    return obj


def entry(result: dict[str, Any], entry_id: str = "tweet-x", promoted: bool = False) -> dict[str, Any]:
    item = {"itemType": "TimelineTweet", "tweet_results": {"result": result}}
    if promoted:
        item["promotedMetadata"] = {"advertiser": "x"}
    return {"entryId": entry_id, "content": {"entryType": "TimelineTimelineItem", "itemContent": item}}


def module(*results: dict[str, Any], entry_id: str = "profile-conversation-1") -> dict[str, Any]:
    return {
        "entryId": entry_id,
        "content": {
            "entryType": "TimelineTimelineModule",
            "items": [{"item": {"itemContent": {"tweet_results": {"result": r}}}} for r in results],
        },
    }


def cursor_entry(value: str, kind: str = "Bottom") -> dict[str, Any]:
    return {"entryId": f"cursor-{kind.lower()}-1", "content": {"cursorType": kind, "value": value}}


def timeline(*entries: dict[str, Any], bottom: str | None = "CUR-NEXT") -> dict[str, Any]:
    all_entries = list(entries)
    all_entries.append(cursor_entry("CUR-TOP", "Top"))
    if bottom:
        all_entries.append(cursor_entry(bottom))
    return {
        "data": {"user": {"result": {"timeline": {"timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": all_entries}]}}}}}
    }
