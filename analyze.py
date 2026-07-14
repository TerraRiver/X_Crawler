#!/usr/bin/env python3
"""
analyze.py - 提取批评/污蔑习近平的推文并翻译，支持断点续传

用法：
    uv run python analyze.py                          # 使用默认输入文件
    uv run python analyze.py data/my_tweets.json     # 指定输入文件
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

try:
    from openai import AsyncOpenAI, RateLimitError, APIStatusError
except ImportError:
    print("[!] 请先运行：uv add openai tqdm")
    sys.exit(1)

try:
    from tqdm import tqdm
except ImportError:
    print("[!] 请先运行：uv add tqdm")
    sys.exit(1)

load_dotenv()

# --- Config from .env ---
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL    = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

CONCURRENCY   = 50
SAVE_INTERVAL = 50
MAX_RETRIES   = 3

INPUT_FILE  = sys.argv[1] if len(sys.argv) > 1 else "data/推文+转推+回复.json"
_stem       = Path(INPUT_FILE).stem
OUTPUT_FILE = f"data/xi_criticism_{_stem}.json"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一名文本分析助手。你的任务是判断一条推文是否包含对中国国家主席习近平的批评或污蔑内容，并对相关内容进行翻译。

【判断标准】
以下任一情形均视为"有批评/污蔑内容"（has_criticism=true）：
- 直接以负面措辞评价习近平本人（如批评其施政、抨击其人格、嘲讽或侮辱）
- 将习近平描绘为独裁者、威权领导人或使用蔑称
- 将习近平与负面事件直接挂钩并加以指责
- 转发推文中原作者对习近平有上述批评内容

【不触发】：
- 仅客观陈述习近平的言行，不含评价
- 批评中国政府/政策但未点名习近平
- 正面或中性提及习近平

【输出规则】
- 输出纯 JSON，不加任何 markdown 标记或说明文字
- has_criticism 为布尔值，不加引号
- 无相关内容时 excerpt 和 translation 填 null
- excerpt 为推文中触发判断的原文片段（逐字引用，不改写）
- translation 为 excerpt 的简体中文翻译"""


def build_prompt(tweet: dict) -> str:
    type_map = {
        "tweet":   "原创推文",
        "reply":   "回复推文",
        "retweet": "转发推文",
    }
    type_label = type_map.get(tweet.get("type", "tweet"), "原创推文")

    lines = [
        f"推文类型：{type_label}",
        f"发布时间：{tweet.get('created_at', '')}",
    ]
    if tweet.get("original_author"):
        lines.append(f"原作者：@{tweet['original_author']}")
    lines.append(f"推文内容：\n{tweet.get('text', '（无文字内容）')}")

    return "\n".join(lines) + """

---
请严格按以下 JSON 格式输出（所有字段必须存在，无内容填 null，布尔值不加引号）：
{
  "has_criticism": false,
  "excerpt": null,
  "translation": null
}"""


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def parse_json(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


# ---------------------------------------------------------------------------
# Single tweet analysis
# ---------------------------------------------------------------------------

async def analyze_one(
    client: AsyncOpenAI,
    tweet: dict,
    semaphore: asyncio.Semaphore,
) -> dict:
    content = "{}"
    async with semaphore:
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": build_prompt(tweet)},
                    ],
                )
                content = resp.choices[0].message.content or "{}"
                analysis = parse_json(content)

                return {
                    **tweet,
                    "analysis":    analysis,
                    "analyzed_at": datetime.now().isoformat(),
                    "error":       None,
                }

            except RateLimitError:
                wait = 5 * (2 ** attempt)
                await asyncio.sleep(wait)
                last_error = "rate_limit"

            except json.JSONDecodeError as e:
                return {
                    **tweet,
                    "analysis":    {"raw_response": content, "parse_error": str(e)},
                    "analyzed_at": datetime.now().isoformat(),
                    "error":       f"JSON 解析失败: {e}",
                }

            except APIStatusError as e:
                last_error = str(e)
                if e.status_code in (400, 401, 403):
                    break
                await asyncio.sleep(2 ** attempt)

            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(2 ** attempt)

    return {
        **tweet,
        "analysis":    None,
        "analyzed_at": datetime.now().isoformat(),
        "error":       f"失败（{MAX_RETRIES} 次重试后）: {last_error}",
    }


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def load_results(path: str) -> dict:
    if Path(path).exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_results(results: dict, path: str, pbar: tqdm | None = None) -> None:
    Path("data").mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    msg = f"[自动保存] {len(results)} 条 → {path}"
    if pbar:
        pbar.write(msg)
    else:
        print(msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    if not DEEPSEEK_API_KEY:
        print("[!] 未设置 DEEPSEEK_API_KEY，请在 .env 中填写")
        sys.exit(1)

    if not Path(INPUT_FILE).exists():
        print(f"[!] 找不到输入文件：{INPUT_FILE}")
        sys.exit(1)

    with open(INPUT_FILE, encoding="utf-8") as f:
        tweets: list[dict] = json.load(f)
    print(f"[*] 读取 {len(tweets)} 条推文：{INPUT_FILE}")

    results = load_results(OUTPUT_FILE)
    already_done = set(results.keys())
    pending = [t for t in tweets if t.get("id") not in already_done]

    print(f"[*] 已分析：{len(already_done)} 条，待分析：{len(pending)} 条")
    print(f"[*] 模型：{DEEPSEEK_MODEL}  并发：{CONCURRENCY}  输出：{OUTPUT_FILE}")

    if not pending:
        print("[+] 全部已分析，无需重复处理。")
        return

    client    = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    pbar = tqdm(total=len(pending), desc="分析进度", unit="条", dynamic_ncols=True)

    async def run_one(tweet: dict) -> None:
        result = await analyze_one(client, tweet, semaphore)
        results[result["id"]] = result
        pbar.update(1)
        if len(results) % SAVE_INTERVAL == 0:
            save_results(results, OUTPUT_FILE, pbar)

    await asyncio.gather(*[run_one(t) for t in pending])
    pbar.close()

    save_results(results, OUTPUT_FILE)

    hits   = sum(1 for r in results.values() if r.get("analysis", {}) and r["analysis"].get("has_criticism"))
    errors = sum(1 for r in results.values() if r.get("error"))
    print(f"\n[+] 完成。共 {len(results)} 条，命中批评/污蔑习近平：{hits} 条，出错：{errors} 条。")
    print(f"[+] 结果：{OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
