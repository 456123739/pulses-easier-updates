"""
run_harness_local.py — 在本机 Chromium 里跑"页面内驱动"
------------------------------------------------
真机（Windows + WebView2）上的端到端是：`?e2e=1` 打开新界面 →
`web/src/e2e/harness.ts` 在页面里点完 A–H 全部场景 → `api.e2e_report` 写报告。
本机没有 WebView2，但"真前端产物 + 真 Api + 真流程"都能用 bridge_server 补齐：

    Playwright 打开 http://127.0.0.1:<port>/index.html?e2e=1
    window.pywebview  → bridge_server 的 HTTP shim（同一套 api.py）
    e2e.config/fs/can_close → 同一份 Api（PULSES_E2E=1 才注册）

于是驱动代码一行不改就能在本机跑一遍。它不替代真机验证，只保证
"页面内驱动的点击与断言"本身是对的、而且不会卡住（有整体超时 + 报告）。

用法：python scripts/dev/run_harness_local.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEV = ROOT / "scripts" / "dev"
if str(DEV) not in sys.path:                 # 复用 run_ui_e2e 的夹具/启动/复核
    sys.path.insert(0, str(DEV))

import run_ui_e2e as base                    # noqa: E402

HARNESS = DEV / "harness_run.mjs"
TIMEOUT = float(os.environ.get("PULSES_E2E_TIMEOUT", "900"))


def main() -> int:
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        print("缺少 web/dist：先 npm ci && npm run build", file=sys.stderr)
        return 1
    if not (ROOT / "web" / "node_modules" / "playwright").is_dir():
        print("缺少 web/node_modules/playwright：先 npm ci", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pe-harness-"))
    home = tmp / "home"
    home.mkdir()
    env = base.child_env(home)
    procs: list[subprocess.Popen] = []
    try:
        # 两套夹具：基本流程（复制覆盖文件）+ 真下载（HTTP + 5MiB 分片）
        basic = base.run_fixture(DEV / "make_fixture.py", tmp / "basic", env)
        file_port = base.free_port()
        dl = base.run_fixture(DEV / "make_download_fixture.py",
                              tmp / "download", env, "--port", str(file_port))
        procs.append(base.start(
            [sys.executable, str(DEV / "file_server.py"),
             "--dir", dl["serverDir"], "--port", str(file_port),
             "--delay-ms", os.environ.get("PULSES_E2E_DELAY_MS", "60")],
            env, "file_server（真下载）"))

        cfg = {"work": str(tmp), "suite": "all", "basic": basic, "download": dl}
        cfg_path = tmp / "harness-cfg.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        report = tmp / "report.json"

        port = base.free_port()
        procs.append(base.start(
            [sys.executable, str(DEV / "bridge_server.py"),
             "--port", str(port), "--dist", str(ROOT / "web" / "dist"),
             "--db", basic["db"],
             "--e2e-cfg", str(cfg_path), "--e2e-report", str(report)],
            env, "bridge_server（页面内驱动）"))
        if not (base.wait_port(port) and base.wait_port(file_port)):
            print("服务没起来", file=sys.stderr)
            return 1

        url = f"http://127.0.0.1:{port}/index.html?e2e=1"
        base.log(f"Playwright：{url}")
        try:
            proc = subprocess.run(
                ["node", str(HARNESS), url, str(report), str(tmp), str(TIMEOUT)],
                cwd=str(ROOT), timeout=TIMEOUT + 120)
            code = proc.returncode
        except FileNotFoundError:
            base.log("找不到 node：请先装 Node 22 并 npm ci")
            return 127
        except subprocess.TimeoutExpired:
            base.log(f"页面内驱动超时（>{TIMEOUT + 120:.0f}s）")
            return 124

        ok = code == 0
        data = {}
        if report.is_file():
            data = json.loads(report.read_text(encoding="utf-8"))
        else:
            ok = base.check(False, "拿到页面内驱动的报告", str(report))

        # 磁盘为准：页面里的 e2e.fs 只看得到文件内容，这里再独立复核一遍
        ok &= base.verify_pack_disk(Path(basic["pack"]))
        artifacts = data.get("artifacts") or {}
        exported = artifacts.get("exported") or ""
        if exported:
            ok &= base.verify_exported(Path(exported))
        else:
            base.check(False, "拿到开发者导出的路径（artifacts.exported）")
            ok = False
        ok &= base.verify_downloaded(dl)
        return 0 if ok else 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:                            # noqa: BLE001
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
