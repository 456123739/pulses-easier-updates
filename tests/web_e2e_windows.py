"""真机端到端（Windows CI）：起新界面 → 页面内驱动自己跑完 → 查磁盘。

驱动在页面里（`web/src/e2e/harness.ts`，只有 `?e2e=1` 才会被动态 import）：
它点的是真按钮，后端动作全走 `invoke`，跑完用 `api.e2e_report` 回传结果，
Python 侧只负责写报告、关窗。**不再有 Python 线程 evaluate_js**——那条路在
Windows/WinForms 上会卡住整个进程（见 `app/ui_web/api.py` 与 harness 顶部说明）。

所以这个脚本只做四件事：搭夹具 → 起服务/起进程 → 等报告 → 按 steps 打
PASS/FAIL 并复核磁盘与导出的 `.eapack`。等报告期间每 15 秒打一次心跳，
超时或失败时把 `web-start.log`（或 `PULSES_WEB_LOG` 指定的文件）打出来。

夹具与服务都复用 `scripts/dev/` 下那套（与 `run_ui_e2e.py` 同一份），
避免"浏览器里一套、真机上一套"漂移。非 Windows 直接跳过（返回 0）。

    python tests/web_e2e_windows.py [--exe <打包后的程序>]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 直接 `python tests/xxx.py` 时 sys.path[0] 是 tests/，要显式把仓库根加回来
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEV = ROOT / "scripts" / "dev"
if str(DEV) not in sys.path:                 # 复用夹具/复核（与浏览器套件同一份断言）
    sys.path.insert(0, str(DEV))

import run_ui_e2e as base                    # noqa: E402

log = base.log
check = base.check
TIMEOUT = float(os.environ.get("PULSES_E2E_TIMEOUT", "900"))
HEARTBEAT = 15.0


def local_appdata_log() -> Path:
    """新界面固定落盘的启动日志（host._log_path 同一规则）。"""
    base_dir = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base_dir) / "Pulses Easier" / "web-start.log"


def dump_logs(log_path: Path) -> None:
    """超时/失败时打印现场：日志文件是唯一能看出卡在哪儿的证据。"""
    seen: set[str] = set()
    for path in (log_path, local_appdata_log()):
        try:
            key = str(path.resolve())
        except OSError:
            continue
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        log(f"--- {path}（最后 80 行）---")
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            log(f"读不了：{exc}")
            continue
        for line in lines[-80:]:
            print("  " + line, flush=True)


def pump_output(proc: subprocess.Popen, sink: list[str]) -> None:
    """把子进程输出实时打出来：卡住时至少知道走到哪一步了。"""
    stream = proc.stdout
    if stream is None:
        return
    try:
        for line in stream:
            text = line.rstrip()
            if not text:
                continue
            sink.append(text)
            if len(sink) > 400:
                del sink[:200]
            print(f"[app] {text}", flush=True)
    except Exception:                                   # noqa: BLE001
        pass


def verify_disk(basic: dict, dl: dict, report: dict) -> bool:
    """磁盘为准（界面说完成不算），导出的 .eapack 只有 Python 读得懂。"""
    ok = True
    ok &= base.verify_pack_disk(Path(basic["pack"]))
    artifacts = report.get("artifacts") or {}
    exported = str(artifacts.get("exported") or "")
    if exported and Path(exported).is_file():
        ok &= base.verify_exported(Path(exported))
    else:
        ok = check(False, "导出包存在", exported) and ok
    ok &= base.verify_downloaded(dl)
    return ok


def main() -> int:
    if sys.platform != "win32":
        log("非 Windows，跳过真机 e2e")
        return 0

    # 要测的"壳"：显式 --exe 就是打包后的程序，否则跑源码
    exe_arg = ""
    if "--exe" in sys.argv and sys.argv.index("--exe") + 1 < len(sys.argv):
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
    proc: subprocess.Popen | None = None
    server: subprocess.Popen | None = None
    log_path = tmp / "web-start.log"
    try:
        home = tmp / "home"
        home.mkdir()
        env = {**os.environ,
               "HOME": str(home),
               "USERPROFILE": str(home),
               "PYTHONUTF8": "1",
               "PYTHONIOENCODING": "utf-8"}
        for key in ("PULSES_UI", "PULSES_E2E", "PULSES_E2E_CFG",
                    "PULSES_E2E_REPORT", "PULSES_E2E_PACK", "PULSES_E2E_UPDATE"):
            env.pop(key, None)

        basic = base.run_fixture(DEV / "make_fixture.py", tmp / "basic", env)
        file_port = base.free_port()
        dl = base.run_fixture(DEV / "make_download_fixture.py",
                              tmp / "download", env, "--port", str(file_port))

        server = subprocess.Popen(
            [sys.executable, str(DEV / "file_server.py"),
             "--dir", dl["serverDir"], "--port", str(file_port),
             "--delay-ms", "60"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not base.wait_port(file_port):
            log("file server 没起来")
            return 1

        report = tmp / "report.json"
        cfg = {"work": str(tmp), "suite": "all", "basic": basic, "download": dl}
        cfg_path = tmp / "e2e-cfg.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")

        run_env = {**env,
                   "PULSES_UI": "web",
                   "PULSES_E2E": "1",
                   "PULSES_E2E_CFG": str(cfg_path),
                   "PULSES_E2E_REPORT": str(report),
                   "PULSES_WEB_LOG": str(log_path),
               # CI 上 WebView2 冷启动可能十几秒，别让它自动退回 Tk
               "PULSES_E2E_BOOT_TIMEOUT": "120"}
        if exe_path is not None:
            cmd = [str(exe_path)]
        else:
            exe = Path(sys.executable)
            cmd = [str(exe)] if getattr(sys, "frozen", False) else [
                str(exe), str(ROOT / "main.py")]
        log("启动：" + " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=run_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1)
        sink: list[str] = []
        threading.Thread(target=pump_output, args=(proc, sink),
                         daemon=True).start()

        # 等报告：每 15 秒一次心跳，绝不无声无息地等到超时
        started = time.time()
        last = 0.0
        last_progress_at = time.time()
        while time.time() - started < TIMEOUT:
            if report.is_file() or proc.poll() is not None:
                break
            if time.time() - last >= HEARTBEAT:
                last = time.time()
                progress = Path(str(report) + ".progress")
                done = ""
                if progress.is_file():
                    lines = progress.read_text(encoding="utf-8").splitlines()
                    if lines:
                        done = f"，最近一步：{lines[-1]}（共 {len(lines)} 步）"
                        last_progress_at = progress.stat().st_mtime
                # 进度 3 分钟没动、报告也没来：别干等到超时
                if time.time() - last_progress_at > 180:
                    log("进度 180s 没动，提前判定失败")
                    break
                log(f"等报告：已 {int(time.time() - started)}s / {int(TIMEOUT)}s，"
                    f"进程{'在跑' if proc.poll() is None else '已退出'}{done}")
            time.sleep(0.5)

        if not report.is_file():
            log(f"{int(TIMEOUT)}s 内没拿到报告（进程退出码 {proc.poll()}）")
            progress = Path(str(report) + ".progress")
            if progress.is_file():
                lines = progress.read_text(encoding="utf-8").splitlines()
                log(f"最后 15 步：\n  " + "\n  ".join(lines[-15:]))
            dump_logs(log_path)
            return 1

        data = json.loads(report.read_text(encoding="utf-8"))
        for item in data.get("steps", []):
            ok = bool(item.get("ok"))
            print(f"{'PASS' if ok else 'FAIL'}  {item.get('name')}"
                  + ("" if ok or not item.get("detail")
                     else f"  →  {item['detail']}"), flush=True)
        log(f"页面内驱动：{len(data.get('steps') or [])} 条断言，"
            f"{'通过' if data.get('ok') else '失败'}（{data.get('ms', 0)} ms）")
        if not data.get("ok"):
            log("失败：" + str(data.get("error")))
            dump_logs(log_path)
            return 1

        if not verify_disk(basic, dl, data):
            log("磁盘复核失败")
            dump_logs(log_path)
            return 1

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
        dump_logs(log_path)
        return 1
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:                           # noqa: BLE001
                proc.kill()
        if server is not None and server.poll() is None:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
