# Claude 用量监控 (Claude Usage Monitor)

**非官方 Claude Desktop 扩展** —— 在 Claude 桌面端里直接查看你的会话(5 小时)与每周(7 天)用量百分比和重置时间。**免费账户可用。**

> An unofficial Claude Desktop extension (MCPB) that shows your 5-hour / weekly usage percentage and reset time inside the Claude Desktop app. Works on the **Free plan**.

![界面](docs/screenshot.png)

## ✨ 功能

- 📊 会话(5 小时)与每周(7 天)用量百分比
- ⏰ 限额重置时间
- 🔧 **系统托盘图标**:悬停即看用量,每条消息自动刷新,0 额度消耗;超过 80% 弹系统通知;右键图标可打开 Claude
- 🆓 免费账户可用(官方界面不给免费用户显示额度,本扩展补上这块)
- 🔒 纯本地读取:**不联网、不需要 API Key、不需要 sessionKey、不碰你的聊天内容**
- 💬 在对话里直接问 Claude:"查一下我的用量"(注意:这会消耗一条消息额度;平时看托盘即可)

## 📦 安装

### 前置要求

- Windows 10/11 上的 **Claude Desktop** 应用
- **Python 3.10+** 已安装且在 PATH 中(扩展以 `python` 命令启动;可在桌面端 设置 → 桌面应用 → 扩展 → 高级设置 里看到"Python: x.x.x"即为可用)

### 步骤

1. 下载本仓库 Release 中的 **`claude-usage-monitor.mcpb`**(或自行打包,见下文)
2. 打开 Claude 桌面端 → **设置 → 桌面应用 → 扩展 → 管理**
3. 点击 **高级设置**,拉到底部"扩展开发者"
4. 点击 **安装扩展**,选择下载的 `.mcpb` 文件,确认安装
5. **新建一个对话**(重要:旧对话看不到新装的扩展)
6. 在对话里输入:`查一下我的用量` —— Claude 会调用 `claude_usage` 工具并显示数字

## 🧠 工作原理

Claude 桌面版的聊天回复流中携带限额事件(`message_limit`,含 5 小时/每周窗口的利用率与重置时间)。桌面版前端会把这份状态缓存进本机 IndexedDB(react-query 缓存,`messageLimits` 字段)。

本扩展是一个本地 MCP 服务器:被 Claude 桌面端拉起后,解析 IndexedDB 的 LevelDB 文件(LevelDB WAL 分帧 + Snappy 解压 + v8 ValueSerializer 结构化克隆解码),取出最新限额数据,在对话中返回。

仅此而已——**没有任何网络请求**。读取的是 Claude 桌面版自己写在自己目录里的缓存文件,与 ccusage 读取 Claude Code 本地文件是同一类原理。

## 🔒 隐私与安全

- 只读取 `%APPDATA%\Claude\IndexedDB\` 下的本地文件
- 只提取限额字段(利用率、重置时间、组织 ID),**不读取、不存储任何聊天内容**
- 不联网、不上传、不申请任何凭证

## ⚠️ 已知限制

- 数字反映的是**桌面端**收到的限额数据;网页端(claude.ai 网页)聊天不会更新它
- 工具在**对话开始时绑定**:安装扩展后需要新建对话才能使用
- 托盘图标在 Claude 桌面端运行期间存在,退出应用自动消失
- 解析依赖桌面版内部缓存格式,**Claude 桌面版更新可能导致失效**——遇到问题请提 Issue
- 限额数据在每条消息后才刷新;若窗口已重置而缓存未更新,工具会如实显示"已重置"而非旧值

## 🛠 自行打包

```bash
# 仓库根目录下(manifest.json 与 main.py 同目录)
python -c "import zipfile; z=zipfile.ZipFile('claude-usage-monitor.mcpb','w',zipfile.ZIP_DEFLATED); z.write('manifest.json','manifest.json'); z.write('main.py','main.py'); z.close()"
```

## 📄 许可

MIT。本项目与 Anthropic 无关,亦未获得 Anthropic 认可。Claude 是 Anthropic 的商标。

---

## English (quick start)

Unofficial Claude Desktop MCPB extension: ask Claude "check my usage" in a **new** conversation and it returns your 5-hour / weekly limit percentage and reset time. Works on the **Free plan**. Requires Claude Desktop on Windows + Python 3.10+ in PATH. 100% local: no network requests, no credentials, reads only Claude Desktop's own IndexedDB cache. Install via Settings → Desktop app → Extensions → Manage → Advanced settings → Install extension. See the Chinese sections above for details.
