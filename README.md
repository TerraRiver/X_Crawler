# X Crawler

基于 Playwright 的 X（Twitter）公开时间线数据抓取工具。程序通过浏览器访问目标账号主页并向下滚动，监听并解析后台返回的 GraphQL 接口 JSON 数据，提取目标账号的原创推文、转推和回复（包含被回复的原帖与被引用的原帖内容），按推文 ID 去重后保存为 JSON 文件。

> 本工具仅用于小规模、低频率的数据采集，使用时请遵守当地法律法规与 X 服务条款。

## 功能说明

### 运行逻辑与功能结构图

```mermaid
flowchart TD
    Start["1. 启动脚本并选择抓取范围"] --> Scope1["Posts (原创推文与引用推文)"]
    Start --> Scope2["Reposts (纯转推)"]
    Start --> Scope3["Replies (回复)"]
    Start --> Scope4["全部 (依次抓取上述三个子页面)"]

    Scope1 & Scope2 & Scope3 & Scope4 --> CheckLocal["2. 读取本地历史数据与断点 (data/用户名/)"]
    CheckLocal --> ModeSelect["选择运行模式：断点续传 / 从头扫描 / 清空重抓"]

    ModeSelect --> BrowserInit["3. 启动本机 Chrome 并加载持久化目录 (.chrome_profile)，去除自动化提示条"]
    BrowserInit --> AuthCheck{"4. 检查 auth_token 登录状态"}
    AuthCheck -- "已登录" --> ProfileFetch["5. 访问目标主页并解析 UserByScreenName 接口"]
    AuthCheck -- "未登录且有 Cookie 文件" --> InjectCookie["导入 browser_cookies.json"] --> ProfileFetch
    AuthCheck -- "未登录" --> ManualLogin["打开登录页等待手动登录 (最长 3 分钟)"] --> ProfileFetch

    ProfileFetch --> SaveProfile["终端显示账号总发帖数等信息，写入 profile.json"]
    SaveProfile --> TabLoop["6. 按顺序进入待抓取的子页面 (跳过已完成的子页面)"]

    TabLoop --> CursorCheck{"是否存在未完成游标?"}
    CursorCheck -- "是 (断点续传)" --> InjectCursor["page.route 改写首个时间线请求的 cursor，直接从上次断点位置加载"]
    CursorCheck -- "否" --> ScrollLoop["7. 使用 mouse.wheel 分步向下滚动页面 (伴随鼠标随机移动与小幅回滚)"]
    InjectCursor --> ScrollLoop

    ScrollLoop --> Intercept["8. 监听并解析对应的 GraphQL 时间线接口 JSON"]
    Intercept --> Filter["过滤 promoted 广告，校验推文作者等于 TARGET_USER"]
    Filter --> Extract["提取正文 (含 note_tweet 长文)、被引用推文 (quoted_tweet)、被回复原帖 (reply_to)"]
    Extract --> Persist["9. 新数据先追加写入 tweets.journal.jsonl 并落盘，再更新 state.json 翻页游标"]

    Persist --> RateLimit{"是否遇到 HTTP 429 限流?"}
    RateLimit -- "是" --> WaitRetry["按 x-rate-limit-reset 或 60s/120s/240s 递增等待后重试 (最多 4 次)"] --> ScrollLoop
    RateLimit -- "多次限流" --> KeepCursor["保留当前翻页游标，停止继续抓取"]
    RateLimit -- "否" --> EndCheck{"是否到达页面底部?"}
    EndCheck -- "否" --> ScrollLoop
    EndCheck -- "是 (或到达起始日期)" --> MarkDone["标记当前子页面已完成，进入下一个子页面"]
    EndCheck -- "连续 12 次无新数据" --> KeepCursor2["不标记当前子页面完成，保留翻页游标"]
    MarkDone & KeepCursor & KeepCursor2 --> FinalSave["10. 全部结束或 Ctrl+C 中断时导出 tweets.json 并关闭浏览器"]
```

### 按运行顺序的功能说明

1. **选择抓取范围与读取本地断点**
   - 启动后在终端选择要抓取的内容类型：原创推文（`Posts`）、纯转推（`Reposts`）、回复（`Replies`）或全部。
   - 自动读取 `data/<TARGET_USER>/` 目录下已有的 `tweets.json`（历史推文）和 `state.json`（上次记录的翻页游标与已完成子页面列表）。若本地已有数据，可选择断点续传、从头扫描或清空重抓。
