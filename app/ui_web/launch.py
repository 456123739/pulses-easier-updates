"""
launch.py — 新界面的受监督启动
------------------------------------------------
WebView2 在某些机器/会话里会"卡在起窗"（不报错也不出画面）。为了不让玩家
看到黑屏，默认路径改成：父进程拉起一个子进程跑新界面，子进程在页面加载完成
后写一个就绪文件；父进程等不到就杀掉它、自己退回 Tk。

    PULSES_UI=auto（默认）→ 受监督启动，失败退回 Tk
    PULSES_UI=web         → 在本进程里直接跑（调试用，失败就炸）
    PULSES_UI=tk          → 只用 Tk
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
    script = entry or str(Path(sys.argv[0]).resolve())
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

    cwd = (Path(sys.executable).parent if getattr(sys, "frozen", False)
           else Path(entry).resolve().parent if entry else Path.cwd())
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
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:                            # noqa: BLE001, S110
            pass
        return None
    return proc.wait()


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
