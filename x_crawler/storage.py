"""
Persistence layer for one target user (`data/<user>/`):

- tweets.json           Final export (JSON array sorted newest-first), rewritten atomically & periodically.
- tweets.journal.jsonl  Append-only crash journal. Every new / updated record is appended (and fsynced)
                        *before* the pagination cursor is advanced, so the checkpoint can never get ahead
                        of persisted data. It is merged on load and truncated after each successful export.
- state.json            Pagination cursors + completed tabs (checkpoint).
- profile.json          Target user profile metadata.
- backups/<ts>/         Files moved aside by "restart" mode instead of being deleted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .parser import utc_now_iso

log = logging.getLogger(__name__)

# Legacy checkpoint keys from older versions -> current tab labels
LEGACY_TAB_KEYS = {
    "全部": "回复(Replies)",
    "回复": "回复(Replies)",
    "推文+转推": "原创推文(Posts)",
}


class StorageError(RuntimeError):
    """Raised when existing data cannot be read safely; the run must stop to avoid overwriting it."""


@dataclass
class CheckpointState:
    cursors: dict[str, str] = field(default_factory=dict)
    completed_tabs: set[str] = field(default_factory=set)


def atomic_write_json(target_path: Path, data: Any, indent: int | None = 2) -> None:
    """Write JSON atomically via a temporary file + fsync + rename."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    tmp_path.replace(target_path)


def sort_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda t: (t.get("created_at") or "", str(t.get("id") or "")), reverse=True)


