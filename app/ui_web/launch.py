"""
launch.py — 新界面的受监督启动
------------------------------------------------
WebView2 在某些机器/会话里会"卡在起窗"（不报错也不出画面）。这里提供一条
受监督路径：父进程拉起一个子进程跑新界面，子进程在页面加载完成后写一个就绪
文件；父进程等不到就杀掉它、自己退回 Tk。

⚠️ 当前 main.py 的默认路径不走这里：0.10.4 起 `auto` 改为**在本进程里**起新界面，
异常/非 0 退出再退回 Tk（打包环境里子进程的 WebView2 反而起不来）。
本模块只在显式运行 `python -m app.ui_web.launch`（或测试）时生效。

    python -m app.ui_web.launch     → 受监督启动；就绪超时/子进程崩溃时返回 None
                                      （调用方据此退回 Tk；单独运行时直接退出）
    PULSES_UI=web                   → 在 main.py 进程内直接跑（调试用，失败就抛）
    PULSES_UI=tk                    → 只用 Tk
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BOOT_TIMEOUT = 25.0            # 等"页面加载完成"的最长时间（秒）


def child_command(ready_path: str, entry: str | None = None) -> list[str]:
    """受监督子进程的启动命令：冻结版直接复用 exe，源码版用 python + 入口脚本。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--web-host", ready_path]
    # 源码运行：子进程必须是 main.py —— 它负责分发 --web-host 到 host.run()
    # （用 launch.py 自己当入口会再跑一遍 run_supervised，变成套娃）
    script = entry or str(Path(__file__).resolve().parents[2] / "main.py")
    return [sys.executable, script, "--web-host", ready_path]


def _quiet_kwargs() -> dict:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def run_supervised(entry: str | None = None,
                   timeout: float | None = None) -> int | None:
    """跑新界面子进程。

    返回：正常关闭 → 退出码；子进程自己退出 → 它的退出码；
    超时没等到就绪文件 → None（调用方据此退回 Tk）。
    """
    limit = BOOT_TIMEOUT if timeout is None else timeout
    fd, ready_path = tempfile.mkstemp(prefix="pulses-web-", suffix=".ready")
    os.close(fd)
    try:
        os.unlink(ready_path)
    except OSError:
        pass

    if getattr(sys, "frozen", False):
        cwd = Path(sys.executable).parent
    else:
        # 与 child_command 的入口保持一致：源码下子进程是 main.py，cwd 必须是项目根
        cwd = Path(entry).resolve().parent if entry else Path(__file__).resolve().parents[2]
    try:
        proc = subprocess.Popen(  # noqa: S603
            child_command(ready_path, entry),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(cwd),
            **_quiet_kwargs())
    except Exception:                                # noqa: BLE001
        return None

    deadline = time.time() + limit
    ready = False
    while time.time() < deadline:
        if os.path.exists(ready_path):
            ready = True
            break
        if proc.poll() is not None:
            _cleanup(ready_path)
            # 还没加载完就退了：非 0 一律当成失败，让上层退回 Tk
            return 0 if proc.returncode == 0 else None
        time.sleep(0.1)
    _cleanup(ready_path)
    if not ready:
        _kill_tree(proc)
        return None
    return proc.wait()


def _kill_tree(proc) -> None:
    """子进程可能带着 WebView2 的子进程，必须连树一起杀，否则会留在后台空转。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],  # noqa: S603,S607
                           capture_output=True, timeout=10, check=False)
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:                                # noqa: BLE001, S110
        pass


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def runtime_available() -> bool:
    """本机有没有 WebView2 运行时（没有就别起新界面）。"""
    if sys.platform != "win32":
        return True
    bases = [os.environ.get("ProgramFiles(x86)", ""),
             os.environ.get("ProgramFiles", "")]
    for base in bases:
        if not base:
            continue
        root = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
        if not root.is_dir():
            continue
        try:
            for sub in root.iterdir():
                if (sub / "msedgewebview2.exe").is_file():
                    return True
        except OSError:
            continue
    return False


if __name__ == "__main__":
    sys.exit(run_supervised() or 0)