2. **启动本地浏览器（`x_crawler/browser.py`）**
   - 启动本机安装的 Google Chrome，加载 `.chrome_profile/` 目录中的浏览器配置与缓存；去掉 `--enable-automation` 启动参数（不显示“正受到自动测试软件的控制”提示条），并启用 `--disable-blink-features=AutomationControlled`。
   - 若配置目录被正在运行的 Chrome 占用，会直接提示关闭 Chrome；只有在 Chrome 无法启动时才回退到 Playwright Chromium，并使用独立的 `<CHROME_USER_DATA_DIR>_chromium/` 目录（避免版本不一致损坏 Chrome 配置）。
3. **检查与保持登录状态**
   - 检查浏览器会话中是否存在有效的 `auth_token` Cookie：若已存在则直接进入抓取流程；若不存在但项目根目录下有 `browser_cookies.json`，则自动导入该文件中的 Cookie；若均无，则在打开的 Chrome 窗口中跳转登录页，等待用户手动完成登录（最长等待 3 分钟），登录状态自动保存在 `.chrome_profile/` 中供后续复用。
4. **获取并显示目标账号基本信息**
   - 进入目标用户主页时，监听并解析后台 `UserByScreenName` 接口返回的 JSON 数据。
   - 在开始滚动抓取前，于终端显示目标账号的昵称、总发帖数、媒体帖数、关注数、粉丝数以及本地已保存的推文条数，并将账号资料保存至 `data/<TARGET_USER>/profile.json`。
5. **切换子页面与注入断点游标**
   - 按所选范围依次访问 `Posts`（`/user`）、`Reposts`（`/user/reposts`）、`Replies`（`/user/with_replies`）三个子页面；在不同子页面之间切换时，先滚动回页面顶部，再移动鼠标点击对应的顶部标签。
   - 若处于断点续传模式：已抓取到底部的子页面会直接跳过；未抓完的子页面通过 Playwright `page.route` 将该子页面**首个时间线请求**的 `variables.cursor` 改写为上次保存的游标，页面直接从中断位置开始加载。若未能拦截到请求，会在终端给出警告并从顶部扫描，原游标保留不被覆盖。
6. **模拟鼠标滚轮滚动与鼠标移动点击（`x_crawler/stealth.py`）**
   - 在子页面中通过 `page.mouse.wheel()` 分多次向下滚动页面以触发翻页请求，滚动间隙随机移动鼠标位置或小幅向上回滚。
   - 点击页面元素（如切换顶部标签页或点击重试按钮）时，记录上一次鼠标停留坐标，按曲线轨迹分步移动鼠标至目标元素内部后再执行按下与抬起操作。
7. **监听 GraphQL 接口并过滤非目标数据**
   - 通过 Playwright 的 `page.on("response", ...)` 分别监听 `UserOriginalsTimeline`（原创推文）、`UserRepostsTimeline`（纯转推）和 `UserRepliesTimeline`（回复）接口返回的 JSON 数据，不依赖前端 DOM 标签结构。
   - 遍历 JSON 数据时跳过 `entryId` 以 `promoted-` 开头的广告条目，并检查每条推文的作者用户名是否与 `TARGET_USER` 一致，排除 `/with_replies` 页面对话串中由其他用户发布的被回复原帖。
8. **提取推文全文与关联上下文**
   - **超过 280 字的长推文**：优先读取 JSON 中的 `note_tweet` 字段获取未截断全文，并将 HTML 转义字符（如 `&amp;`、`&gt;`）还原为普通字符。
   - **纯转推（`type = "retweet"`）**：从内嵌的 `retweeted_status_result` 字段中提取原作者用户名（`original_author`）与未截断的原帖全文。
   - **被引用的推文（`quoted_tweet`）**：当推文包含引用转发（Quote Post）时，从 `quoted_status_result` 字段中提取被引用原帖的 ID、作者、发布时间与正文。
   - **被回复的原帖（`reply_to`）**：当推文属于回复（`type = "reply"`）时，从接口返回的对话列表中查找被回复的原帖（若原帖在后续翻页中出现也会自动补全），记录其 ID、作者、发布时间与正文。
