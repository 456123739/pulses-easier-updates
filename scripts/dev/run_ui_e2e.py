"""
run_ui_e2e.py — 一条命令跑完"真前端 + 真 Api + 真流程"的端到端
------------------------------------------------
1. 造夹具（整合包 + 更新包）
2. 起 bridge_server（真 Api）
3. Playwright 在真页面里点完整流程
4. 核对磁盘

CI 里直接 `python scripts/dev/run_ui_e2e.py`；本地也同样能跑（需要
`npm ci && npx playwright install chromium`）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"


def wait_port(port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    if not (WEB / "dist" / "index.html").is_file():
        print("缺少 web/dist：先 npm ci && npm run build", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pe-ui-e2e-"))
    port = int(os.environ.get("PULSES_E2E_PORT", "8231"))
    procs: list[subprocess.Popen] = []
    try:
        fx = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dev" / "make_fixture.py"),
             str(tmp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if fx.returncode != 0:
            print("夹具失败：\n" + (fx.stderr or "")[-3000:], file=sys.stderr)
            return 1
        fixture = json.loads(fx.stdout.strip().splitlines()[-1])
        (tmp / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")

        srv = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "dev" / "bridge_server.py"),
             "--port", str(port), "--dist", str(WEB / "dist"),
             "--db", fixture["db"]],
            cwd=str(ROOT))
        procs.append(srv)
        if not wait_port(port):
            print("bridge server 没起来", file=sys.stderr)
            return 1

        node = "node"
        run = subprocess.run(
            [node, str(WEB / "scripts" / "e2e_ui.mjs"),
             f"http://127.0.0.1:{port}/index.html", str(tmp)],
            cwd=str(WEB))
        if run.returncode != 0:
            shot = tmp / "e2e.png"
            if shot.is_file():
                target = ROOT / "e2e-ui.png"
                target.write_bytes(shot.read_bytes())
                print(f"失败截图：{target}", file=sys.stderr)
        else:
            shot = tmp / "e2e.png"
            if shot.is_file():
                (ROOT / "e2e-ui.png").write_bytes(shot.read_bytes())
        return run.returncode
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
