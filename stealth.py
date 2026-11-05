from __future__ import annotations

import math
import random
import weakref
from datetime import datetime
from playwright.async_api import Locator, Page

# 记录每个页面（标签页）当前鼠标所在的坐标，使用 WeakKeyDictionary 避免已关闭标签页残留引用
_MOUSE_POS: weakref.WeakKeyDictionary[Page, tuple[float, float]] = weakref.WeakKeyDictionary()

# 深度隐藏浏览器自动化指纹的初始化 JS 补丁
STEALTH_INIT_SCRIPT = """
(() => {
    // 1. 彻底抹除 navigator.webdriver 标记
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. 伪造正常的中文/英文语言列表特征
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });

    // 3. 确保 window.chrome 对象完整（模拟正常安装的 Chrome 扩展运行环境）
    if (!window.chrome) {
        window.chrome = {};
    }
    if (!window.chrome.runtime) {
        window.chrome.runtime = {
            PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
            PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' }
        };
    }

    // 4. 修复自动化环境下 Notification 权限查询异常
    if (window.navigator.permissions && window.navigator.permissions.query) {
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
    }
})();
"""


def log_step(msg: str) -> None:
    """带时间戳的步骤日志输出"""
    now = datetime.now().strftime("%H:%M:%S")
    print(f"  [{now}] {msg}", flush=True)


async def human_wait(page: Page, min_ms: int, max_ms: int, reason: str = "") -> None:
    """随机暂停等待，并在终端打印暂停时长及原因"""
    wait_ms = random.randint(min_ms, max_ms)
    wait_sec = wait_ms / 1000.0
    reason_str = f"（{reason}）" if reason else ""
    log_step(f"⏳ 暂停等待 {wait_sec:.2f}s {reason_str}")
    await page.wait_for_timeout(wait_ms)


async def human_reset_gaze(page: Page, reason: str = "视线复位") -> None:
    """将鼠标平滑移回页面中上方阅读准备区并更新 _MOUSE_POS"""
    viewport = page.viewport_size or {"width": 1280, "height": 900}
    vw = viewport["width"]
    vh = viewport["height"]
    reset_x = random.uniform(vw * 0.35, vw * 0.60)
    reset_y = random.uniform(vh * 0.25, vh * 0.45)
    await page.mouse.move(reset_x, reset_y, steps=random.randint(12, 22))
    _MOUSE_POS[page] = (reset_x, reset_y)
    log_step(f"🔄 {reason}：鼠标已自然移回页面中上方阅读区 ({reset_x:.0f}, {reset_y:.0f})")


async def human_idle_reading_gesture(
    page: Page,
    card_locator: Locator,
    text_len: int = 140,
    elapsed_sec: float = 0.0,
) -> None:
    """
    模拟真人在阅读推文/正文时的无意识手部动作 + 合理阅读时长补足：
    1. 78% 概率在阅读时将鼠标滑向卡片的中间偏左文字区域（模拟视线跟随鼠标指读）。
    2. 按字数计算真人最短扫读时间，扣除已耗时并补足剩余阅读停顿。
    """
    try:
        box = await card_locator.bounding_box()
    except Exception:
        box = None

    if box and random.random() < 0.78:
        start_x, start_y = _MOUSE_POS.get(page, (600.0, 400.0))
        read_x = box["x"] + box["width"] * random.uniform(0.18, 0.58)
        read_y = box["y"] + box["height"] * random.uniform(0.25, 0.72)
        steps = random.randint(10, 22)
        for i in range(1, steps + 1):
            t = i / steps
            cur_x = start_x + (read_x - start_x) * t + random.gauss(0, 0.9)
            cur_y = start_y + (read_y - start_y) * t + random.gauss(0, 0.9)
            await page.mouse.move(cur_x, cur_y)
            await page.wait_for_timeout(random.randint(6, 15))
        _MOUSE_POS[page] = (read_x, read_y)

    min_human_read_sec = min(4.0, max(1.8, text_len / random.uniform(130.0, 210.0)))
    remain_sec = min_human_read_sec - elapsed_sec
    if remain_sec > 0.2:
        remain_ms = int(remain_sec * 1000) + random.randint(100, 450)
        await page.wait_for_timeout(remain_ms)