9. **安全保存、限流等待与结束归档（`x_crawler/storage.py`）**
   - 每收到一页数据，新增（或补全了回复上下文）的推文会先追加写入 `tweets.journal.jsonl` 中断日志并刷新到磁盘，**之后**才把新的翻页游标写入 `state.json`，保证游标位置不会超过已写入磁盘的数据。
   - 每累计 200 条以及运行结束 / `Ctrl+C` 时，将全部数据去重排序后先写入临时文件并落盘，再替换 `tweets.json`，随后清空中断日志；下次启动时若发现残留的中断日志，会自动合并恢复。
   - 当接口返回 HTTP 429（请求频率限制）时，优先按响应头 `x-rate-limit-reset` 等待到限额重置，否则按 60 秒、120 秒、240 秒翻倍增加等待时间（单次最长 20 分钟），然后点击重试按钮继续；连续 4 次仍被限流则保留断点并结束本次运行。
   - 只有**真正到达时间线底部**（接口返回无错误的空时间线或结束标记）、到达 `SINCE_DATE` 或栏目本身为空时，才将子页面记入已完成列表；若因连续 12 次滚动无新数据而停止（可能是网络或接口异常），则**保留断点游标**，下次可继续续传。
   - 若 `tweets.json` 无法解析，程序会直接中止且不改动该文件，避免历史数据被覆盖。

## 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- 本机已安装 Google Chrome（或使用 Playwright 内置 Chromium）
- 可访问 `x.com` 的网络环境

## 安装与配置

```bash
git clone <repository-url>
cd X_Crawler
uv sync
cp .env.example .env
```

如果本机未安装 Google Chrome，可安装 Playwright 内置 Chromium：

```bash
uv run playwright install chromium
```

### 环境变量说明（`.env`）

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `TARGET_USER` | `bhupen07` | 目标 X 账号的用户名（不带 `@`） |
| `DATA_DIR` | `data` | 数据文件的保存根目录 |
| `SINCE_DATE` | 空（不限制） | 可选：抓取起始日期（含当天，格式 `YYYY-MM-DD`，如 `2025-01-01`）；滚动到早于该日期的推文时自动停止当前子页面。格式无效时程序会报错退出 |
| `UNTIL_DATE` | 空（不限制） | 可选：抓取结束日期（含当天，格式 `YYYY-MM-DD`，如 `2025-12-31`）；晚于该日期的推文跳过不保存 |
| `DATE_TZ` | 系统时区 | `SINCE_DATE` / `UNTIL_DATE` 按哪个时区的自然日解释，如 `Asia/Shanghai`、`UTC` |
| `CHROME_PATH` | 自动查找本机 Chrome | 本机 Google Chrome 可执行文件路径（macOS 默认 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`） |
| `CHROME_USER_DATA_DIR` | `.chrome_profile` | 本地 Chrome 用户数据目录，用于保存登录状态 |
| `PROXY` | 空（使用系统网络） | 可选代理地址，例如 `http://127.0.0.1:7890` |
| `HEADLESS` | `false` | 是否使用无头模式运行浏览器（`true` / `false`） |
| `BLOCK_SERVICE_WORKERS` | `true` | 是否禁用页面 Service Worker（保证断点续传的请求改写一定生效） |
| `COOKIE_FILE` | `browser_cookies.json` | 可选的导入 Cookie 文件路径 |

`TARGET_USER` 只能包含字母、数字和下划线（1~15 位），否则程序会报错退出。

## 登录方式

程序启动本机 Google Chrome，并将浏览器数据保存在 `.chrome_profile/` 目录中：

1. **在浏览器窗口中手动登录**：首次运行 `uv run python main.py` 时，如果 `.chrome_profile/` 中没有有效的 `auth_token` Cookie，程序会打开登录页面并等待用户完成登录（最长等待 3 分钟）。登录后程序自动开始抓取，后续运行直接复用该目录中的登录状态。
2. **从文件导入 Cookie**：如果 `.chrome_profile/` 中尚未登录，且项目根目录下存在 `browser_cookies.json`，程序启动时会将文件中的 Cookie 写入浏览器会话。

## 运行与使用

执行以下命令启动程序：

```bash
uv run python main.py            # 或安装后使用：uv run x-crawler
```

命令行参数优先于 `.env`，同时提供 `--types` 与 `--mode` 时可完全非交互运行（适合定时任务）：

```bash
uv run python main.py --user someone --since 2025-01-01 --types posts,replies --mode resume
uv run python main.py --help     # 查看全部参数（--until / --tz / --data-dir / --headless / -v 调试日志）
```

退出码：`0` 全部栏目完成；`1` 有栏目未完成（已保留断点）；`2` 配置 / 登录 / 数据文件错误；`130` 用户中断。