class Storage:
    def __init__(self, data_dir: Path, target_user: str) -> None:
        self.data_dir = data_dir
        self.target_user = target_user
        self.user_dir = data_dir / target_user
        self.tweets_file = self.user_dir / "tweets.json"
        self.journal_file = self.user_dir / "tweets.journal.jsonl"
        self.state_file = self.user_dir / "state.json"
        self.profile_file = self.user_dir / "profile.json"

    # ------------------------------------------------------------------ tweets

    def load_tweets(self) -> dict[str, dict[str, Any]]:
        """Load tweets.json + replay the crash journal + migrate legacy files. Raises StorageError if corrupt."""
        merged: dict[str, dict[str, Any]] = {}

        if self.tweets_file.exists():
            try:
                with open(self.tweets_file, encoding="utf-8") as f:
                    items = json.load(f)
            except (OSError, ValueError) as exc:
                raise StorageError(
                    f"无法读取已有数据文件 {self.tweets_file}：{exc}\n"
                    f"    为避免覆盖历史数据，本次运行已中止。请修复或移走该文件后重试"
                    f"（{self.journal_file.name} 中可能保留了最近采集的记录）。"
                ) from exc
            if not isinstance(items, list):
                raise StorageError(f"数据文件 {self.tweets_file} 格式异常（顶层不是数组），本次运行已中止。")
            self._merge_items(merged, items)

        replayed = self._replay_journal(merged)
        migrated = self._migrate_legacy_files(merged)
        if replayed or migrated:
            self.export(merged)
            if migrated:
                for path in migrated:
                    try:
                        path.unlink()
                    except OSError as exc:
                        log.warning("[!] 旧数据文件 %s 已合并但删除失败：%s", path, exc)
                log.info("[*] 已将旧路径数据文件自动迁移至 %s（共 %d 条）", self.tweets_file, len(merged))
            if replayed:
                log.info("[*] 已从中断日志恢复 %d 条记录并合并至 %s", replayed, self.tweets_file)
        return merged

    @staticmethod
    def _merge_items(merged: dict[str, dict[str, Any]], items: Iterable[Any]) -> int:
        n = 0
        for t in items:
            if isinstance(t, dict) and t.get("id"):
                merged[str(t["id"])] = t
                n += 1
        return n

    def _replay_journal(self, merged: dict[str, dict[str, Any]]) -> int:
        if not self.journal_file.exists():
            return 0
        n = bad = 0
        with open(self.journal_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1  # typically a half-written last line after a hard crash
                    continue
                n += self._merge_items(merged, [rec])
        if bad:
            log.warning("[!] 中断日志 %s 中有 %d 行损坏已跳过", self.journal_file, bad)
        return n

    def _migrate_legacy_files(self, merged: dict[str, dict[str, Any]]) -> list[Path]:
        """Merge `data/<user>.json` / `data/<user>_YYYYMMDD_HHMMSS.json`; returns files safe to delete."""
        if not self.data_dir.exists():
            return []
        legacy_re = re.compile(rf"^{re.escape(self.target_user)}(?:_\d{{8}}_\d{{6}})?\.json$")
        migrated: list[Path] = []
        for path in sorted(p for p in self.data_dir.iterdir() if p.is_file() and legacy_re.fullmatch(p.name)):
            try:
                with open(path, encoding="utf-8") as f:
                    items = json.load(f)
            except (OSError, ValueError) as exc:
                log.warning("[!] 旧数据文件 %s 读取失败，已保留未删除：%s", path, exc)
                continue
            if not isinstance(items, list):
                log.warning("[!] 旧数据文件 %s 格式不是数组，已保留未删除", path)
                continue
            self._merge_items(merged, items)
            migrated.append(path)
        return migrated

    def append_journal(self, records: Iterable[dict[str, Any]]) -> int:
        """Append records to the crash journal and fsync. Raises OSError on failure."""
        lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records]
        if not lines:
            return 0
        self.user_dir.mkdir(parents=True, exist_ok=True)
        with open(self.journal_file, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return len(lines)

    def export(self, tweets: dict[str, dict[str, Any]]) -> int:
        """Atomically write the sorted tweets.json, then truncate the journal (now redundant)."""
        result = sort_records(tweets.values())
        atomic_write_json(self.tweets_file, result)
        if self.journal_file.exists():
            self.journal_file.unlink()
        return len(result)

    # ------------------------------------------------------------------ checkpoint state

    def load_state(self, since_bound: str | None, until_bound: str | None) -> CheckpointState:
        legacy_state = self.data_dir / f".{self.target_user}_state.json"
        if legacy_state.exists() and not self.state_file.exists():
            self.user_dir.mkdir(parents=True, exist_ok=True)
            try:
                legacy_state.rename(self.state_file)
            except OSError as exc:
                log.warning("[!] 迁移旧断点文件 %s 失败：%s", legacy_state, exc)

        if not self.state_file.exists():
            return CheckpointState()
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("顶层不是对象")
        except (OSError, ValueError) as exc:
            log.warning("[!] 断点文件 %s 读取失败，将忽略断点（已采集的数据不受影响）：%s", self.state_file, exc)
            return CheckpointState()

        saved_since, saved_until = data.get("since_date"), data.get("until_date")
        if (saved_since or since_bound or saved_until or until_bound) and (saved_since != since_bound or saved_until != until_bound):
            log.info("[*] 检测到时间范围配置变更，已重置子页面翻页断点状态。")
            return CheckpointState()

        raw_cursors = data.get("cursors")
        raw_completed = data.get("completed_tabs")
        state = CheckpointState(
            cursors={str(k): str(v) for k, v in raw_cursors.items()} if isinstance(raw_cursors, dict) else {},
            completed_tabs={str(x) for x in raw_completed} if isinstance(raw_completed, list) else set(),
        )
        for old_key, new_key in LEGACY_TAB_KEYS.items():
            if old_key in state.cursors:
                state.cursors.setdefault(new_key, state.cursors.pop(old_key))
            if old_key in state.completed_tabs:
                state.completed_tabs.discard(old_key)
                state.completed_tabs.add(new_key)
        return state

    def save_state(self, state: CheckpointState, since_bound: str | None, until_bound: str | None) -> None:
        """Persist checkpoint. Raises OSError on failure (callers decide how loud to be)."""
        atomic_write_json(
            self.state_file,
            {
                "target_user": self.target_user,
                "updated_at": utc_now_iso(),
                "since_date": since_bound,
                "until_date": until_bound,
                "cursors": state.cursors,
                "completed_tabs": sorted(state.completed_tabs),
            },
        )

    # ------------------------------------------------------------------ profile

    def load_profile(self) -> dict[str, Any] | None:
        if not self.profile_file.exists():
            return None
        try:
            with open(self.profile_file, encoding="utf-8") as f:
                prof = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning("[!] 读取 %s 失败：%s", self.profile_file, exc)
            return None
        if isinstance(prof, dict) and str(prof.get("screen_name", "")).lower() == self.target_user.lower():
            return prof
        return None

    def save_profile(self, profile: dict[str, Any]) -> None:
        try:
            atomic_write_json(self.profile_file, {**profile, "updated_at": utc_now_iso()})
        except OSError as exc:
            log.warning("[!] 保存用户资料 %s 失败：%s", self.profile_file, exc)

    # ------------------------------------------------------------------ restart

    def backup_and_reset(self) -> Path | None:
        """Move tweets / journal / state into backups/<timestamp>/ instead of deleting them."""
        files = [p for p in (self.tweets_file, self.journal_file, self.state_file) if p.exists()]
        if not files:
            return None
        backup_dir = self.user_dir / "backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir.mkdir(parents=True, exist_ok=True)
        for p in files:
            shutil.move(str(p), str(backup_dir / p.name))
        return backup_dir