async def human_scroll_to_element(page: Page, locator: Locator, target_desc: str = "") -> None:
    """
    高拟真度模拟真人滚动鼠标滚轮将目标元素滚入“黄金阅读视野”：
    1. 全程使用真实的 page.mouse.wheel() 触发滚轮事件。
    2. 目标位置设定在屏幕中上方（距离顶部 24%~42% 处），误差 <= 70px。
    3. 远/中/近三档变速拨动滚轮 + 60% 概率伴随手部联动微漂移。
    4. 25% 概率模拟“滚过头一点再往回拨一下”的真人视觉定位微调。
    """
    if target_desc:
        log_step(f"📜 正在滚动页面，将【{target_desc}】移入黄金阅读视野...")

    viewport = page.viewport_size or {"width": 1280, "height": 900}
    vh = viewport["height"]
    vw = viewport["width"]

    cur_x, cur_y = _MOUSE_POS.get(
        page, (random.uniform(vw * 0.35, vw * 0.65), random.uniform(vh * 0.3, vh * 0.6))
    )
    if page not in _MOUSE_POS or cur_y > vh * 0.82 or cur_y < vh * 0.12:
        cur_x = random.uniform(vw * 0.35, vw * 0.65)
        cur_y = random.uniform(vh * 0.32, vh * 0.58)
        await page.mouse.move(cur_x, cur_y, steps=random.randint(10, 18))
        _MOUSE_POS[page] = (cur_x, cur_y)

    target_band_y = vh * random.uniform(0.24, 0.42)
    total_scrolled = 0.0
    scroll_steps = 0
    prev_y: float | None = None
    stalled_steps = 0

    for _ in range(25):
        try:
            box = await locator.bounding_box()
        except Exception:
            box = None
        if not box:
            break

        if prev_y is not None:
            if abs(box["y"] - prev_y) < 1.0:
                stalled_steps += 1
                if stalled_steps >= 2:
                    break
            else:
                stalled_steps = 0
        prev_y = box["y"]

        diff_y = box["y"] - target_band_y
        if abs(diff_y) <= 70:
            break

        direction = 1 if diff_y > 0 else -1
        abs_diff = abs(diff_y)

        if abs_diff > 400:
            step_scroll = random.uniform(110, 180)
            pause_ms = random.randint(25, 55)
        elif abs_diff > 150:
            step_scroll = random.uniform(65, 115)
            pause_ms = random.randint(40, 85)
        else:
            step_scroll = min(abs_diff * random.uniform(0.6, 0.9), random.uniform(35, 70))
            pause_ms = random.randint(60, 120)

        if random.random() < 0.6:
            cur_x = max(100.0, min(vw - 100.0, cur_x + random.gauss(0, 1.6)))
            cur_y = max(100.0, min(vh - 100.0, cur_y + random.gauss(0, 1.2)))
            await page.mouse.move(cur_x, cur_y)
            _MOUSE_POS[page] = (cur_x, cur_y)

        delta_y = direction * step_scroll
        await page.mouse.wheel(0, delta_y)
        total_scrolled += delta_y
        scroll_steps += 1
        await page.wait_for_timeout(pause_ms)

    if scroll_steps > 0 and random.random() < 0.25:
        tiny_over = random.choice([-1, 1]) * random.uniform(20, 45)
        await page.mouse.wheel(0, tiny_over)
        await page.wait_for_timeout(random.randint(80, 160))
        await page.mouse.wheel(0, -tiny_over * random.uniform(0.7, 1.1))
        await page.wait_for_timeout(random.randint(60, 130))