启动后可输入编号选择要抓取的内容类型（多选用空格分隔，直接按回车默认选 `4`）：

| 选项 | 访问地址 | 对应 GraphQL 接口 | 抓取内容 |
| --- | --- | --- | --- |
| `1` | `x.com/<user>`（`Posts`） | `UserOriginalsTimeline` | 原创推文 Posts（含纯原创推文与带评论引用推文） |
| `2` | `x.com/<user>/reposts`（`Reposts`） | `UserRepostsTimeline` | 纯转推 Reposts（直接一键转发他人的推文） |
| `3` | `x.com/<user>/with_replies`（`Replies`） | `UserRepliesTimeline` | 回复 Replies（含被回复原帖与引用推文上下文） |
| `4` 或直接回车 | 依次访问 `Posts`、`Reposts`、`Replies` | 上述全部三个接口 | 依次抓取三个子页面并按 ID 去重合并 |

当页面到底部时，当前子页面抓取结束；连续 12 次滚动没有新数据时也会停止，但该子页面不会被标记为完成。数据保存在 `data/<TARGET_USER>/` 目录下：

```text
data/<TARGET_USER>/
├── tweets.json            # 推文列表（按发布时间从新到旧排序）
├── tweets.journal.jsonl   # 运行中的中断日志（正常结束后自动清空，异常退出后下次启动自动合并）
├── profile.json           # 账号基本信息（总发帖数、媒体帖数、粉丝数、简介等）
├── state.json             # 翻页游标与子页面完成状态
└── backups/<时间戳>/       # “清空重抓”时移入的旧数据
```

如果本地已存在历史数据或未完成的翻页游标，启动时可选择：
1. **断点续传（默认）**：已抓取到底部的子页面直接跳过，未完成的子页面从上次记录的游标位置继续向后抓取。
2. **从头扫描**：从页面顶部最新推文开始向下滚动，抓取结果与本地已有数据合并去重。
3. **清空重抓**：将本地已保存的推文与游标记录移入 `backups/<时间戳>/` 后重新开始抓取。

### 输出 JSON 结构说明

```json
[
  {
    "id": "1880000000000000000",
    "created_at": "2025-01-15T08:30:00Z",
    "author": "bhupen07",
    "type": "reply",
    "original_author": "",
    "original_id": null,
    "original_created_at": null,
    "text": "目标用户发布的回复正文",
    "reply_to": {
      "id": "1879999999999999999",
      "author": "some_user",
      "created_at": "2025-01-15T08:00:00Z",
      "text": "被回复的原帖正文"
    },
    "quoted_tweet": {
      "id": "1878888888888888888",
      "author": "another_user",
      "created_at": "2025-01-14T12:00:00Z",
      "text": "被引用的推文正文"
    }
  }
]
```

字段含义：
- `id`：推文 ID
- `created_at`：UTC 发布时间（ISO 8601 格式，如 `2025-01-15T08:30:00Z`）
- `author`：发布该推文的用户名（`screen_name`）
- `type`：推文类型，取值为 `tweet`（原创推文或带评论引用推文）、`retweet`（不带评论的纯转推）或 `reply`（回复）
- `original_author`：当 `type` 为 `retweet` 时记录被转发原帖的作者用户名，其他类型为空字符串
- `original_id` / `original_created_at`：当 `type` 为 `retweet` 时记录被转发原帖的 ID 与 UTC 发布时间，其他类型为 `null`（旧版本采集的记录可能没有这两个字段）
- `text`：推文全文（当 `type` 为 `retweet` 时，记录被转发原帖的全文）
- `reply_to`：当 `type` 为 `reply` 时，记录被回复原帖的 `id`、`author`、`created_at` 和 `text`；非回复时为 `null`（若原帖为纯图片/视频无文字推文，则 `text` 为空字符串 `""`；若原帖已被删除或不可见，则 `text` 和 `created_at` 为 `null`）
- `quoted_tweet`：当推文引用了其他推文时，记录被引用推文的 `id`、`author`、`created_at` 和 `text`；未引用时为 `null`

## X 个人主页帖子分类与接口说明（记录于 2026-11-06）

> **编写日期：2026 年 11 月 6 日**（基于当日 X 网页端个人主页结构整理，供后续编写或维护 X 采集脚本参考）。

### 1. 账号总发帖数（`total_tweets`）的计算方式

个人主页顶部以及 `UserByScreenName` 接口（`legacy.statuses_count` 或 `tweet_counts.tweets`）显示的**帖子总数**，等于以下三类帖子的数量之和：

