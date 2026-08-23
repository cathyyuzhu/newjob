# 本地开发环境配置记录

记录在 Windows 电脑上把这个项目跑起来、以及在 Cursor 里用 Claude Code 需要的环境配置，供以后换电脑/重装时参考。

## 1. Node.js（Claude Code CLI 依赖）

- 从 https://nodejs.org 下载安装 **LTS 版本**（`.msi` 安装包，一路默认选项）
- 安装后需要**重新打开一个新的终端窗口**，环境变量才会生效
- 验证：
  ```powershell
  node -v
  npm -v
  ```

## 2. PowerShell 执行策略（npm 安装全局包需要）

Windows 默认禁止 PowerShell 运行脚本，导致 `npm install -g` 报错
`无法加载文件...npm.ps1，因为在此系统上禁止运行脚本`。解决：

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```
（如果这条也报权限错误，改用"以管理员身份运行"的 PowerShell 再执行一次）

## 3. 安装 Claude Code CLI

```powershell
npm install -g @anthropic-ai/claude-code
claude --version
```

## 4. VPN / 代理配置（Cursor 里 Claude Code 登录/授权用）

Cursor 自己触发的网络请求默认不会走系统代理，导致登录 Claude Code 时报错（连接被拒绝，或提示地区不支持）。**有效解决办法**：

- 本机 VPN 客户端的**系统代理服务地址**：`127.0.0.1:7897`（在 VPN 客户端的"系统代理设置"页面可以看到，如果换了VPN软件或重装，端口号可能会变，以软件里实际显示的为准）

`Ctrl+Shift+P` → **Preferences: Open User Settings (JSON)** → 在 `settings.json` 里加：

```json
"http.proxy": "http://127.0.0.1:7897",
"http.proxySupport": "on"
```

保存后**完全退出重启 Cursor**（不是 Reload Window）才会生效。

## 5. 在 Cursor 里用 Claude Code

装了官方插件 **"Claude Code for VS Code"**（Anthropic 发布）。两种使用方式：

- **终端方式**：在 Cursor 集成终端里 `cd` 到项目目录，执行 `claude`
- **插件面板方式**：`Ctrl+Shift+P` 搜 "Claude Code"，选对应命令打开侧边栏/面板

已知问题：
- 插件面板有时会报 `Error loading webview: ... ServiceWorker ...`，跟网络无关，是 Cursor 本身的 webview 渲染 bug，解决办法按优先级尝试：
  1. `Ctrl+Shift+P` → **Developer: Reload Window**
  2. 完全退出重启 Cursor
  3. 还不行就在 `argv.json`（`Ctrl+Shift+P` → **Preferences: Configure Runtime Arguments**）里加 `"disable-hardware-acceleration": true`，重启

## 6. newjob 项目本身的运行环境

```powershell
cd C:\Users\dell\Downloads\newjob
python3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
浏览器打开 `http://127.0.0.1:5050`。

### 自动分析功能（可选，需要 API key）

网页上"自动分析"按钮需要设置 `ANTHROPIC_API_KEY`（或 `DEEPSEEK_API_KEY`，看 `config.json` 里 `llm_provider` 配的是哪家）。

**一定要用 `setx` 持久化写入，不要用 `$env:` 临时设置**：
```powershell
setx DEEPSEEK_API_KEY "sk-xxxxx"
```
`$env:DEEPSEEK_API_KEY = "sk-xxxxx"` 只在当前这一个终端窗口里有效，窗口一关就没了——之前每天定时分析报"未设置环境变量"就是这么踩的坑。`setx` 写进注册表用户环境变量，永久生效，但**只对之后新开的进程生效**。

不设置这个的话，定时搜索、待审核列表功能完全不受影响，只有自动分析/手动点"AI 分析"会报错提示未设置。

**关键提醒：改了环境变量之后，必须完全关掉正在跑的 `python app.py` 进程（连带关闭它所在的终端/IDE 窗口），再重新启动。** Windows 的环境变量只在进程创建那一刻继承一次快照，已经在跑的进程感知不到之后的 `setx`——哪怕 `setx` 已经把值写进注册表，旧进程每天定时分析时用的还是没有 key 的旧环境，会一直失败。判断当前跑着的进程是不是"旧的"：
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId, CommandLine, CreationDate
```
对比 `CreationDate` 和你设置/修改 key 的时间，进程比 key 早创建的话就必须重启。

另外发现过一次奇怪现象：`.venv\Scripts\python.exe app.py` 启动后，会立刻自己额外 spawn 出一个用系统全局 Python（`AppData\Local\Programs\Python\Python312\python.exe`）跑的子进程，两边命令行都是 `app.py`，父子关系明确（子进程的 `ParentProcessId` 就是那个 venv 进程）。代码里没找到任何主动 `subprocess`/自我重启逻辑，原因还没查清楚，目前观察下来子进程能正常继承父进程的环境变量、不影响功能，先记录一下，以后遇到端口冲突或行为诡异可以从这里查起。

## 7. 已知限制 / 边界（重要，别忘了）

- **LinkedIn 是非官方抓取**（用 `python-jobspy` 绕过登录墙），可能违反其服务条款，有账号/IP被限流封禁的风险，这是已经确认接受的方案，不是bug
- 程序只能**本地跑**，靠电脑开着 + 手动/系统定时任务触发，不是24x7云端服务，关机/程序退出不会补跑错过的当天搜索
- 本程序不会自动写 `JD匹配追踪表.xlsx`，除非手动点了"自动分析"；否则追踪表的更新仍然依赖手动把职位链接发给 Claude 走 `jd-resume-matcher` 技能
