#!/usr/bin/env python3
"""开发环境自检：一次确认所有工具都在，缺了直说。

用法：
    .venv/bin/python scripts/dev/check_env.py
退出码 0 = 全部必需项就绪；1 = 有必需项缺失（会在表里标 MISSING）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROWS: list[tuple[str, bool, str]] = []


def add(name: str, ok: bool, detail: str = "", required: bool = True) -> None:
    mark = "OK  " if ok else ("MISS" if required else "skip")
    ROWS.append((name, ok, f"{mark} {detail}"))


def mod(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def run(cmd: list[str], **kw) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 30))
        return p.returncode, (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else ""
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def main() -> int:
    # ---- Python ----
    add("python", sys.version_info[:2] >= (3, 11), sys.version.split()[0])
    for m in ("pytest", "PIL", "customtkinter", "tkinterdnd2", "markdown",
              "multimark", "tkhtmlview", "webview"):
        add(f"py:{m}", mod(m), "importable")

    # ---- Node ----
    node = shutil.which("node") or ""
    add("node", bool(node), (run([node, "-v"])[1] if node else "missing"))
    npm = shutil.which("npm") or ""
    add("npm", bool(npm), (run([npm, "-v"])[1] if npm else "missing"))
    web = ROOT / "web"
    add("web/node_modules", (web / "node_modules").is_dir(),
        f"{web}/node_modules")
    add("web/node_modules/.bin/vite", (web / "node_modules/.bin/vite").exists(),
        "vite 可执行")

    # ---- 浏览器 ----
    chrome = Path("/home/admin/.cache/ms-playwright/chromium-1194/chrome-linux/chrome")
    add("chromium", chrome.exists(), run([str(chrome), "--version"])[1] if chrome.exists() else str(chrome))
    add("playwright(pkg)", (home := Path("/home/admin")) and (Path("/home/admin/111/pulses-swap/src/pulses_web/web/node_modules/playwright")).exists(),
        "111/pulses-swap 里已装 1.56.1（可 npm i 复制）", required=False)

    # ---- 虚拟显示 ----
    xvfb = ROOT / ".tools/xvfb/usr/bin/Xvfb"
    add("xvfb", xvfb.exists(), str(xvfb))
    add("xdpyinfo", shutil.which("xdpyinfo") is not None, "系统自带", required=False)

    # ---- 资源 ----
    add(".tools/pywebview", (ROOT / ".tools/pywebview-6.2.1-webview").is_dir(),
        "pywebview 6.2.1 源码（API 以它为准）")
    add("REF pulses-swap", Path("/home/admin/111/pulses-swap/src/pulses_web").is_dir(),
        "pywebview+React 参考实现", required=False)
    add("REF pcl_analysis", Path("/home/admin/NS/pcl_analysis").is_dir(),
        "PCL2 源码", required=False)

    # ---- 字体 ----
    fonts = list(Path("/home/admin/.fonts").rglob("*.ttc"))
    add("CJK 字体", bool(fonts), f"{len(fonts)} 个 ttc（Noto CJK，截图用）")

    # ---- git / gh ----
    add("git", shutil.which("git") is not None, run(["git", "--version"])[1])
    add("gh", shutil.which("gh") is not None, "CI 用", required=False)

    # ---- 输出 ----
    width = max(len(n) for n, _, _ in ROWS) + 2
    print("=" * (width + 40))
    print("Pulses Easier 开发环境自检")
    print("=" * (width + 40))
    for name, ok, detail in ROWS:
        print(f"{name:<{width}} {detail}")
    required_missing = [n for n, ok, d in ROWS if not ok and not d.startswith("skip")]
    print("=" * (width + 40))
    if required_missing:
        print("缺失必需项：" + ", ".join(required_missing))
        return 1
    print("全部必需项就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
