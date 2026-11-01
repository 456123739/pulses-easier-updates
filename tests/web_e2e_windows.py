"""真机端到端（Windows CI）：启动新界面 → 在真页面里跑完整套件 → 查磁盘。

怎么做到"无人值守"：`app/ui_web/e2e_driver.py` 在真实 WebView2 页面里用
`evaluate_js` 点按钮、读文本（和玩家操作同一条路径），结果写 JSON。
这个脚本只负责：搭夹具 → 起服务 → 起进程 → 等报告 → 核对磁盘。

夹具与服务都复用 `scripts/dev/` 下那套（与 `run_ui_e2e.py` 同一份），
避免"浏览器里一套、真机上一套"漂移。非 Windows 直接跳过（返回 0）。

    python tests/web_e2e_windows.py [--exe <打包后的程序>]
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 直接 `python tests/xxx.py` 时 sys.path[0] 是 tests/，要显式把仓库根加回来
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEV = ROOT / "scripts" / "dev"
TIMEOUT = float(os.environ.get("PULSES_E2E_TIMEOUT", "600"))


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def wait_port(port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_fixture(script: Path, out_dir: Path, env: dict,
                  *args: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(script), str(out_dir), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"夹具失败：{script.name}\n"
                           f"{(proc.stderr or proc.stdout)[-3000:]}")
    path = out_dir / "fixture.json"
    if not path.is_file():
        raise RuntimeError(f"夹具没有写 fixture.json：{out_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_exported(path: Path) -> None:
    """磁盘为准：导出的更新包必须真的能被 core 读回来。"""
    from app.core import eapack as eapack_mod

    settings = eapack_mod.read_settings(path) or {}
    manifest = eapack_mod.read_manifest(path) or {}
    hashes = eapack_mod.read_hashes(path) or {}
    assert (settings.get("recommended_strategies") or {}).get("mods") == "替换重名"
    assert "config" in (manifest.get("excluded_folders") or [])
    assert "mods/old.jar" in (hashes.get("files") or {})
    assert "开发者端" in (eapack_mod.read_changelog(path) or "")
    log("导出包可被 app.core.eapack 读回（策略/排除/哈希/更新日志都在）")


def main() -> int:
    if sys.platform != "win32":
        log("非 Windows，跳过真机 e2e")
        return 0

    # 要测的"壳"：显式 --exe 就是打包后的程序，否则跑源码
    exe_arg = ""
    if "--exe" in sys.argv:
        exe_arg = sys.argv[sys.argv.index("--exe") + 1]
    exe_path = Path(exe_arg).resolve() if exe_arg else None
    if exe_path is not None:
        if not exe_path.is_file():
            log(f"找不到打包产物：{exe_path}")
            return 1
    elif not (ROOT / "web" / "dist" / "index.html").is_file():
        log("缺少 web/dist，先构建前端")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pe-e2e-"))
    proc = None
    server = None
    try:
        home = tmp / "home"
        home.mkdir()
        env = {**os.environ,
               "HOME": str(home),
               "USERPROFILE": str(home),
               "PYTHONUTF8": "1",
               "PYTHONIOENCODING": "utf-8"}
        for key in ("PULSES_UI", "PULSES_E2E", "PULSES_E2E_CFG"):
            env.pop(key, None)

        basic = build_fixture(DEV / "make_fixture.py", tmp / "basic", env)
        file_port = free_port()
        dl = build_fixture(DEV / "make_download_fixture.py", tmp / "download",
                           env, "--port", str(file_port))

        server = subprocess.Popen(
            [sys.executable, str(DEV / "file_server.py"),
             "--dir", dl["serverDir"], "--port", str(file_port),
             "--delay-ms", "60"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(file_port):
            log("file server 没起来")
            return 1

        cfg = {"work": str(tmp), "basic": basic, "download": dl,
               "report": str(tmp / "report.json")}
        cfg_path = tmp / "e2e-cfg.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")

        run_env = {**env,
                   "PULSES_UI": "web",
                   "PULSES_E2E": "1",
                   "PULSES_E2E_CFG": str(cfg_path),
                   "PULSES_E2E_REPORT": cfg["report"]}
        if exe_path is not None:
            cmd = [str(exe_path)]
        else:
            exe = Path(sys.executable)
            cmd = [str(exe)] if getattr(sys, "frozen", False) else [
                str(exe), str(ROOT / "main.py")]
        log("启动：" + " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=run_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")

        report = Path(cfg["report"])
        end = time.time() + TIMEOUT
        while time.time() < end:
            if report.is_file():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)

        output = ""
        if proc.poll() is None:
            proc.terminate()
        try:
            output = proc.communicate(timeout=20)[0] or ""
        except Exception:                               # noqa: BLE001
            proc.kill()
        if output.strip():
            log("程序输出：\n" + output[-4000:])

        if not report.is_file():
            log("没有拿到 e2e 报告")
            return 1
        data = json.loads(report.read_text(encoding="utf-8"))
        for item in data.get("steps", []):
            log(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}"
                + (f"  → {item['detail']}" if not item["ok"] else ""))
        if not data.get("ok"):
            log("失败：" + str(data.get("error")))
            return 1

        # 磁盘为准（界面说完成不算）
        pack = Path(basic["pack"])
        assert (pack / "mods" / "old.jar").read_text(encoding="utf-8") == "NEW"
        assert (pack / "mods" / "added.jar").read_text(encoding="utf-8") == "ADDED"
        assert not (pack / "mods" / "gone.jar").exists()
        assert (pack / "config" / "mine.toml").read_text(encoding="utf-8") == "mine = 1\n"
        assert (pack / "config" / "sodium-options.json").read_text(
            encoding="utf-8") == '{"render_distance": 8}\n'
        log("磁盘结果正确（含智能剔除保护）")

        exported = (data.get("artifacts") or {}).get("exported") or ""
        if exported:
            verify_exported(Path(exported))
        aborted = (data.get("artifacts") or {}).get("aborted") or ""
        assert aborted and Path(aborted).is_file()

        try:                                            # 截图留给 CI 产物
            from PIL import ImageGrab
            ImageGrab.grab().save(str(ROOT / "e2e-windows.png"))
            log("已截图 e2e-windows.png")
        except Exception as exc:                        # noqa: BLE001
            log(f"截图失败（不影响结论）：{exc}")
        return 0
    except Exception:                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        if server is not None and server.poll() is None:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