async def human_timeline_scroll(
    page: Page,
    target_distance: float = 2200.0,
    fast_mode: bool = False,
) -> float:
    """
    针对 X 无限滚动时间线的高拟真生物力学滚轮下拉：
    1. 彻底替代机械的 window.scrollBy(0, 2500)，全程使用真实的 page.mouse.wheel() 分拨次滚动。
    2. 鼠标若处于屏幕边缘则先平滑移回时间线正文区。
    3. 模拟手指加速-巡航-减速的分拨次拨动滚轮，期间 60% 概率伴随手握鼠标的 1~3px 生理微漂移。
    4. 25% 概率模拟“滚过头往回拨一点”的视觉微调；35% 概率在滚动间隙将鼠标滑向可见推文模拟指读扫视。
    5. fast_mode=True 时（断点续传快速掠过已抓区域）加大单次拨动幅度并跳过指读停顿。
    """
    viewport = page.viewport_size or {"width": 1280, "height": 900}
    vh = viewport["height"]
    vw = viewport["width"]

    # 确保鼠标位于时间线正文区域内
    cur_x, cur_y = _MOUSE_POS.get(
        page, (random.uniform(vw * 0.35, vw * 0.62), random.uniform(vh * 0.30, vh * 0.58))
    )
    if page not in _MOUSE_POS or cur_y > vh * 0.82 or cur_y < vh * 0.15 or cur_x < vw * 0.22 or cur_x > vw * 0.78:
        cur_x = random.uniform(vw * 0.35, vw * 0.62)
        cur_y = random.uniform(vh * 0.32, vh * 0.58)
        await page.mouse.move(cur_x, cur_y, steps=random.randint(10, 18))
        _MOUSE_POS[page] = (cur_x, cur_y)

    # 目标总滚动距离加入随机波动
    goal = target_distance * random.uniform(0.85, 1.15)
    total_scrolled = 0.0
    flick_count = 0

    while total_scrolled < goal:
        remaining = goal - total_scrolled
        if fast_mode:
            step_scroll = random.uniform(280, 460)
            pause_ms = random.randint(18, 40)
        elif remaining > 600:
            step_scroll = random.uniform(140, 240)
            pause_ms = random.randint(30, 68)
        elif remaining > 220:
            step_scroll = random.uniform(85, 145)
            pause_ms = random.randint(45, 95)
        else:
            step_scroll = max(40.0, remaining * random.uniform(0.65, 1.0))
            pause_ms = random.randint(65, 130)

        # 60% 概率伴随手握鼠标拨滚轮时的 1~3 像素手部联动微漂移
        if random.random() < 0.60:
            cur_x = max(120.0, min(vw - 120.0, cur_x + random.gauss(0, 1.6)))
            cur_y = max(120.0, min(vh - 120.0, cur_y + random.gauss(0, 1.2)))
            await page.mouse.move(cur_x, cur_y)
            _MOUSE_POS[page] = (cur_x, cur_y)

        await page.mouse.wheel(0, step_scroll)
        total_scrolled += step_scroll
        flick_count += 1
        await page.wait_for_timeout(pause_ms)

        # 模拟手指换气：每拨动 4~6 下滚轮，有小概率短暂停顿 180~420ms 扫视推文
        if not fast_mode and flick_count % random.randint(4, 6) == 0 and random.random() < 0.35:
            await page.wait_for_timeout(random.randint(180, 420))

    if not fast_mode:
        # 25% 概率模拟“滚过头一点，再轻轻反向往回拨一点”的真人微调
        if flick_count > 0 and random.random() < 0.25:
            tiny_over = random.uniform(25, 55)
            await page.mouse.wheel(0, tiny_over)
            await page.wait_for_timeout(random.randint(80, 160))
            back_delta = -tiny_over * random.uniform(0.75, 1.15)
            await page.mouse.wheel(0, back_delta)
            total_scrolled += tiny_over + back_delta
            await page.wait_for_timeout(random.randint(60, 130))

        # 35% 概率在滚完一屏后，把鼠标轻轻滑向视野内的一条推文正文区模拟视线指读
        if random.random() < 0.35:
            try:
                articles = page.locator("article[data-testid='tweet']")
                cnt = await articles.count()
                if cnt > 0:
                    idx = random.randint(0, min(cnt - 1, 3))
                    box = await articles.nth(idx).bounding_box()
                    if box and 80 < box["y"] < vh - 120:
                        start_x, start_y = _MOUSE_POS.get(page, (cur_x, cur_y))
                        read_x = box["x"] + box["width"] * random.uniform(0.18, 0.58)
                        read_y = box["y"] + min(box["height"], 260) * random.uniform(0.25, 0.72)
                        steps = random.randint(8, 16)
                        for i in range(1, steps + 1):
                            t = i / steps
                            mx = start_x + (read_x - start_x) * t + random.gauss(0, 0.9)
                            my = start_y + (read_y - start_y) * t + random.gauss(0, 0.9)
                            await page.mouse.move(mx, my)
                            await page.wait_for_timeout(random.randint(6, 14))
                        _MOUSE_POS[page] = (read_x, read_y)
            except Exception:
                pass

    return total_scrolled


