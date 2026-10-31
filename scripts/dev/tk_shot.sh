#!/usr/bin/env bash
# 在虚拟显示里跑 Tk 版并截图（做 Web 版对照基线用）。
# 用法： scripts/dev/tk_shot.sh <输出.png> [等待秒数] [宽] [高]
set -u
cd "$(dirname "$0")/../.."

OUT="${1:-/tmp/tk.png}"
WAIT="${2:-12}"
W="${3:-1440}"
H="${4:-860}"
DISP="${XVFB_DISPLAY:-:99}"

pgrep -f "Xvfb $DISP" >/dev/null || {
    setsid nohup .tools/xvfb/usr/bin/Xvfb "$DISP" -screen 0 1600x1000x24 \
        -nolisten tcp >/tmp/xvfb.log 2>&1 < /dev/null &
    sleep 2
}

# 窗口居中：x=(screen-w)/2, y=(screen-h)/2 → 1600x1000 上就是 80,70
X=$(( (1600 - W) / 2 ))
Y=$(( (1000 - H) / 2 ))

DISPLAY="$DISP" setsid nohup .venv/bin/python main.py \
    >/tmp/tk_app.log 2>&1 < /dev/null &
APP=$!
sleep "$WAIT"

DISPLAY="$DISP" .venv/bin/python scripts/dev/shot.py "$OUT" "$X" "$Y" "$W" "$H"
kill "$APP" 2>/dev/null
pkill -f "python main.py" 2>/dev/null
echo "app log tail:"; tail -3 /tmp/tk_app.log
