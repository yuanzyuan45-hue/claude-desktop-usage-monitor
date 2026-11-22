# Claude Usage Monitor

**See your Claude usage on the Free plan — where the desktop app shows you nothing.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6.svg)](#requirements)
[![Python 3.7+](https://img.shields.io/badge/python-3.7%2B-3776AB.svg)](#requirements)
[![100% local](https://img.shields.io/badge/network-none-success.svg)](#privacy)

An unofficial Claude Desktop extension (MCPB) that reads your 5-hour session and 7-day weekly usage percentage and reset times straight out of the desktop app's own local cache, and keeps them in your system tray.

<p align="center"><img src="icon.png" width="128" alt="Claude Usage Monitor icon"></p>

**[English](#-why-this-exists) · [中文](#-为什么做这个)**

## 🔍 Why this exists

The Claude Desktop app does not show a usage percentage on the **Free plan**. You find out you hit the 5-hour limit when the reply just stops — no gauge, no countdown, no warning.

The app does keep the numbers locally: every reply stream carries `message_limit` events (5-hour / weekly utilization + reset time), and the frontend caches them in its own IndexedDB. That cache has to exist — it is what the app itself would render if it ever showed you. This extension reads it back out and puts it behind a tray icon, no guessing required.

When I looked for an existing tool, everything I found read a different source:

| Approach | What it reads | Why it doesn't cover this case |
|---|---|---|
| Claude Code widgets / statuslines | Claude Code CLI logs or the API with OAuth / API key | You need to be a CLI user, and to hand over a credential |
| Browser extensions | `claude.ai` in the browser, via a web login | Needs a browser session, not the desktop app |
| **This extension** | The desktop app's own local cache | No login, no API key, no network, works on the Free plan |

So: as far as I can tell it is the only Claude **Desktop** extension (MCPB) that surfaces usage on the **Free** plan. If you know of another, open an issue and I will link it here.

## ✨ Features

- 📊 **Session (5-hour) and weekly (7-day) usage percentages.** 100% means 100% — no artificial 99% cap.
- ⏰ **Reset times**, and an honest "already reset" when the window has expired instead of repeating a stale number.
- 🔧 **System tray icon** — hover to read usage, refreshed after every message, zero message quota consumed. Fires a Windows notification above 80%. Right-click to open Claude.
- 🆓 **Works on the Free plan** — the gap the official UI leaves.
- 🔒 **100% local** — no network requests, no API key, no sessionKey, never touches your conversations.
- 🩺 **Self-diagnosing** — parse failures write `error.log` with field names and lengths only (never chat content). Cache entries that are not usage-related are counted, not logged one by one, so the log cannot balloon.
- 💬 **Or just ask in chat** — "check my usage" inside a conversation (costs one message; the tray costs nothing).

## 📦 Install

### Requirements

- **Claude Desktop** on Windows 10/11
- **Python 3.7+** installed and on `PATH` (the extension is launched as `python`; check Settings → Desktop app → Extensions → Advanced settings — if it shows "Python: x.x.x", you're good)

### Steps

1. Download **`claude-usage-monitor.mcpb`** from [Releases](https://github.com/yuanzyuan45-hue/claude-desktop-usage-monitor/releases/latest) (a copy with identical content also sits in the repo root).
2. Claude Desktop → **Settings → Desktop app → Extensions → Manage**.
3. Click **Advanced settings**, scroll to "Extension developer".
4. Click **Install extension**, pick the `.mcpb` file, confirm.
5. **Start a new conversation** (important: an existing conversation will not see a newly installed extension).
6. Type `check my usage` — Claude calls the `claude_usage` tool and prints the numbers.

> The tray icon and the in-chat tool are two independent channels: the tray is bound to its own process and disappears when Claude quits; the tool only runs when a conversation asks for it.

## 🧠 How it works

Claude Desktop's reply stream carries limit events (`message_limit`, with the utilization and reset time of the 5-hour and weekly windows). The frontend caches that state into its local IndexedDB (a react-query cache, under the `messageLimits` field).

This extension is a local MCP server. Once the desktop app launches it, it parses the IndexedDB LevelDB files — LevelDB WAL framing, Snappy decompression, SSTable block index, v8 ValueSerializer structured-clone decoding — takes the newest limit record, and returns it in the conversation.

Scanning is incremental: the WAL is only re-read for newly appended 32 KB-aligned blocks, and a `.ldb` file is skipped when its size is unchanged. Cold start is about 0.4 s; afterwards each poll is in the 2 ms range.

That is all it does — **there is no network request anywhere in it**. It reads files the Claude Desktop app wrote into its own directory, the same class of technique as `ccusage` reading Claude Code's local files.

## 🔒 Privacy and security

- Reads only local files under `%APPDATA%\Claude\IndexedDB\`
- Extracts only the limit fields (utilization, reset time, organization ID) — **no chat content is read or stored**
- No network, no upload, no credentials
- `error.log` records field names, lengths and hex prefixes (to identify parse formats), never plaintext content

## ⚠️ Known limitations

- The numbers reflect what the **desktop app** received; chatting on the web (claude.ai) does not update them.
- The tool is bound at **conversation start** — install the extension, then start a new conversation.
- The tray icon lives only while Claude Desktop runs; it disappears when the app quits.
- Parsing depends on the desktop app's internal cache format, so **a Claude Desktop update can break it** — if that happens, open an Issue with the `error.log` from the extension directory.
- Limit data refreshes after each message; if a window has reset but the cache has not caught up, the tool says "already reset" rather than showing a stale value.

## 🩺 Troubleshooting

If you get no numbers, check in this order:

1. **Send a message first.** Usage is written to the cache only after Claude replies; a fresh install with no conversation has nothing to read yet.
2. **Make sure it is a new conversation.** Old conversations do not load a newly installed extension.
3. **Read `error.log`.** Location:
   - Installed from `.mcpb`: inside the extracted extension directory, i.e.
     `%APPDATA%\Claude\Claude Extensions\<extension-id>\error.log`
   - Run from source: next to `main.py`
   It states whether the cache has "no usage field yet", "window markers missing", or "utilization out of range"; format samples are given as hex prefixes. Cache entries unrelated to usage are counted, not logged one by one, so the log stays small.
4. **Tray missing but the chat tool works?** Then the tray thread died (common in sessions without a desktop or over remote connections) and the chat tool is unaffected. `error.log` will contain a "托盘线程异常退出" stack.
5. `error.log` is capped at 256 KB — it keeps only the tail instead of growing without bound.

## 🛠 Build it yourself

A `.mcpb` is just a zip with `manifest.json`, `main.py` and `icon.png` at its root:

```bash
# from the repo root, where manifest.json / main.py / icon.png live
python -c "import zipfile; z=zipfile.ZipFile('claude-usage-monitor.mcpb','w',zipfile.ZIP_DEFLATED); [z.write(f,f) for f in ('manifest.json','main.py','icon.png')]; z.close()"
```

If you change `main.py`, bump `version` in `manifest.json` too, or the desktop app may not accept the new bundle.

## 🧪 Tests

No third-party dependencies:

```bash
python test_main.py
```

Covers Snappy decompression, WAL framing, SSTable block indexing, limit field extraction, percentage rounding, atomic and concurrent cache writes, and consistency between the manifest and the real files (version, icon, entry_point).

## 📄 License

MIT. Not affiliated with or endorsed by Anthropic. Claude is a trademark of Anthropic.

---

# 中文

**在 Claude 桌面端的免费版上看到你的用量 —— 官方界面不给你看的那部分。**

非官方 Claude Desktop 扩展（MCPB）。从桌面端自己的本地缓存里读出会话（5 小时）与每周（7 天）的用量百分比和重置时间，并把它们放到系统托盘上。

## 🔍 为什么做这个

Claude 桌面端在**免费账户**下不显示用量百分比。你是等到回复突然断掉，才发现 5 小时额度已经用完——没有进度条、没有倒计时、没有预警。

但这些数字本地是有的：每条回复流里都带 `message_limit` 事件（5 小时/每周窗口的利用率与重置时间），前端把它们缓存在自己的 IndexedDB 里。这份缓存必然存在——它就是应用自己该渲染、只是没渲染给你看的东西。本扩展把它读出来，放到托盘图标后面。

我找过现成的工具，它们读的都是别的数据源：

| 做法 | 读什么 | 为什么覆盖不到这个场景 |
|---|---|---|
| Claude Code 小部件 / 状态栏 | Claude Code CLI 日志,或带 OAuth / API Key 调用接口 | 你得是 CLI 用户,而且要把凭证交出去 |
| 浏览器扩展 | 浏览器里的 `claude.ai`,靠网页登录态 | 需要浏览器会话,不是桌面端 |
| **本扩展** | 桌面端自己的本地缓存 | 不需要登录、不需要 API Key、不联网,免费版可用 |

所以据我所知,这是唯一一个面向 Claude **桌面端**、覆盖**免费版**的用量扩展（MCPB）。如果你知道还有别的,开个 Issue,我把它加到这个表里。

## ✨ 功能

- 📊 会话（5 小时）与每周（7 天）用量百分比，满额就是 100%，不做 99% 上限
- ⏰ 限额重置时间，窗口过期会明确说"已重置"而不是继续报旧百分比
- 🔧 **系统托盘图标**：悬停即看用量，每条消息自动刷新，0 额度消耗；超过 80% 弹系统通知；右键图标可打开 Claude
- 🆓 免费账户可用（官方界面不给免费用户显示额度，本扩展补上这块）
- 🔒 纯本地读取：**不联网、不需要 API Key、不需要 sessionKey、不碰你的聊天内容**
- 🩺 解析失败会写 `error.log`（只记字段名和长度，不记聊天内容），坏了能查；与用量无关的缓存条目只计数，`error.log` 不会膨胀
- 💬 在对话里直接问 Claude："查一下我的用量"（注意：这会消耗一条消息额度；平时看托盘即可）

## 📦 安装

### 前置要求

- Windows 10/11 上的 **Claude Desktop** 应用
- **Python 3.7+** 已安装且在 PATH 中（扩展以 `python` 命令启动；可在桌面端 设置 → 桌面应用 → 扩展 → 高级设置 里看到"Python: x.x.x"即为可用）

### 步骤

1. 下载 **`claude-usage-monitor.mcpb`** —— 推荐从 [Releases](https://github.com/yuanzyuan45-hue/claude-desktop-usage-monitor/releases/latest) 直接下载；仓库根目录里也有一份同内容副本
2. 打开 Claude 桌面端 → **设置 → 桌面应用 → 扩展 → 管理**
3. 点击 **高级设置**，拉到底部"扩展开发者"
4. 点击 **安装扩展**，选择下载的 `.mcpb` 文件，确认安装
5. **新建一个对话**（重要：旧对话看不到新装的扩展）
6. 在对话里输入：`查一下我的用量` —— Claude 会调用 `claude_usage` 工具并显示数字

> 托盘图标与对话工具是两条独立通道：托盘挂在自己进程上，Claude 退出即消失；工具只在对话里被调用时执行。

## 🧠 工作原理

Claude 桌面版的聊天回复流中携带限额事件（`message_limit`，含 5 小时/每周窗口的利用率与重置时间）。桌面版前端会把这份状态缓存进本机 IndexedDB（react-query 缓存，`messageLimits` 字段）。

本扩展是一个本地 MCP 服务器：被 Claude 桌面端拉起后，解析 IndexedDB 的 LevelDB 文件（LevelDB WAL 分帧 + Snappy 解压 + SSTable 块索引 + v8 ValueSerializer 结构化克隆解码），取出最新限额数据，在对话中返回。

扫描是增量的：WAL 只读新增的 32KB 对齐块，`.ldb` 文件大小没变就不重扫。冷启动约 0.4s，之后每次 2ms 量级。

仅此而已 —— **没有任何网络请求**。读取的是 Claude 桌面版自己写在自己目录里的缓存文件，与 ccusage 读取 Claude Code 本地文件是同一类原理。

## 🔒 隐私与安全

- 只读取 `%APPDATA%\Claude\IndexedDB\` 下的本地文件
- 只提取限额字段（利用率、重置时间、组织 ID），**不读取、不存储任何聊天内容**
- 不联网、不上传、不申请任何凭证
- `error.log` 只写字段名、长度、十六进制前缀（定位解析格式用），不写任何明文内容

## ⚠️ 已知限制

- 数字反映的是**桌面端**收到的限额数据；网页端（claude.ai 网页）聊天不会更新它
- 工具在**对话开始时绑定**：安装扩展后需要新建对话才能使用
- 托盘图标在 Claude 桌面端运行期间存在，退出应用自动消失
- 解析依赖桌面版内部缓存格式，**Claude 桌面版更新可能导致失效**——遇到问题请提 Issue，并附上扩展目录下的 `error.log`
- 限额数据在每条消息后才刷新；若窗口已重置而缓存未更新，工具会如实显示"已重置"而非旧值

## 🩺 故障排查

读不到数字时按顺序看：

1. **先发一条消息**。用量只在 Claude 回复后写入缓存，全新安装且没聊过天时本来就没有数据。
2. **确认是新建的对话**。旧对话不会加载新装的扩展。
3. **看 `error.log`**。位置：
   - 装的是 `.mcpb`：在扩展解压目录里，即
     `%APPDATA%\Claude\Claude Extensions\<扩展标识>\error.log`
   - 从源码跑：`main.py` 同目录
   里面会写明是"缓存里还没有用量字段""窗口标记缺失"还是"利用率越界"；定位格式用的样本会给十六进制前缀。与用量无关的缓存条目只计数不逐条记录，所以日志不会膨胀。
4. **托盘不出来但对话能用**？那是托盘线程挂了（常见于无桌面会话/远程环境），不影响对话工具。`error.log` 里会有 `托盘线程异常退出` 的堆栈。
5. `error.log` 超过 256KB 会自动只保留尾部，不会无限增长。

## 🛠 自行打包

`.mcpb` 就是一个 zip，根目录放 `manifest.json`、`main.py`、`icon.png` 三个文件：

```bash
# 仓库根目录下（manifest.json / main.py / icon.png 同目录）
python -c "import zipfile; z=zipfile.ZipFile('claude-usage-monitor.mcpb','w',zipfile.ZIP_DEFLATED); [z.write(f,f) for f in ('manifest.json','main.py','icon.png')]; z.close()"
```

改了 `main.py` 记得同时改 `manifest.json` 里的 `version`，否则桌面端可能不认新包。

## 🧪 测试

无第三方依赖，直接跑：

```bash
python test_main.py
```

覆盖 Snappy 解压、WAL 分帧、SSTable 块索引、用量字段提取、百分比取整、缓存原子写、并发写、以及 manifest 与实际文件的一致性（版本号、icon、entry_point）。

## 📄 许可

MIT。本项目与 Anthropic 无关，亦未获得 Anthropic 认可。Claude 是 Anthropic 的商标。