$$\text{帖子总数} = \text{Posts（原创推文 + 带评论引用推文）} + \text{Reposts（纯转推）} + \text{Replies（回复）}$$

这三类内容在网页端分别位于三个不同的标签页下，因此单独抓取其中某一个标签页得到的数量会少于账号显示的帖子总数。

---

### 2. 第一层：个人主页顶部的四个标签页（`Posts ⌄` | `Replies` | `Reposts` | `Media`）

X 个人主页顶部共有 4 个标签页，前 3 个分别对应上述三类帖子，第 4 个为附件筛选列表：

| 顶部标签 | 页面 URL | 包含内容 | 对应 `type` 值 | 后台 GraphQL 接口名 | 说明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`Posts ⌄`** | `https://x.com/<user>` | 用户自己发布的纯原创推文、带评论的引用推文（Quote Post） | `"tweet"` | `UserOriginalsTimeline`<br>*(切换为 All 时调用 `UserTweets`)* | 打开个人主页时默认选中二级菜单中的 `Posts ✓`，只返回原创与引用推文，不包含纯转推（`Reposts`）。 |
| **`Replies`** | `https://x.com/<user>/with_replies` | 用户在他人或自己帖子下发布的回复，以及对应的被回复原帖 | `"reply"`<br>*(若对话中包含目标用户自己的主贴则为 `"tweet"`)* | `UserRepliesTimeline`<br>*(旧版接口名为 `UserTweetsAndReplies`)* | 只返回回复所在的对话串，不包含没有回复的普通原创推文和纯转推。接口返回的数据中同时包含其他用户发布的被回复原帖，解析时需核对推文作者是否为目标用户。 |
| **`Reposts`** | `https://x.com/<user>/reposts` | 用户未添加评论、直接转发他人的推文（即 `RT @xxx`） | `"retweet"` | `UserRepostsTimeline` | 单独列出所有的纯转推。接口外层节点的 `full_text` 通常以 `RT @原作者:` 开头且会被截断，未截断的完整原文位于内嵌的 `legacy.retweeted_status_result.result` 字段中。 |
| **`Media`** | `https://x.com/<user>/media` | 该账号所有包含图片、GIF 或视频的推文 | 非独立分类 | `UserMedia` | 对应账号信息中的 `media_tweets`（媒体帖数）。该列表只是筛选出带图片或视频的帖子，其中的帖子已经包含在前面三个标签页中。 |

---

### 3. 第二层：点击 `Posts ⌄` 弹出的下拉菜单（`All` | `Posts ✓` | `Highlights` | `Sort by`）

点击第一个标签 **`Posts ⌄`** 右侧的箭头，会展开四个下拉选项：

1. **`All`（原创推文 + 纯转推）**
   - 将该用户的 **`Posts`（原创推文与带评论引用推文）** 和 **`Reposts`（纯转推）** 合并在同一个列表中按时间展示，不包含 `Replies`（回复）。
   - 后台对应 `UserTweets` 接口。
2. **`Posts ✓`（仅原创推文，默认选中）**
   - 仅展示该用户自己撰写的推文（包含纯原创推文与带评论引用推文），不显示纯转推（`Reposts`）。
   - 后台对应 `UserOriginalsTimeline` 接口，直接访问 `https://x.com/<user>` 时默认加载该视图。
3. **`Highlights`（精选）**
   - 展示账号主人手动添加至精选列表的推文（仅开通 X Premium 的账号可使用该功能）。
   - 后台对应 `UserHighlightsTweets` 接口。
4. **`Sort by >`（排序方式）**
   - 用于选择列表的排序方式：默认按发布时间从新到旧排序（`Latest`），也可切换为按点赞数或互动量排序。按时间排序时接口会返回用于翻页的 `Bottom cursor`。

---

### 4. GraphQL 接口 JSON 中的推文类型判断字段

在解析 GraphQL 接口返回的单个推文对象（`tweet_results.result`）时，可通过以下 JSON 字段区分不同类型并提取内容：

