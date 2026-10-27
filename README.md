# 把 DeepSeek 智能体接到 QQ，并远程操作 Windows 电脑

一句话概括：**NapCat 把 QQ 消息转到本机，Python 机器人调用 DeepSeek API 思考，再用 PowerShell 操作这台电脑。** 整个过程不需要公网 IP、不需要端口映射、不需要买服务器。

> 这套东西等于把「一个能执行命令的 AI」放进你的 QQ 私聊里。方便，但风险也真实存在。**请先读第 0 节**，再动手。

---

## 0. 先看风险（很重要）

| 风险 | 说明 | 建议 |
| --- | --- | --- |
| QQ 账号被限制 | NapCat 属于第三方 QQ 协议端，可能违反腾讯的用户协议，存在被踢下线、限制登录甚至封号的可能 | **用小号**做机器人，不要用主号；不要群发、不要频繁加好友 |
| 电脑被远程控制 | 机器人以你的 Windows 用户权限运行，能执行任意 PowerShell | 不要用管理员身份运行；解锁口令要长；不用时 `/lock` |
| QQ 被盗 → 电脑被控 | 别人登录了你的 QQ，就能和机器人对话 | 给小号开设备锁；不要把解锁口令发在群里 |
| 提示注入 | 机器人读到的网页/文件里可能藏着恶意指令 | 解锁状态下不要让它处理来路不明的内容 |
| 屏幕内容上传 | 开启 `analyze_screenshots` 时，截图会发给 DeepSeek 分析；读文件的内容也会进入模型上下文 | 屏幕上有密码/隐私时关掉这个选项，或先 `/lock` |
| 密钥泄露 | `config.toml` 里有 DeepSeek Key 和解锁口令 | 不要提交到 Git；不要截图发人 |

如果你只是想要「随时远程看屏幕、动鼠标」，**RustDesk / ToDesk + Tailscale** 比 QQ 机器人更合适（见第 8 节）。QQ 机器人更适合「发一句话，让电脑干一件事」。

---

## 1. 方案长什么样

```
        手机 QQ
           │  消息经腾讯服务器中转（公网，加密）
           ▼
┌───────────────────────── 你的 Windows 电脑 ─────────────────────────┐
│  QQ NT 客户端 + NapCat（协议端）                                      │
│      │  127.0.0.1:3001  HTTP POST 上报消息                            │
│      ▼                                                               │
│  qqbot（本项目，Python）                                              │
│      │  127.0.0.1:3000  调用 NapCat 接口发消息                         │
│      │  HTTPS → api.deepseek.com  调用模型                            │
│      ▼                                                               │
│  DeepSeek 模型 ──工具调用──► PowerShell / 截屏 / 文件 / 关机 …          │
└──────────────────────────────────────────────────────────────────────┘
```

| 组件 | 作用 | 是否必须 |
| --- | --- | --- |
| QQ NT | 你平时用的 QQ 客户端，负责登录 | 必须 |
| NapCat | 把 QQ 的能力翻译成 OneBot 11 协议（HTTP/WS） | 必须 |
| qqbot（本项目） | 收消息、调模型、执行工具、发回复 | 必须 |
| DeepSeek API | 提供对话和工具调用能力 | 必须 |

**为什么不用公网？** NapCat 和 qqbot 都只监听 `127.0.0.1`，外部无法直接连入；你的电脑只主动向外连接腾讯和 DeepSeek。这样既不用路由器端口映射，也不会把电脑暴露在公网扫描里。

---

## 2. 准备清单

