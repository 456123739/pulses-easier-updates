"""Windows 诊断：启动本地 PCL，抓窗口/UIA 树，为导出基准自动化做准备。

不修改仓库、不写玩家数据；全部在 %TEMP%\\pe-pcl-probe 下。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def setup() -> tuple[Path, Path]:
    root = Path(os.environ.get("TEMP", ".")) / "pe-pcl-probe"
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
    (inst / "BenchPack.json").write_text(json.dumps({
        "id": "BenchPack", "type": "release",
        "mainClass": "net.minecraft.client.main.Main",
        "libraries": [], "arguments": {"game": [], "jvm": []},
    }), encoding="utf-8")
    for i in range(8):
        (inst / "mods" / f"m{i}.jar").write_bytes(b"x" * 1024)
    (inst / "config" / "a.toml").write_text("a=1\n", encoding="utf-8")
    return pcl, mc


def set_registry(mc: Path) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\PCL") as key:
        winreg.SetValueEx(key, "LaunchFolders", 0, winreg.REG_SZ, str(mc))
        winreg.SetValueEx(key, "SystemEulaVersion", 0, winreg.REG_DWORD, 999)
        winreg.SetValueEx(key, "UiLauncherTheme", 0, winreg.REG_SZ, "0")


def main() -> int:
    if sys.platform != "win32":
        print("Windows only")
        return 2
    pcl, mc = setup()
    set_registry(mc)
    log = pcl / "probe.log"
    err = open(pcl / "stderr.txt", "w", encoding="utf-8")
    proc = subprocess.Popen([str(pcl / "PCL.exe")], cwd=str(pcl),
                            stdout=err, stderr=subprocess.STDOUT)
    time.sleep(20)
    out = {"pid": proc.pid, "exit": proc.poll(), "windows": [], "controls": []}
    try:
        from pywinauto import Desktop
        desk = Desktop(backend="uia")
        for w in desk.windows():
            try:
                out["windows"].append({"title": w.window_text(),
                                       "class": w.class_name(),
                                       "rect": list(w.rectangle())})
            except Exception:
                pass
        # 主窗口控件树（只抓前 200 个，足够定位“版本管理/导出”）
        for w in desk.windows():
            if "PCL" in w.window_text() or "Plain Craft" in w.window_text():
                try:
                    for c in w.descendants()[:200]:
                        out["controls"].append({
                            "type": c.element_info.control_type,
                            "name": c.window_text(),
                            "auto": c.element_info.automation_id,
                        })
                except Exception as exc:
                    out["error"] = str(exc)
                break
        try:
            from PIL import ImageGrab
            ImageGrab.grab().save(pcl / "screen.png")
        except Exception:
            pass
    except Exception as exc:
        out["error"] = str(exc)
    try:
        out["files"] = [str(p.relative_to(pcl)) for p in pcl.rglob("*") if p.is_file()][:80]
    except Exception:
        pass
    for lg in pcl.rglob("Log*.txt"):
        try:
            out.setdefault("pcl_log", []).extend(lg.read_text(encoding="utf-8", errors="replace").splitlines()[-80:])
        except Exception:
            pass
    log.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        proc.terminate()
    except Exception:
        pass
    print(json.dumps(out, ensure_ascii=False)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
