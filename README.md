# X Crawler

一个基于 Selenium 的 X（Twitter）公开时间线采集与文本分析工具。它可以抓取指定账号的原创推文、转推和回复，按推文 ID 去重后保存为 JSON；也可以调用兼容 OpenAI SDK 的 DeepSeek API 筛选并翻译涉及习近平的批评内容，再生成 Markdown 报告。

> X 的页面结构和访问策略会持续变化。本项目依赖已登录的浏览器会话，仅适合小规模、低频率的数据采集。请遵守当地法律、X 服务条款以及数据使用规范。

## 功能

- 采集指定用户的原创推文、转推和回复
- 支持连接 Windows/WSL2 中已登录的远程 Chrome，也支持本地 Chrome + Cookie
- 自动识别内容类型、按推文 ID 去重，并每新增 10 条自动保存
- 采集结果以带时间戳的 JSON 文件保存，不覆盖历史数据
- 可选：通过 DeepSeek 批量分析、翻译并断点续传
- 可选：将分析结果整理为 Markdown 报告

## 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Google Chrome 或 Chromium
- 可正常访问 `x.com` 的网络环境

当前远程 Chrome 流程主要面向 Windows + WSL2；如果在 Linux 或 macOS 本机运行，可使用下文的“本地 Chrome + Cookie”方式。

## 安装

```bash
git clone <repository-url>
cd X_Crawler
uv sync
cp .env.example .env
```

编辑 `.env`，至少设置目标账号：

```env
TARGET_USER=bhupen07
```

`.env`、浏览器 Cookie、采集数据和生成报告均已加入 `.gitignore`，不会被 Git 跟踪。

## 登录方式

### 方式一：连接已登录的远程 Chrome（推荐用于 WSL2）

先在 Windows PowerShell 启动独立的 Chrome 调试实例：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir=C:\ChromeDebug
```

在新打开的 Chrome 窗口中登录 `x.com`，然后在 WSL2 中查询 Windows 宿主机地址：

```bash
ip route show default
```

将输出中的网关地址写入 `.env`，例如：

```env
CHROME_REMOTE_URL=172.20.240.1:9222
PROXY=http://172.20.240.1:7897
```

Chrome 调试端口应只对可信网络开放。使用此方式时，脚本复用现有登录会话，不读取 Cookie 文件，运行结束也不会关闭 Chrome。

### 方式二：本地 Chrome + Cookie

1. 在浏览器中登录 `x.com`。
2. 使用浏览器扩展将 `x.com` Cookie 导出为 JSON 数组。
3. 将文件保存到项目根目录并命名为 `browser_cookies.json`。
4. 如需代理，在 `.env` 中配置 `PROXY`；不要设置 `CHROME_REMOTE_URL`。

Cookie 数组中的每项应至少包含 `name` 和 `value`，其他常见字段如 `domain`、`path`、`secure`、`httpOnly` 和 `expirationDate` 会在注入时一并处理。Cookie 等同于登录凭证，请勿提交或分享。

## 抓取数据

运行主程序：

```bash
uv run python main.py
```

根据交互提示选择内容：

| 选项 | 页面 | 采集内容 |
| --- | --- | --- |
| `1` | `x.com/<user>` | 原创推文和转推 |
| `2` | `x.com/<user>/with_replies` | 回复 |
| `3` 或直接回车 | `x.com/<user>/with_replies` | 原创推文、转推和回复 |

多选时用空格分隔，例如 `1 2`。程序连续 10 次滚动没有发现新内容后停止，并将结果写入：

```text
data/<TARGET_USER>_<YYYYMMDD_HHMMSS>.json
```

单条记录示例：

```json
{
  "id": "1234567890",
  "created_at": "2024-01-01T00:00:00.000Z",
  "type": "tweet",
  "original_author": "",
  "text": "推文正文"
}
```

`type` 的取值为 `tweet`、`retweet` 或 `reply`；只有转推会填写 `original_author`。采集器只保存页面中可见的文字，不下载图片、视频、互动数或引用内容。

## 可选：AI 分析

`analyze.py` 使用兼容 OpenAI SDK 的接口，当前提示词用于识别并翻译涉及习近平的批评内容。先在 `.env` 中配置：

```env
DEEPSEEK_API_KEY=your_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

然后传入任意采集结果：

```bash
uv run python analyze.py data/<crawler-output>.json
```

结果写入 `data/xi_criticism_<input-name>.json`。脚本以推文 ID 记录进度，再次运行相同命令时会跳过已完成项目。默认并发为 50，请根据接口限额和费用自行调整 `analyze.py` 中的 `CONCURRENCY`。

注意：推文正文会发送给所配置的第三方模型服务。分析结论可能存在误判，应用于研究或发布前应人工复核。

## 可选：生成报告

对 `analyze.py` 的结果生成批评内容汇总：

```bash
uv run python report.py data/xi_criticism_<input-name>.json
```

报告保存为 `reports/xi_criticism.md`。`report.py` 还兼容包含 `career_info`、`political_activities` 和 `china_related` 字段的扩展分析结果，并会生成 `career.md`、`political.md` 和 `china.md`。

## 项目结构

```text
.
├── main.py          # Selenium 采集入口
├── analyze.py       # DeepSeek 批量分析与断点续传
├── report.py        # Markdown 报告生成器
├── .env.example     # 配置模板
├── pyproject.toml   # 项目与依赖配置
└── uv.lock          # 锁定的依赖版本
```

运行时还会产生以下未跟踪内容：

```text
browser_cookies.json  # 本地浏览器登录 Cookie
data/                 # 采集与分析 JSON
reports/              # 生成的 Markdown 报告
```

## 常见问题

- **连接不上远程 Chrome**：确认 Chrome 使用 `--remote-debugging-port=9222` 启动，并检查 `CHROME_REMOTE_URL` 的 IP、端口和防火墙设置。
- **跳转到登录页**：远程 Chrome 中重新登录，或重新导出 `browser_cookies.json`。
- **页面没有推文**：确认目标账号存在且当前登录账号有权查看；X 页面改版后也可能需要更新 `main.py` 中的 DOM 选择器。
- **ChromeDriver 启动失败**：首次运行可能需要下载匹配的驱动，请检查网络和代理；脚本会依次尝试 webdriver-manager 与 Selenium Manager。
- **结果不完整**：置顶内容、动态加载、访问频率限制以及页面可见范围都会影响采集完整性。

## 许可

仓库当前未附带开源许可证，默认保留全部权利。如需分发或复用，请先补充合适的许可证。
