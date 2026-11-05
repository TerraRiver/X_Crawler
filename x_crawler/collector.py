"""Passive GraphQL response collector: parses timeline payloads, filters records, persists safely."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .parser import (
    TIMELINE_RE,
    USER_PROFILE_RE,
    ParsedPage,
    TweetPool,
    dig,
    extract_user_profile_info,
    parse_timeline_payload,
)
from .storage import CheckpointState, Storage

if TYPE_CHECKING:
    from playwright.async_api import Response

log = logging.getLogger(__name__)

TweetFilter = Callable[[dict[str, Any]], bool]


def _accept_all(_: dict[str, Any]) -> bool:
    return True


class TimelineCollector:
    """
    Intercepts X GraphQL timeline responses, resolves reply/quote contexts and manages checkpoints.

    Persistence order per response: new/updated records -> crash journal (fsync) -> cursor -> state.json.
    The checkpoint therefore never points past data that is not on disk.
    """

    def __init__(
        self,
        target_user: str,
        storage: Storage,
        sink: dict[str, dict[str, Any]],
        state: CheckpointState,
        since_bound: str | None = None,
        until_bound: str | None = None,
    ) -> None:
        self.target_user = target_user.lower()
        self.storage = storage
        self.sink = sink
        self.state = state
        self.since_bound = since_bound
        self.until_bound = until_bound

        self.pool = TweetPool()
        self.pool.seed(sink.values(), default_author=target_user)
        self.user_profile: dict[str, Any] | None = storage.load_profile()

        # parent tweet id -> ids of sink records whose reply_to context is still missing
        self._pending_replies: dict[str, set[str]] = {}
        for t in sink.values():
            self._track_pending_reply(t)

        self.response_event = asyncio.Event()
        self.unexported = 0  # records appended to the journal since the last tweets.json export
        self.dirty = False  # sink changed since the last export (independent of journal success)

        # Per-tab state (reset by begin_tab)
        self.current_label = ""
        self.expected_endpoint_re: re.Pattern[str] | None = None
        self.tweet_filter: TweetFilter = _accept_all
        self.seen_timeline_ids: set[str] = set()
        self.empty_timeline_responses = 0
        self.timeline_terminated = False
        self.consecutive_older_than_since = 0
        self.rate_limited = False
        self.rate_limit_reset: float | None = None
        # While a resume cursor is pending but not yet injected, page-1 cursors must not overwrite it
        self.hold_cursor = False

    # ------------------------------------------------------------------ tab lifecycle

    def begin_tab(
        self,
        label: str,
        endpoint_re: re.Pattern[str] | None,
        tweet_filter: TweetFilter | None,
        hold_cursor: bool = False,
    ) -> None:
        self.current_label = label
        self.expected_endpoint_re = endpoint_re
        self.tweet_filter = tweet_filter or _accept_all
        self.hold_cursor = hold_cursor
        self.reset_progress()
        self.clear_rate_limit()

    def reset_progress(self) -> None:
        self.seen_timeline_ids.clear()
        self.empty_timeline_responses = 0
        self.timeline_terminated = False
        self.consecutive_older_than_since = 0
        self.response_event.clear()

    def clear_rate_limit(self) -> None:
        self.rate_limited = False
        self.rate_limit_reset = None

    def mark_tab_complete(self, label: str) -> None:
        self.state.completed_tabs.add(label)
        self.state.cursors.pop(label, None)
        self.persist_state()

    def persist_state(self) -> bool:
        try:
            self.storage.save_state(self.state, self.since_bound, self.until_bound)
            return True
        except OSError as exc:
            log.error("[!] 保存断点文件失败：%s", exc)
            return False

    def export(self) -> int:
        """Write tweets.json (sorted) and clear the journal. Returns the number of records written."""
        count = self.storage.export(self.sink)
        self.unexported = 0
        self.dirty = False
        return count

    def flush_reply_contexts(self) -> int:
        """Resolve any remaining reply contexts and journal the updated records."""
        updated = self.resolve_missing_reply_contexts()
        self._journal(updated)
        return len(updated)

    # ------------------------------------------------------------------ response handling

    async def handle_response(self, response: Response) -> None:
        """Playwright passive response listener (profile metadata + timeline pages)."""
        url = response.url
        if USER_PROFILE_RE.search(url):
            if response.status == 200:
                try:
                    self.process_profile_payload(await response.json())
                except Exception as exc:
                    log.debug("解析 UserByScreenName 响应失败：%s", exc)
            return

        if not TIMELINE_RE.search(url):
            return
        if self.expected_endpoint_re and not self.expected_endpoint_re.search(url):
            return

        try:
            if response.status == 429:
                self._flag_rate_limit(response.headers.get("x-rate-limit-reset"))
                return
            if response.status != 200:
                log.debug("时间线接口返回 HTTP %s：%s", response.status, url)
                return
            try:
                payload = await response.json()
            except Exception as exc:
                log.debug("时间线响应不是合法 JSON：%s", exc)
                return
            self.process_timeline_payload(payload)
        except Exception:
            log.exception("[!] 处理时间线响应时发生异常（已跳过该页）")
        finally:
            self.response_event.set()

    def _flag_rate_limit(self, reset_header: str | None) -> None:
        self.rate_limited = True
        try:
            self.rate_limit_reset = float(reset_header) if reset_header else None
        except ValueError:
            self.rate_limit_reset = None

    def process_profile_payload(self, payload: Any) -> None:
        info = extract_user_profile_info(dig(payload, "data", "user", "result"))
        if info and str(info.get("screen_name", "")).lower() == self.target_user:
            self.user_profile = info
            self.storage.save_profile(info)

    def process_timeline_payload(self, payload: Any) -> ParsedPage:
        """Parse one timeline page, store qualifying records, then advance the checkpoint cursor."""
        page = parse_timeline_payload(payload, self.pool, profile_screen_name=None if self.user_profile else self.target_user)
        if page.terminated_bottom:
            self.timeline_terminated = True
        if page.author_profile and not self.user_profile:
            self.user_profile = page.author_profile
            self.storage.save_profile(page.author_profile)

        if not page.records:
            if page.is_rate_limit_error:
                self._flag_rate_limit(None)
            elif page.is_empty_timeline:
                self.empty_timeline_responses += 1
            return page

        self.empty_timeline_responses = 0
        to_journal: list[dict[str, Any]] = []
        for record in page.records:
            self.seen_timeline_ids.add(record["id"])
            if record["author"].lower() != self.target_user:
                continue
            if not self._within_date_range(record.get("created_at") or ""):
                continue
            if record["id"] not in self.sink and self.tweet_filter(record):
                self.sink[record["id"]] = record
                self._track_pending_reply(record)
                to_journal.append(record)

        journaled_ids = {r["id"] for r in to_journal}
        to_journal.extend(r for r in self.resolve_missing_reply_contexts() if r["id"] not in journaled_ids)
        if not self._journal(to_journal):
            return page  # do NOT advance the cursor past data that is not on disk

        if page.bottom_cursor and self.current_label and not self.hold_cursor:
            self.state.cursors[self.current_label] = page.bottom_cursor
            self.persist_state()
        return page

    def _within_date_range(self, created_at: str) -> bool:
        if self.since_bound and created_at and created_at < self.since_bound:
            self.consecutive_older_than_since += 1
            return False
        if created_at:
            self.consecutive_older_than_since = 0
        return not (self.until_bound and created_at and created_at > self.until_bound)

    def _journal(self, records: list[dict[str, Any]]) -> bool:
        if not records:
            return True
        self.dirty = True
        try:
            self.unexported += self.storage.append_journal(records)
            return True
        except OSError as exc:
            log.error("[!] 写入中断日志失败（断点游标暂不推进）：%s", exc)
            return False

    # ------------------------------------------------------------------ reply context backfill

    def _track_pending_reply(self, record: dict[str, Any]) -> None:
        reply_to = record.get("reply_to")
        # Unresolved = parent never seen (text and created_at both None). A media-only parent has text == "".
        if isinstance(reply_to, dict) and reply_to.get("id") and reply_to.get("text") is None and reply_to.get("created_at") is None:
            self._pending_replies.setdefault(str(reply_to["id"]), set()).add(str(record["id"]))

    @property
    def pending_reply_count(self) -> int:
        return sum(len(v) for v in self._pending_replies.values())

    def resolve_missing_reply_contexts(self) -> list[dict[str, Any]]:
        """Backfill reply_to text/created_at for parents seen after the reply. Returns updated records."""
        updated: list[dict[str, Any]] = []
        for parent_id in [pid for pid in self._pending_replies if pid in self.pool]:
            parent = self.pool.get(parent_id) or {}
            for child_id in self._pending_replies.pop(parent_id):
                record = self.sink.get(child_id)
                reply_to = record.get("reply_to") if record else None
                if not isinstance(reply_to, dict):
                    continue
                reply_to["author"] = parent.get("author") or reply_to.get("author")
                reply_to["created_at"] = parent.get("created_at") or None
                reply_to["text"] = parent.get("text", "")
                updated.append(record)
        return updated