async def human_move_and_click(page: Page, locator: Locator, action_desc: str = "目标元素") -> None:
    """
    高拟真度模拟真人鼠标轨迹并点击（融合 5 项生物力学特征）：
    1. 连续起点：记住上一次鼠标停留的位置 _MOUSE_POS，杜绝从 (0, 0) 瞬移。
    2. 弧线轨迹（三阶贝塞尔曲线）：双控制点偏移量随距离动态缩放。
    3. 变速运动（Ease-Out 减速瞄准）：t = 1 - math.pow(1 - raw_t, 2.2)。
    4. 生理微颤抖与偶发过冲（Jitter & Overshoot）：随进度衰减的微抖动，长距离(>120px) 30% 概率滑过头再拉回。
    5. 真实按压时长：悬停瞄准（80~260ms）+ 手指按下保持（50~140ms）后松开。
    """
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None

    viewport = page.viewport_size or {"width": 1280, "height": 900}
    if not box or box["y"] < 60 or box["y"] > viewport["height"] - 80:
        await human_scroll_to_element(page, locator, target_desc=action_desc)
        try:
            box = await locator.bounding_box()
        except Exception:
            box = None

    if not box or box["y"] < 0 or box["y"] + box["height"] > viewport["height"]:
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
            await page.wait_for_timeout(300)
            box = await locator.bounding_box()
        except Exception:
            box = None

    if not box or box["y"] < 0 or box["y"] + box["height"] > viewport["height"]:
        log_step(f"🖱️ 点击【{action_desc}】（元素不在可视视口内，执行直接点击）")
        await locator.click()
        return

    # 1. 获取连续起点坐标
    start_x, start_y = _MOUSE_POS.get(
        page, (random.uniform(300, 900), random.uniform(200, 600))
    )

    # 2. 按钮内部 30%~70% 随机落点
    target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

    dist = math.hypot(target_x - start_x, target_y - start_y)
    steps = max(18, min(55, int(dist / 12) + random.randint(-4, 6)))

    log_step(
        f"🖱️ 准备点击【{action_desc}】：鼠标从 ({start_x:.0f}, {start_y:.0f}) 沿贝塞尔弧线滑向 ({target_x:.0f}, {target_y:.0f})，距离 {dist:.0f}px，拆分 {steps} 步"
    )

    # 3. 双控制点三阶贝塞尔曲线
    offset_scale = max(20.0, dist * 0.25)
    ctrl1_x = start_x + (target_x - start_x) * random.uniform(0.2, 0.4) + random.uniform(-offset_scale, offset_scale)
    ctrl1_y = start_y + (target_y - start_y) * random.uniform(0.2, 0.4) + random.uniform(-offset_scale, offset_scale)
    ctrl2_x = start_x + (target_x - start_x) * random.uniform(0.6, 0.8) + random.uniform(-offset_scale, offset_scale)
    ctrl2_y = start_y + (target_y - start_y) * random.uniform(0.6, 0.8) + random.uniform(-offset_scale, offset_scale)

    # 4. 沿贝塞尔曲线分步移动鼠标（Ease-Out 减速 + 衰减生理微颤抖）
    for i in range(1, steps + 1):
        raw_t = i / steps
        t = 1 - math.pow(1 - raw_t, 2.2)

        u = 1 - t
        cur_x = (u**3) * start_x + 3 * (u**2) * t * ctrl1_x + 3 * u * (t**2) * ctrl2_x + (t**3) * target_x
        cur_y = (u**3) * start_y + 3 * (u**2) * t * ctrl1_y + 3 * u * (t**2) * ctrl2_y + (t**3) * target_y

        if i < steps:
            jitter = (1 - raw_t) * 1.8
            cur_x += random.gauss(0, jitter)
            cur_y += random.gauss(0, jitter)

        await page.mouse.move(cur_x, cur_y)
        step_delay = random.uniform(4, 12) + (raw_t * 6)
        await page.wait_for_timeout(step_delay)

    # 5. 长距离 (>120px) 30% 概率触发 Overshoot（滑过头再微调拉回）
    if dist > 120 and random.random() < 0.3:
        overshoot_x = target_x + random.uniform(-6, 6)
        overshoot_y = target_y + random.uniform(-5, 5)
        await page.mouse.move(overshoot_x, overshoot_y, steps=random.randint(2, 4))
        await page.wait_for_timeout(random.randint(30, 80))
        await page.mouse.move(target_x, target_y, steps=random.randint(2, 4))

    # 6. 悬停瞄准（80~260ms）
    hover_ms = random.randint(80, 260)
    await page.wait_for_timeout(hover_ms)

    # 7. 真实手指按压时长（mouse.down -> 50~140ms -> mouse.up）
    hold_ms = random.randint(50, 140)
    await page.mouse.down()
    await page.wait_for_timeout(hold_ms)
    await page.mouse.up()
    log_step(f"   └─ ✅ 已点击【{action_desc}】（悬停瞄准 {hover_ms}ms，手指按压 {hold_ms}ms）")

    _MOUSE_POS[page] = (target_x, target_y)


async def human_type_text(page: Page, locator: Locator, text: str, desc: str = "输入框") -> None:
    """先用贝塞尔曲线点击聚焦输入框，再以 80~250ms 随机间隔逐字敲击输入"""
    await human_move_and_click(page, locator, desc)
    log_step(f"⌨️ 开始在【{desc}】模拟真人逐字输入: {text}")
    for char in text:
        char_delay = random.randint(80, 250)
        await locator.press_sequentially(char, delay=char_delay)
