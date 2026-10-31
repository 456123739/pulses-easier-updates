#!/usr/bin/env bash
# 开发环境变量（source 使用）：
#   source scripts/dev/env.sh
# 一次性把 python / node / 浏览器 / 虚拟显示 都指到位，避免每次找工具。
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export REPO_ROOT

# ---- Python ----
export VENV="$REPO_ROOT/.venv"
export PY="$VENV/bin/python"
export PIP="$VENV/bin/pip"
export PATH="$VENV/bin:$PATH"
# 依赖统一从清华镜像装：官方 PyPI 在这台机器上会出现 sha256 不一致（代理改写）
export PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"

# ---- Node ----
# 这台机器 NODE_ENV=production，且 npm 配置 omit=dev，装依赖必须带 --include=dev
export NPM_INSTALL="npm install --include=dev --no-audit --no-fund"
export WEB_DIR="$REPO_ROOT/web"

# ---- 浏览器 / 截图 ----
export PLAYWRIGHT_BROWSERS_PATH="/home/admin/.cache/ms-playwright"
export CHROMIUM="/home/admin/.cache/ms-playwright/chromium-1194/chrome-linux/chrome"
export CHROMIUM_HEADLESS="/home/admin/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell"

# ---- 虚拟显示（Tk 截图用）----
export XVFB_BIN="$REPO_ROOT/.tools/xvfb/usr/bin/Xvfb"
export XVFB_DISPLAY="${XVFB_DISPLAY:-:99}"

# ---- 参考实现 ----
export REF_SWAP="/home/admin/111/pulses-swap"          # pywebview + React 现成样板
export REF_PCL="/home/admin/NS/pcl_analysis"           # PCL2 源码（动画参考）
export PYWEBVIEW_SRC="$REPO_ROOT/.tools/pywebview-6.2.1-webview"

# ---- 常用命令 ----
# 跑单元测试：  pytest_all
pytest_all() { ( cd "$REPO_ROOT" && "$PY" -m pytest tests -q "$@" ); }
# 起虚拟显示：  xvfb_start ; 停：xvfb_stop
xvfb_start() {
    if ! pgrep -f "Xvfb $XVFB_DISPLAY" >/dev/null 2>&1; then
        "$XVFB_BIN" "$XVFB_DISPLAY" -screen 0 1440x900x24 -nolisten tcp \
            >/tmp/xvfb.log 2>&1 &
        sleep 1.2
    fi
    export DISPLAY="$XVFB_DISPLAY"
    echo "DISPLAY=$DISPLAY"
}
xvfb_stop() { pkill -f "Xvfb $XVFB_DISPLAY" 2>/dev/null || true; }

echo "[env] python=$($PY -V 2>&1) node=$(node -v) display=${XVFB_DISPLAY}"