- Windows 10 / 11（本项目的工具层是为 Windows 写的）
- Python **3.11+**（安装时勾选 *Add python.exe to PATH*）
- 已安装并能正常登录的 QQ NT（最新版）
- 不需要单独安装 Node.js：NapCat 的 `Shell.Windows.Node.zip` 自带运行时
- **一个备用 QQ 号**（强烈建议，不要用主号）
- DeepSeek API Key：[platform.deepseek.com](https://platform.deepseek.com/) → API Keys（桌面版的登录账号不能直接复用，需要单独创建一个 Key）
- 10–20 分钟

检查 Python：

```powershell
python --version
```

---

## 3. 安装并配置 NapCat

NapCat 的版本更新很快，下面以 **v4.18.x（2026-10）** 为例；界面以你下载到的版本为准。官方文档：<https://napneko.github.io/>，下载页：<https://github.com/NapNeko/NapCatQQ/releases>。

### 3.1 下载

两种方式，任选一种：

**方式 A：普通 Shell（需要先装 QQ）**

1. 打开 [Releases](https://github.com/NapNeko/NapCatQQ/releases)，下载 **`NapCat.Shell.Windows.Node.zip`**（自带 Node，约 124 MB，推荐）。
   如果本机已经装了 Node.js 18+，也可以下载体积更小的 `NapCat.Shell.zip`。
2. 解压到一个固定目录，例如 `D:\NapCat`（路径不要有中文和空格，省事）。

**方式 B：一键安装（不用预先装 QQ）**

1. 下载 `NapCat.Shell.Windows.OneKey.zip`。
2. 解压后运行 `NapCatInstaller.exe`，完成后进入生成的 `NapCat.XXXX.Shell` 目录。
3. 双击 `napcat.bat` 启动。

### 3.2 启动并登录 QQ

方式 A 解压后：

- Windows 11：双击 `launcher.bat`
- Windows 10：双击 `launcher-win10.bat`
- 想直接指定 QQ 号快速登录：`launcher.bat 123456789`

控制台会提示登录，并打印 WebUI 地址，形如：

```
[NapCat] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxxx
```

打开这个地址，用**小号**扫码登录。登录后 WebUI 的 token 会刷新，新的 token 会显示在手机端“自身消息”或控制台里；按提示改一个强密码。

登录成功后，用你的**主号加这个小号为好友**（互相加，或者至少主号能给小号发私聊）。不熟的人给新号发消息可能进“临时会话”甚至被限制，加好友最稳。

### 3.3 收紧 WebUI（重要）

NapCat 的 WebUI 默认监听 `0.0.0.0:6099`，也就是同一局域网的人都能访问。打开 NapCat 目录下的 `config\webui.json`，把：

```json
"host": "0.0.0.0"
```

改成：

```json
"host": "127.0.0.1"
```

然后重启 NapCat。改完后只能本机访问 WebUI，安全很多。

### 3.4 配置 OneBot 网络

打开 WebUI → **网络配置**，新建两个适配器：

**① HTTP 服务端**（让 qqbot 能调用 NapCat 发消息）

| 字段 | 值 |
| --- | --- |
| 名称 | `http-server`（随便） |
| 启用 | ✅ |
| 监听地址 | `127.0.0.1` |
| 监听端口 | `3000` |
| Token | 自己设一串，例如 `napcat-api-7f3a9c`（要和 `config.toml` 的 `api_token` 一致） |
| 消息格式 | `array` |

**② HTTP 客户端**（让 NapCat 把消息推给 qqbot）

| 字段 | 值 |
| --- | --- |
| 名称 | `http-client`（随便） |
| 启用 | ✅ |
| 上报地址 | `http://127.0.0.1:3001/onebot` |
| Token | 自己设一串，例如 `napcat-hook-2b8d1e`（要和 `config.toml` 的 `webhook_token` 一致） |
| 消息格式 | `array` |
| 重连间隔 | 默认即可 |

保存时勾选“保存时启用”。如果版本界面里叫“HTTP 服务器 / HTTP 客户端”，意思一样。

> 说明：`3000` 和 `3001` 都可以改，只要 `config.toml` 里对应改掉即可。两个 Token 建议用不同的随机串。

### 3.5 验证 NapCat

浏览器打开 <http://127.0.0.1:6099/webui> 能进；控制台没有报错；QQ 小号在线。先别关 NapCat。

---

## 4. 配置机器人

### 最快：用配置向导

在项目目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

按提示输入 QQ 号、API Key，两个 Token 和解锁口令可以直接回车自动生成。脚本会写好 `config.toml` 并做一次检查，最后打印需要填进 NapCat 的两组参数。

### 手动：复制模板再改

```powershell
Copy-Item config.example.toml config.toml
notepad config.toml
```

至少改这 5 处：

```toml
[onebot]
api_token = "napcat-api-7f3a9c"       # 与 NapCat HTTP 服务端的 Token 一致
webhook_token = "napcat-hook-2b8d1e" # 与 NapCat HTTP 客户端的 Token 一致

[access]
owner_ids = [你的QQ号]                # 你的主号；不在名单里的消息一律忽略

[llm]
api_key = "sk-..."                    # DeepSeek API Key

[admin]
secret = "一串至少24位的随机字符"      # 远程控制解锁口令，只有你知道
```

生成随机口令（PowerShell）：

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
```

检查配置：

```powershell
python -m qqbot --check
```

看到“配置检查通过”即可。也可以运行 `scripts\check-env.ps1` 做更完整的自检。

---

## 5. 启动，第一次对话

```powershell
python -m qqbot --config config.toml
```

或者双击 `scripts\start-bot.ps1`。正常会看到：

```
QQ 机器人已启动  v1.0.0
监听：127.0.0.1:3001/onebot
NapCat API：http://127.0.0.1:3000
模型：deepseek-flash
白名单：[你的QQ号]
等待 NapCat 推送消息…
```

用你的主号给小号发消息：

| 发送 | 预期 |
| --- | --- |
| `/id` | 返回你的 QQ 号（顺便确认名单配对） |
| `/status` | 运行时间、模型、权限、计数 |
| `你好` | 模型正常回复 |
| `/help` | 命令列表 |
| `/unlock 你的口令` | “已解锁，有效期 30 分钟” |
| `/shot` | 收到当前屏幕截图 |

第一次回复可能需要几秒（模型冷启动）。

---

## 6. 远程控制怎么用

### 6.1 解锁

默认是**锁定**状态：只能聊天，不能碰电脑。发：

```
/unlock 你的口令
```

解锁后 30 分钟内（可在 `ttl_minutes` 改），模型可以调用工具；你也可以用 `/lock` 立即收回。

### 6.2 自然语言示例

解锁后直接说人话：

- “看下我现在屏幕”——自动截屏发给你；开启 `analyze_screenshots` 时模型还能描述画面内容
- “打开记事本”
- “D 盘还剩多少空间？”
- “列出桌面上的文件”
- “把 `C:\Users\你\Desktop\a.txt` 的内容读出来”
- “帮我查一下本机 IP 和显卡型号”
- “用浏览器打开 bilibili.com”

模型会自己决定调用哪个工具；工具执行结果会回传给模型，再由它总结成一句话。

### 6.3 命令速查

| 命令 | 作用 | 需要解锁 |
| --- | --- | --- |
| `/help` | 帮助 | 否 |
| `/id` | 查看自己的 QQ 号 / 群号 | 否 |
| `/status` | 运行状态 | 否 |
| `/reset` | 清空当前会话上下文 | 否 |
| `/unlock <口令>` | 解锁 30 分钟 | 否（口令本身） |
| `/lock` | 立即锁定 | 否 |
| `/shot` | 截屏 | 是 |
| `/sys` | 电脑概况 | 是 |
| `/run <PowerShell>` | 直接执行命令 | 是 |
| `/power lock\|sleep\|restart\|shutdown confirm` | 锁屏/睡眠/重启/关机 | 是 |

`/power` 必须带 `confirm`，例如：

```
/power shutdown confirm
```

### 6.4 能力边界

现在能做的：执行 PowerShell、读写文本文件、列目录、截图、看懂截图（需开启 `analyze_screenshots`）、打开文件/网址、查看系统信息、锁屏/睡眠/重启/关机。

暂时没做：联网搜索、把电脑上的文件发回 QQ、精细的鼠标键盘控制、定时任务。这些都可以在 `tools.py` 里加工具实现；鼠标键盘也可以直接用 PowerShell 的 `SendKeys` / UI Automation 间接完成，但不如远程桌面稳。

### 6.5 安全习惯

- 口令不要发在群里，也不要在聊天记录里反复贴；用完 `/lock`。
- 截图默认会发给 DeepSeek 分析；屏幕上有密码、聊天记录等隐私时，先把 `analyze_screenshots` 设为 `false`，或者不要截图。
- 不要把 `allow_group_admin` 打开。
- 机器人以你的用户权限运行，**不要用管理员身份启动**，更不要做成 Windows 服务（截图会黑屏，权限也过大）。
- `data\audit.jsonl` 记录了每次工具调用，出问题可以回查。
- 如果 QQ 小号出现异常登录提醒，立刻改密码并 `/lock`。

---

## 7. 开机自启 / 后台运行

### 方式 A：启动文件夹（简单，推荐）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
```

它会在当前用户的“启动”文件夹里建一个 `QQBot.lnk`，登录 Windows 后自动用 `pythonw.exe` 后台运行。日志在 `data\bot.log`。

撤销：

```powershell
Remove-Item "$([Environment]::GetFolderPath('Startup'))\QQBot.lnk"
```

### 方式 B：任务计划程序

- 触发器：**登录时**
- 操作：启动程序 `pythonw.exe`，参数 `-m qqbot --config "D:\DeepSeek Harness Set\QQ机器人\config.toml"`，起始于项目目录
- 勾选“不管用户是否登录都要运行”会导致截屏黑屏，**不要勾**

### 注意

截图、桌面自动化都依赖**当前登录的交互式桌面**。所以机器人必须在“用户已登录”的会话里跑；锁屏状态下截图会是黑的，这是 Windows 的限制。

---

## 8. 如果你想要“完整远程桌面”

QQ 机器人擅长“发指令、看结果”，不擅长“流畅地拖鼠标”。需要完整图形控制时，建议组合：

| 工具 | 用途 |
| --- | --- |
| [RustDesk](https://rustdesk.com/)（开源自建）/ ToDesk / 向日葵 | 看屏幕、动鼠标键盘 |
| [Tailscale](https://tailscale.com/) | 把手机和电脑放进同一个虚拟内网，不用公网 IP |
| OpenSSH（Windows 可选功能） | 纯命令行远程 |

机器人可以帮你**启动**这些工具、报告连接码，但真正的画面传输交给它们更稳。

---

## 9. 常见问题

| 现象 | 可能原因 | 解决 |
| --- | --- | --- |
| 启动时报 `401 Unauthorized` | `api_token` 与 NapCat HTTP 服务端不一致 | 两边改成同一个串，重启 NapCat |
| 机器人收不到消息 | HTTP 客户端没启用 / 地址或 Token 不对 | 检查 `http://127.0.0.1:3001/health`；核对上报地址和 `webhook_token` |
| 发消息没反应，日志有“忽略非白名单” | `owner_ids` 没写对 | 日志里会打印 `user=数字`，把这个号填进 `owner_ids`；QQ 个人资料里也能看到自己的号 |
| `模型调用失败：HTTP 401` | DeepSeek Key 错 | 重新复制 Key，注意不要带空格 |
| `HTTP 402` / 余额不足 | 账户欠费 | 去 platform.deepseek.com 充值 |
| `连接 NapCat 失败` | NapCat 没启动 / 端口不对 | 先启动 NapCat，确认 3000 在监听 |
| NapCat 启动时报 QQ 版本不兼容 | NapCat 对 QQ 版本有要求 | 按 NapCat 文档安装指定版本 QQ，或改用 OneKey 包 |
| 截图全黑 | 锁屏 / 以服务方式运行 / 远程会话断开 | 保持用户登录且屏幕解锁；不要用“不管用户是否登录” |
| 中文乱码 | 极少数老程序输出 GBK | 代码已做 GBK 回退；仍乱码可用 `/run chcp 65001` 后重试 |
| 端口被占用 | 机器人开了两个 / 其他程序占用 | 改 `listen_port`，或结束旧进程 |
| 群消息不回 | 没配 `group_ids` / 没 @ / `require_at=true` | 填群号，或在群里 @ 机器人 |
| `/unlock` 提示尝试过多 | 1 分钟内错 5 次 | 等 60 秒 |

日志位置：`data\bot.log`；审计记录：`data\audit.jsonl`；会话：`data\sessions.json`。

---

## 10. 项目结构 & 二次开发

```
QQ机器人/
├─ qqbot/
│  ├─ __main__.py    命令行入口（--check / --config / --log-level）
│  ├─ config.py      TOML 配置加载与校验
│  ├─ onebot.py      OneBot 11 HTTP 客户端 + 消息解析
│  ├─ app.py         HTTP 接收、白名单、命令路由、发送
│  ├─ agent.py       DeepSeek 对话与工具调用循环
│  ├─ tools.py       PowerShell / 截屏 / 文件 / 系统信息
│  ├─ admin.py       解锁口令、有效期、尝试限流
│  ├─ store.py       按会话保存上下文
│  └─ util.py        日志、切分、原子写
├─ tests/            单元测试 + HTTP 端到端测试（26 个）
├─ scripts/          setup / start-bot / install-autostart / check-env
├─ config.example.toml
└─ requirements.txt  （无第三方依赖）
```

- **改人格**：编辑 `qqbot/agent.py` 里的 `_system_prompt()`。
- **加工具**：在 `qqbot/tools.py` 的 `TOOL_SPECS` 里加 schema，在 `ToolBox.execute()` 里加分发。
- **改回复长度**：`app.py` 的 `split_text(..., 1400)`，QQ 单条消息别超过约 4000 字。
- **多用户**：`owner_ids` 可以写多个号；每个号/每个群的上下文是分开的，解锁状态也分开。
- **跑测试**：`python -m unittest discover -s tests -v`

### 为什么不用 `dsh headless`？

DSH 桌面版确实有 `dsh headless "任务"` 模式，可以直接复用它的 skills、MCP 和沙箱。但它每次都要启动 Electron 运行时，单条消息要等几秒；而且无界面模式下的审批策略不好处理——要么全自动放行（等于把整台电脑交给 QQ 消息），要么大量操作被拒绝。

所以这个项目选择**同一个 DeepSeek 模型 + 一套等价的工具循环**：启动快、零第三方依赖、权限边界清楚（解锁才给工具），代码量小也方便你自己审计。如果你确实想复用 DSH 的完整能力，可以把 `qqbot/agent.py` 里的 `DeepSeekClient` 换成一个调用 `dsh --profile headless --json` 的适配器。

---

## 11. 卸载

1. 停止机器人（关窗口，或任务管理器结束 `pythonw.exe`）。
2. 删除启动快捷方式（见第 7 节方式 A）。
3. 删除 NapCat 目录；如果不再需要，卸载对应 QQ。
4. 删除本项目目录（`data/` 里有聊天记录和审计日志，介意的话先删）。

---

## 12. 参考

- NapCat 文档：<https://napneko.github.io/>
- NapCat Releases：<https://github.com/NapNeko/NapCatQQ/releases>
- OneBot 11 协议：<https://github.com/botuniverse/onebot-11>
- DeepSeek API 文档：<https://api-docs.deepseek.com/>
- DeepSeek 开放平台：<https://platform.deepseek.com/>

---

## 附：为什么不用官方 QQ 机器人？

腾讯官方的 QQ 开放平台机器人更合规、不怕封号，但：

- 需要公网回调地址（要服务器/内网穿透），本机直连比较麻烦；
- 只能被动回复 @ 或指令，能力受平台限制；
- 审核、上线流程更长。

如果你在意账号安全、且愿意折腾公网回调，可以走官方路线；如果你只是想在自己的小号上自用，NapCat 这种本机方案最省事——**前提是接受第 0 节的风险**。