| 类型 | JSON 判断条件 | 内容提取位置 |
| :--- | :--- | :--- |
| **纯原创推文**<br>(`type = "tweet"`) | 不含 `legacy.retweeted_status_result`，且不含 `legacy.in_reply_to_status_id_str` | 若存在 `note_tweet.note_tweet_results.result.text`（超过 280 字的长推文）则读取该字段；否则读取 `legacy.full_text`。 |
| **带评论引用推文**<br>(`type = "tweet"`，含 `quoted_tweet`) | 存在 `quoted_status_result.result`，且不含 `legacy.retweeted_status_result` | 用户自己写的评论从外层 `note_tweet` 或 `legacy.full_text` 读取；被引用的原帖信息从内嵌的 `quoted_status_result.result` 对象中读取。 |
| **纯转推**<br>(`type = "retweet"`) | 存在 `legacy.retweeted_status_result.result` | 外层 `legacy.full_text` 会被截断；需从 `legacy.retweeted_status_result.result` 中读取原作者用户名与原帖完整正文。 |
| **回复**<br>(`type = "reply"`，含 `reply_to`) | 存在 `legacy.in_reply_to_status_id_str`（或 `in_reply_to_user_id_str`） | 被回复原帖的 ID 保存在 `in_reply_to_status_id_str`；被回复原帖的正文可从同一批次或后续翻页响应的对话条目（`TimelineTimelineModule.items`）中按推文 ID 匹配获取。 |
| **广告推文（需过滤）** | 外层条目 `entryId` 以 `"promoted-"` 开头，或包含 `promotedMetadata` 字段 | 属于推广广告，解析时直接跳过。 |
| **受限或已删除推文** | `__typename` 为 `TweetWithVisibilityResults`、`TweetTombstone` 或 `TweetUnavailable` | `TweetWithVisibilityResults` 的实际推文数据包裹在内层 `.tweet` 字段中；`TweetTombstone` 与 `TweetUnavailable` 表示推文已删除或无权限查看，不包含正文数据。 |

## 项目结构

```text
.
├── main.py                # 兼容入口（等价于 x-crawler 命令）
├── x_crawler/
│   ├── cli.py             # 命令行入口、交互选择、整体流程编排
│   ├── config.py          # 配置（.env + 命令行参数）与校验
│   ├── browser.py         # Chrome 启动、Cookie 导入、登录检测
│   ├── crawler.py         # 子页面表、滚动循环、限流等待、page.route 断点续传
│   ├── collector.py       # GraphQL 响应监听、过滤、回复上下文补全、落盘顺序控制
│   ├── parser.py          # GraphQL JSON 数据解析（不依赖浏览器）
│   ├── storage.py         # tweets.json / 中断日志 / state.json / profile.json 读写
│   └── stealth.py         # 鼠标移动、滚轮滚动、点击模拟
├── tests/                 # 单元测试（基于合成 GraphQL 数据，无需浏览器）
├── .env.example           # 环境变量配置示例
├── pyproject.toml         # 项目依赖与工具配置（ruff / pytest）
└── uv.lock                # 依赖版本锁定文件
```

开发与测试：

```bash
uv sync --group dev
uv run pytest              # 或 uv run python -m unittest discover -s tests -t .
uv run ruff check .
```

运行过程中生成的本地文件（已在 `.gitignore` 中忽略）：

```text
.env                  # 本地环境变量配置
.chrome_profile/      # 本地 Chrome 登录状态与缓存目录
browser_cookies.json  # 可选的导入 Cookie 文件
data/                 # 抓取结果保存目录
```

## 常见问题

- **如何切换登录的 X 账号？**
  删除项目根目录下的 `.chrome_profile/` 文件夹（如果存在 `browser_cookies.json` 也一并删除），重新运行 `uv run python main.py`，在弹出的 Chrome 窗口中登录新账号即可。
- **遇到“触发 X 接口频率限制 (429)”提示怎么办？**
  X 对单个账号在短时间内的翻页请求次数有限制（通常以 15 分钟为窗口）。程序会按响应头中的重置时间或按 60 秒、120 秒、240 秒递增等待后自动重试；连续 4 次仍被限流时会保留断点并结束运行，稍后选择“断点续传”即可继续。
- **提示“浏览器配置目录正被其他 Chrome 进程占用”？**
  关闭所有使用 `.chrome_profile/` 的 Chrome 窗口后重试。
- **提示数据文件无法读取并中止？**
  说明 `tweets.json` 已损坏。程序不会覆盖它；请修复或移走该文件后重试（同目录下的 `tweets.journal.jsonl` 可能保留了最近采集的记录，下次启动会自动合并）。
- **如何使用无头模式在后台运行？**
  先在默认有头模式下运行一次并完成登录（使登录状态保存在 `.chrome_profile/` 中），之后在 `.env` 中将 `HEADLESS` 设为 `true`。
