"""
run_e2e_driver_local.py — 在本地 Chromium 里先跑一遍"真机驱动"
------------------------------------------------
`app/ui_web/e2e_driver.py` 是给 Windows 真机（真 WebView2）用的：
它只通过 `window.evaluate_js` 碰页面，用 `controller.flow.snapshot()`
读后端状态。本机没有 WebView2，但这两样都能替身出来：

    window.evaluate_js   →  js_proxy.mjs（Playwright 打开同一个 bridge 页面）
    controller.flow/dev  →  bridge 的 flow.state / app.state
    controller.can_close →  bridge 的 /api/close_guard（真控制器在算）

于是驱动代码一行不改就能在本机跑一遍。它不替代真机验证，只保证
"驱动里的点击与断言逻辑"本身是对的（CI 上 Windows 那一步才有意义）。

用法：python scripts/dev/run_e2e_driver_local.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEV = ROOT / "scripts" / "dev"

import run_ui_e2e as base                        # noqa: E402  复用同一套夹具/启动


class ProxyWindow:
    """把 evaluate_js 转给 js_proxy（Playwright 页面）。"""

    def __init__(self, port: int):
        self.port = port

    def evaluate_js(self, code: str):
        body = json.dumps({"code": code}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/eval", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "evaluate_js 失败")
        return data.get("value")

    def destroy(self) -> None:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/close",
                                         data=b"{}")
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:                            # noqa: BLE001, S110
            pass


class RemoteController:
    """把驱动要的那几个控制器能力映射到 bridge 的真实现上。"""

    def __init__(self, port: int):
        self.port = port
        self.flow = _RemoteFlow(self)
        self.dev = _RemoteDev(self)

    def post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def invoke(self, method: str, params: dict | None = None):
        res = self.post("/api/invoke", {"method": method,
                                        "params": params or {}})
        return (res or {}).get("data")

    def can_close(self) -> bool:
        res = self.post("/api/close_guard", {})
        return bool((res or {}).get("data", {}).get("canClose"))


class _RemoteFlow:
    def __init__(self, ctl: "RemoteController"):
        self.ctl = ctl

    def snapshot(self) -> dict:
        return self.ctl.invoke("flow.state") or {}

    def busy(self) -> bool:
        return (self.snapshot().get("phase") or "idle") in (
            "scan", "download", "apply")


class _RemoteDev:
    def __init__(self, ctl: "RemoteController"):
        self.ctl = ctl

    def snapshot(self) -> dict:
        return (self.ctl.invoke("app.state") or {}).get("dev") or {}


def main() -> int:
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        print("缺少 web/dist：先 npm ci && npm run build", file=sys.stderr)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="pe-driver-"))
    home = tmp / "home"
    home.mkdir()
    env = base.child_env(home)
    procs: list[subprocess.Popen] = []
    try:
        basic = base.run_fixture(DEV / "make_fixture.py", tmp / "basic", env)
        file_port = base.free_port()
        dl = base.run_fixture(DEV / "make_download_fixture.py",
                              tmp / "download", env, "--port", str(file_port))
        procs.append(base.start(
            [sys.executable, str(DEV / "file_server.py"),
             "--dir", dl["serverDir"], "--port", str(file_port),
             "--delay-ms", "60"], env, "file_server"))
        port = base.free_port()
        procs.append(base.start(
            [sys.executable, str(DEV / "bridge_server.py"),
             "--port", str(port), "--dist", str(ROOT / "web" / "dist"),
             "--db", basic["db"]], env, "bridge_server"))
        if not (base.wait_port(port) and base.wait_port(file_port)):
            print("服务没起来", file=sys.stderr)
            return 1

        proxy_port = base.free_port()
        # node 侧必须用**真实 HOME**：playwright 的内核装在 ~/.cache/ms-playwright，
        # 换成临时 HOME 就找不到浏览器了（window.evaluate_js 的替身会起不来）。
        proxy_log = (tmp / "js_proxy.log").open("w", encoding="utf-8")
        base.log(f"启动 js_proxy：node {DEV / 'js_proxy.mjs'}")
        procs.append(subprocess.Popen(
            ["node", str(DEV / "js_proxy.mjs"),
             f"http://127.0.0.1:{port}/index.html", str(proxy_port)],
            cwd=str(ROOT), env=dict(os.environ),
            stdout=proxy_log, stderr=subprocess.STDOUT))
        if not base.wait_port(proxy_port, 40):
            proxy_log.flush()
            print("js_proxy 没起来：", file=sys.stderr)
            print((tmp / "js_proxy.log").read_text(encoding="utf-8")[-2000:],
                  file=sys.stderr)
            return 1

        cfg = {"work": str(tmp), "basic": basic, "download": dl,
               "report": str(tmp / "report.json")}
        from app.ui_web import e2e_driver

        e2e_driver.run(ProxyWindow(proxy_port), RemoteController(port),
                       None, cfg)

        data = json.loads(Path(cfg["report"]).read_text(encoding="utf-8"))
        for item in data.get("steps", []):
            print(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}"
                  + (f"  → {item['detail']}" if not item["ok"] else ""), flush=True)
        if not data.get("ok"):
            print("失败：" + str(data.get("error")), file=sys.stderr)
            return 1
        print("\n真机驱动逻辑在本地 Chromium 里全部通过", flush=True)
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:                        # noqa: BLE001
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
