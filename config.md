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

## 4. VPN / 代理配置

因为账号/网络环境的原因，命令行工具和 Cursor 默认不会走浏览器已经在用的系统代理，需要手动配置。

- 本机 VPN 客户端的**系统代理服务地址**：`127.0.0.1:7897`（在 VPN 客户端的"系统代理设置"页面可以看到，如果换了VPN软件或重装，端口号可能会变，以软件里实际显示的为准）

### 4.1 终端里用（临时，仅当前窗口有效）

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
$env:HTTP_PROXY = "http://127.0.0.1:7897"
```

### 4.2 终端里用（永久，所有新开窗口都生效）

```powershell
[System.Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://127.0.0.1:7897", "User")
[System.Environment]::SetEnvironmentVariable("HTTP_PROXY", "http://127.0.0.1:7897", "User")
```
设置后需要**重启电脑**才彻底生效。

### 4.3 Cursor 里用（关键：Cursor 自己不会自动走系统代理）

`Ctrl+Shift+P` → **Preferences: Open User Settings (JSON)** → 在 `settings.json` 里加：

```json
"http.proxy": "http://127.0.0.1:7897",
"http.proxySupport": "on"
```

保存后**完全退出重启 Cursor**（不是 Reload Window）才会生效。这一步解决了"浏览器登录能走代理，但 Cursor 里 Claude Code 登录/授权一直 403 或提示地区不支持"的问题——因为 Cursor 触发的网络请求默认不会用系统代理，必须在 Cursor 自己的设置里显式配置。

## 5. 在 Cursor 里用 Claude Code

装了官方插件 **"Claude Code for VS Code"**（Anthropic 发布）。两种使用方式：

- **终端方式**：在 Cursor 集成终端里 `cd` 到项目目录，设置好代理（见上面4.1/4.2），执行 `claude`
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

网页上"自动分析"按钮需要设置：
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-xxxxx"
```
不设置这个的话，定时搜索、待审核列表功能完全不受影响，只有点"自动分析"会报错提示未设置。

## 7. 已知限制 / 边界（重要，别忘了）

- **LinkedIn 是非官方抓取**（用 `python-jobspy` 绕过登录墙），可能违反其服务条款，有账号/IP被限流封禁的风险，这是已经确认接受的方案，不是bug
- 程序只能**本地跑**，靠电脑开着 + 手动/系统定时任务触发，不是24x7云端服务，关机/程序退出不会补跑错过的当天搜索
- 本程序不会自动写 `JD匹配追踪表.xlsx`，除非手动点了"自动分析"；否则追踪表的更新仍然依赖手动把职位链接发给 Claude 走 `jd-resume-matcher` 技能
