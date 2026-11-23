"""Windows 基准：真实 PCL 导出（UI 自动化）。

仅用于 CI 证据，不参与发布；所有文件在 bench-run/pcl 下。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def setup() -> tuple[Path, Path, Path]:
    root = Path.cwd() / "bench-run" / "pcl"
    shutil.rmtree(root, ignore_errors=True)
    pcl = root / "PCL"
    pcl.mkdir(parents=True)
    here = Path(__file__).resolve().parents[2]
    shutil.copy2(here / "bench" / "pcl" / "PCL.exe", pcl / "PCL.exe")
    mc = pcl / ".minecraft"
    inst = mc / "versions" / "BenchPack"
    (inst / "mods").mkdir(parents=True)
    (inst / "config").mkdir()
    (inst / "saves" / "World").mkdir(parents=True)
    shutil.copy2(here / "bench" / "pcl" / "BenchPack.json", inst / "BenchPack.json")
    (inst / "version.json").write_text((inst / "BenchPack.json").read_text(encoding="utf-8"), encoding="utf-8")
    (inst / "BenchPack.jar").write_bytes(b"")
    for i in range(40):
        (inst / "mods" / f"m{i}.jar").write_bytes(os.urandom(2048))
    for i in range(100):
        (inst / "config" / f"c{i}.toml").write_text(f"a={i}\n", encoding="utf-8")
    (inst / "saves" / "World" / "level.dat").write_bytes(b"level" * 100)
    out = root / "out.zip"
    return pcl, mc, out


def dump(win, path: Path) -> None:
    rows = []
    try:
        for c in win.descendants()[:600]:
            try:
                rows.append({"type": c.element_info.control_type,
                             "name": c.window_text(),
                             "auto": c.element_info.automation_id})
            except Exception:
                pass
    except Exception as exc:
        rows.append({"error": str(exc)})
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def click_name(win, name: str, timeout: float = 8) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        for c in win.descendants():
            try:
                if c.window_text() == name and c.is_visible():
                    c.click_input()
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.platform != "win32":
        return 2
    pcl, mc, out = setup()
    proc = subprocess.Popen([str(pcl / "PCL.exe")], cwd=str(pcl))
    time.sleep(12)
    from pywinauto import Desktop
    desk = Desktop(backend="uia")
    win = None
    for w in desk.windows():
        if "Plain Craft Launcher" in w.window_text():
            win = w
            break
    if win is None:
        print("PCL window not found")
        return 3
    dump(win, pcl / "before.json")
    print("before", win.window_text())
    if click_name(win, "同意", timeout=3):
        print("eula accepted")
        time.sleep(2)
    print("version settings click", click_name(win, "版本设置"))
    time.sleep(2)
    dump(win, pcl / "instance.json")
    print("export click", click_name(win, "导出"))
    time.sleep(2)
    dump(win, pcl / "export.json")
    print("start click", click_name(win, "开始导出"))
    time.sleep(2)
    # 保存对话框
    try:
        dlg = None
        for w in desk.windows():
            t = w.window_text()
            if "导出" in t or "保存" in t or "另存" in t:
                dlg = w
                print("dialog-title", t)
                break
        if dlg is not None:
            edits = [c for c in dlg.descendants() if c.element_info.control_type == "Edit"]
            if edits:
                edits[0].type_keys(str(out), with_spaces=True)
            else:
                dlg.type_keys(str(out), with_spaces=True)
            for b in dlg.descendants():
                if b.window_text() in ("保存", "Save", "确定", "OK"):
                    b.click_input(); break
    except Exception as exc:
        print("dialog", exc)
    start = time.time()
    for _ in range(120):
        time.sleep(1)
        if out.exists() and out.stat().st_size > 0:
            print("done", time.time() - start, out.stat().st_size)
            return 0
        if proc.poll() is not None:
            print("PCL exited", proc.returncode)
            return 4
    print("timeout")
    return 5


if __name__ == "__main__":
    sys.exit(main())
