#!/usr/bin/env python3
"""
report.py - 将分析结果整合为可读性高的 Markdown 报告

用法：
    uv run python report.py [analysis_json_file]
    uv run python report.py data/xi_criticism_推文+转推+回复.json
    uv run python report.py data/analysis_推文+转推+回复.json
"""

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

INPUT_FILE = sys.argv[1] if len(sys.argv) > 1 else "data/xi_criticism_推文+转推+回复.json"
OUT_DIR    = Path("reports")

TYPE_LABEL = {
    "tweet":   "原创",
    "reply":   "回复",
    "retweet": "转发",
}

ATTITUDE_EMOJI = {
    "negative":  "🔴 负面",
    "positive":  "🟢 正面",
    "neutral":   "⚪ 中立",
    "ambiguous": "🟡 模糊",
    None:        "❓ 未标注",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso[:10] if iso else "未知日期"


def fmt_date_full(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso


def to_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return "\n".join(str(v) for v in val if v)
    return str(val)


def blockquote(val) -> str:
    text = to_str(val)
    if not text:
        return ""
    return "\n".join(f"> {l}" for l in text.strip().splitlines())


def load_data(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    items = list(raw.values()) if isinstance(raw, dict) else raw
    items.sort(key=lambda x: x.get("created_at", ""))
    return items


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Report: 批评/污蔑习近平
# ---------------------------------------------------------------------------

def build_xi_criticism(items: list) -> str:
    entries = [
        it for it in items
        if (it.get("analysis") or {}).get("has_criticism")
    ]
    total    = len(items)
    type_cnt = Counter(it.get("type", "tweet") for it in entries)

    lines = [
        "# 批评/污蔑习近平 推文汇总",
        "",
        f"> **分析范围**：共 {total} 条推文，其中 **{len(entries)} 条**含批评/污蔑内容  "
        f"\n> **生成时间**：{now_str()}",
        "",
        "---",
        "",
        "## 统计概览",
        "",
        "| 指标 | 数量 |",
        "|------|------|",
        f"| 批评推文总数 | **{len(entries)}** |",
        f"| 原创推文 | {type_cnt.get('tweet', 0)} |",
        f"| 回复推文 | {type_cnt.get('reply', 0)} |",
        f"| 转发推文 | {type_cnt.get('retweet', 0)} |",
        "",
        "---",
        "",
        "## 详细条目（按时间排序）",
        "",
    ]

    for i, it in enumerate(entries, 1):
        a       = it.get("analysis", {})
        excerpt = to_str(a.get("excerpt"))
        trans   = to_str(a.get("translation"))
        ttype   = TYPE_LABEL.get(it.get("type", "tweet"), "原创")
        author  = it.get("original_author", "")
        date    = fmt_date_full(it.get("created_at", ""))

        header = f"### [{i}] {fmt_date(it.get('created_at', ''))}"
        if author:
            header += f"（转自 @{author}）"

        lines += [
            header,
            "",
            f"**时间**：{date}　　**推文类型**：{ttype}",
            "",
        ]

        lines += [
            "**推文全文**",
            "",
            blockquote(it.get("text", "")),
            "",
        ]

        if excerpt:
            lines += [
                "**批评片段**",
                "",
                blockquote(excerpt),
                "",
            ]

        if trans:
            lines += [
                "**中文翻译**",
                "",
                trans,
                "",
            ]

        lines += ["---", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report: 个人履历
# ---------------------------------------------------------------------------

def build_career(items: list) -> str:
    entries = [
        it for it in items
        if (it.get("analysis") or {}).get("career_info", {}).get("has_info")
    ]
    total    = len(items)
    type_cnt = Counter(it.get("type", "tweet") for it in entries)

    lines = [
        "# 个人履历与职业信息",
        "",
        f"> **分析范围**：共 {total} 条推文，其中 **{len(entries)} 条**含相关信息  "
        f"\n> **生成时间**：{now_str()}",
        "",
        "---",
        "",
        "## 统计概览",
        "",
        "| 指标 | 数量 |",
        "|------|------|",
        f"| 含履历信息的推文数 | **{len(entries)}** |",
        f"| 原创推文 | {type_cnt.get('tweet', 0)} |",
        f"| 回复推文 | {type_cnt.get('reply', 0)} |",
        f"| 转发推文 | {type_cnt.get('retweet', 0)} |",
        "",
        "---",
        "",
        "## 详细条目",
        "",
    ]

    for i, it in enumerate(entries, 1):
        a      = it.get("analysis", {})
        ci     = a.get("career_info", {})
        note   = a.get("analysis_note") or ""
        ttype  = TYPE_LABEL.get(it.get("type", "tweet"), "原创")
        author = it.get("original_author", "")
        date   = fmt_date_full(it.get("created_at", ""))

        header = f"### [{i}] {fmt_date(it.get('created_at', ''))}"
        if author:
            header += f"（转自 @{author}）"

        lines += [
            header,
            "",
            f"**时间**：{date}　　**推文类型**：{ttype}",
            "",
        ]

        if note:
            lines += [f"> ⚠️ {note}", ""]

        lines += [
            "**推文原文**",
            "",
            blockquote(it.get("text", "")),
            "",
            "**发现内容**",
            "",
            to_str(ci.get("findings")) or "_（无）_",
            "",
        ]

        if ci.get("translation"):
            lines += [
                "**中文翻译**",
                "",
                to_str(ci["translation"]),
                "",
            ]

        lines += ["---", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report: 政治组织与活动参与
# ---------------------------------------------------------------------------

def build_political(items: list) -> str:
    entries = [
        it for it in items
        if (it.get("analysis") or {}).get("political_activities", {}).get("has_info")
    ]
    total    = len(items)
    type_cnt = Counter(it.get("type", "tweet") for it in entries)

    lines = [
        "# 政治组织与活动参与",
        "",
        f"> **分析范围**：共 {total} 条推文，其中 **{len(entries)} 条**含相关信息  "
        f"\n> **生成时间**：{now_str()}",
        "",
        "---",
        "",
        "## 统计概览",
        "",
        "| 指标 | 数量 |",
        "|------|------|",
        f"| 含政治活动信息的推文数 | **{len(entries)}** |",
        f"| 原创推文 | {type_cnt.get('tweet', 0)} |",
        f"| 回复推文 | {type_cnt.get('reply', 0)} |",
        f"| 转发推文 | {type_cnt.get('retweet', 0)} |",
        "",
        "---",
        "",
        "## 详细条目",
        "",
    ]

    for i, it in enumerate(entries, 1):
        a      = it.get("analysis", {})
        pa     = a.get("political_activities", {})
        note   = a.get("analysis_note") or ""
        ttype  = TYPE_LABEL.get(it.get("type", "tweet"), "原创")
        author = it.get("original_author", "")
        date   = fmt_date_full(it.get("created_at", ""))

        header = f"### [{i}] {fmt_date(it.get('created_at', ''))}"
        if author:
            header += f"（转自 @{author}）"

        lines += [
            header,
            "",
            f"**时间**：{date}　　**推文类型**：{ttype}",
            "",
        ]

        if note:
            lines += [f"> ⚠️ {note}", ""]

        lines += [
            "**推文原文**",
            "",
            blockquote(it.get("text", "")),
            "",
            "**发现内容**",
            "",
            to_str(pa.get("findings")) or "_（无）_",
            "",
        ]

        if pa.get("translation"):
            lines += [
                "**中文翻译**",
                "",
                to_str(pa["translation"]),
                "",
            ]

        lines += ["---", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report: 涉华内容与对华态度
# ---------------------------------------------------------------------------

def build_china(items: list) -> str:
    entries = [
        it for it in items
        if (it.get("analysis") or {}).get("china_related", {}).get("has_info")
    ]
    total    = len(items)
    type_cnt = Counter(it.get("type", "tweet") for it in entries)
    att_cnt  = Counter(
        (it.get("analysis") or {}).get("china_related", {}).get("attitude_label")
        for it in entries
    )

    lines = [
        "# 涉华内容与对华态度",
        "",
        f"> **分析范围**：共 {total} 条推文，其中 **{len(entries)} 条**含涉华内容  "
        f"\n> **生成时间**：{now_str()}",
        "",
        "---",
        "",
        "## 统计概览",
        "",
        "### 推文类型分布",
        "",
        "| 类型 | 数量 |",
        "|------|------|",
        f"| 原创推文 | {type_cnt.get('tweet', 0)} |",
        f"| 回复推文 | {type_cnt.get('reply', 0)} |",
        f"| 转发推文 | {type_cnt.get('retweet', 0)} |",
        "",
        "### 对华态度分布",
        "",
        "| 态度 | 数量 | 占比 |",
        "|------|------|------|",
    ]

    total_with_att = len(entries)
    for label in ("negative", "neutral", "ambiguous", "positive", None):
        cnt   = att_cnt.get(label, 0)
        pct   = f"{cnt/total_with_att*100:.1f}%" if total_with_att else "0%"
        emoji = ATTITUDE_EMOJI.get(label, "❓")
        lines.append(f"| {emoji} | {cnt} | {pct} |")

    lines += [
        "",
        "---",
        "",
        "## 详细条目（按时间排序）",
        "",
    ]

    for i, it in enumerate(entries, 1):
        a      = it.get("analysis", {})
        cr     = a.get("china_related", {})
        note   = a.get("analysis_note") or ""
        ttype  = TYPE_LABEL.get(it.get("type", "tweet"), "原创")
        author = it.get("original_author", "")
        date   = fmt_date_full(it.get("created_at", ""))
        att    = cr.get("attitude_label")

        header = f"### [{i}] {fmt_date(it.get('created_at', ''))}"
        if author:
            header += f"（转自 @{author}）"

        lines += [
            header,
            "",
            f"**时间**：{date}　　**推文类型**：{ttype}　　**态度**：{ATTITUDE_EMOJI.get(att, '❓')}",
            "",
        ]

        if note:
            lines += [f"> ⚠️ {note}", ""]

        lines += [
            "**推文原文**",
            "",
            blockquote(it.get("text", "")),
            "",
        ]

        if cr.get("explicit_statements"):
            lines += [
                "**明确涉华言论**",
                "",
                blockquote(cr["explicit_statements"]),
                "",
            ]

        if cr.get("implied_attitude"):
            lines += [
                "**态度分析**",
                "",
                to_str(cr["implied_attitude"]),
                "",
            ]

        if cr.get("translation"):
            lines += [
                "**中文翻译**",
                "",
                cr["translation"],
                "",
            ]

        lines += ["---", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not Path(INPUT_FILE).exists():
        print(f"[!] 找不到文件：{INPUT_FILE}")
        sys.exit(1)

    OUT_DIR.mkdir(exist_ok=True)
    items = load_data(INPUT_FILE)
    print(f"[*] 读取 {len(items)} 条分析结果")

    stem = Path(INPUT_FILE).stem

    if stem.startswith("xi_criticism_"):
        content = build_xi_criticism(items)
        out = OUT_DIR / "xi_criticism.md"
        out.write_text(content, encoding="utf-8")
        hits = sum(1 for it in items if (it.get("analysis") or {}).get("has_criticism"))
        print(f"[+] 批评/污蔑习近平 → {out}  (命中 {hits} 条)")
    else:
        reports = [
            ("career",    "个人履历",   build_career(items)),
            ("political", "政治活动",   build_political(items)),
            ("china",     "涉华内容",   build_china(items)),
        ]
        for slug, label, content in reports:
            out = OUT_DIR / f"{slug}.md"
            out.write_text(content, encoding="utf-8")
            print(f"[+] {label} → {out}  ({content.count(chr(10))} 行)")
        print(f"\n[+] 三份报告已生成至 {OUT_DIR}/")


if __name__ == "__main__":
    main()
