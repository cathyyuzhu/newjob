#!/bin/bash
set -euo pipefail

# 只在 Claude Code on the web（云端容器）里跑：每次 session 是全新容器，
# 依赖需要重装；本地开发环境不需要这个脚本插手。
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# 异步模式：session 不等这个脚本跑完就先开始，依赖安装在后台进行。
# 权衡：session 秒开，但刚进去的头几十秒如果立刻要跑测试/lint 可能会
# 因为依赖还没装完而失败，等一会儿重试即可。
echo '{"async": true, "asyncTimeout": 300000}'

cd "$CLAUDE_PROJECT_DIR"

# 用虚拟环境装依赖，不碰系统 Python 的包（Debian 容器里系统自带的
# blinker 等包由 apt 管理，缺 RECORD 文件，pip 直接装到系统环境会因为
# 无法卸载它而报错）。
VENV_DIR="$CLAUDE_PROJECT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -q -r requirements.txt

# 让本 session 后续的 bash 命令默认用这个虚拟环境（python/pip 走 .venv）
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"$VENV_DIR\""
    echo "export PATH=\"$VENV_DIR/bin:\$PATH\""
  } >> "$CLAUDE_ENV_FILE"
fi
