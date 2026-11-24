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
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def setup() -> tuple[Path, Path, Path, Path]:
    root = Path.cwd() / "bench-run" / "pcl"
    shutil.rmtree(root, ignore_errors=True)
    pcl = root / "PCL"
    pcl.mkdir(parents=True)
    here = Path(__file__).resolve().parents[2]
    shutil.copy2(here / "bench" / "pcl" / "PCL.exe", pcl / "PCL.exe")
    mc = pcl / ".minecraft"
    fixture = os.environ.get("PULSES_REAL_FIXTURE", "").strip()
    if fixture:
        src = Path(fixture) / "versions" / "Minerizon beta"
        if not src.is_dir():
            raise FileNotFoundError(f"real fixture missing: {src}; root={list(Path(fixture).rglob('*'))[:20]}")
        inst = mc / "versions" / "Minerizon beta"
        shutil.copytree(src, inst)
        # Setup.ini 让 PCL 直接扫 portable .minecraft
        (pcl / "PCL").mkdir(exist_ok=True)
        (pcl / "PCL" / "Setup.ini").write_text(
            "LaunchFolderSelect:$.minecraft\\\n", encoding="utf-8")
        name = "Minerizon beta"
    else:
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
        name = "BenchPack"
    out = root / "out.zip"
    return pcl, mc, out, inst


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
                    time.sleep(0.2)
                    try:
                        c.parent().click_input()
                    except Exception:
                        pass
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
    pcl, mc, out, inst = setup()
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
    print("version select click", click_name(win, "版本选择"))
    time.sleep(2)
    dump(win, pcl / "select.json")
    # 版本选择页里点当前实例名，回到启动页，再进“版本设置”（实例管理）
    print("instance click", click_name(win, "Minerizon beta", timeout=3))
    time.sleep(2)
    print("settings click", click_name(win, "版本设置"))
    time.sleep(2)
    dump(win, pcl / "instance.json")
    print("export click", click_name(win, "导出"))
    time.sleep(2)
    dump(win, pcl / "export.json")
    print("start click", click_name(win, "开始导出"))
    time.sleep(5)
    dump(win, pcl / "after.json")
    try:
        print("top-windows", [w.window_text() for w in desk.windows()])
    except Exception as exc:
        print("top-windows-error", exc)
    for lg in pcl.rglob("Log*.txt"):
        try:
            print("pcl-log-tail", lg.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        except Exception:
            pass
    # 保存对话框（PCL 的 WPF 对话框在 UIA 树里，但不是独立顶层窗口）
    try:
        edits = [c for c in win.descendants() if c.element_info.automation_id == "1001"]
        print("save-edits", len(edits))
        if edits:
            edits[0].type_keys(str(out), with_spaces=True)
        for b in win.descendants():
            if b.window_text() in ("Save", "保存"):
                print("save-click", b.window_text())
                b.click_input()
                break
    except Exception as exc:
        print("dialog", exc)
    start = time.time()
    last_size = -1
    stable = 0
    complete = False
    while time.time() - start < 900:
        time.sleep(1)
        if not out.exists():
            continue
        size = out.stat().st_size
        valid = False
        if size > 0 and size == last_size:
            try:
                with zipfile.ZipFile(out) as zf:
                    zf.namelist()
                valid = True
            except Exception as exc:
                if stable == 0:
                    print("zip-not-ready", repr(exc), flush=True)
                valid = False
        if valid:
            stable += 1
            if stable >= 2:
                complete = True
                break
        else:
            stable = 0
        last_size = size
    if not complete:
        print("timeout waiting for complete PCL zip", last_size)
        return 5
    pcl_time = time.time() - start
    pcl_size = out.stat().st_size
    print("done", pcl_time, pcl_size)
    # 同一实例跑我们的引擎（冷缓存），并做完整性对比
    try:
        from app.core.instance_scan import scan_instance
        from app.core.export_rules import expand_rules
        from app.core.instance_export import export_from_instance
        from app.core.fingerprint import FingerprintCache
        src = inst
        rules = expand_rules()
        sc = scan_instance(src, rules)
        our = pcl / "our.zip"
        t0 = time.time()
        r = export_from_instance(
            src, our, rules=rules, scan=sc,
            pack_name="Minerizon beta", pack_version="1.0.0",
            cache=FingerprintCache(pcl / "fp.json"),
        )
        our_time = time.time() - t0
        import zipfile, io

        def _consume(z, names):
            for n in z.namelist():
                n = n.replace("\\", "/")
                if n.endswith("modrinth.index.json"):
                    try:
                        idx = json.loads(z.read(n))
                        names |= {str(f.get("path", "")) for f in idx.get("files", [])}
                    except Exception:
                        pass
                if "/overrides/" in n:
                    names.add(n.split("/overrides/", 1)[1])
                elif n.startswith("overrides/"):
                    names.add(n[len("overrides/"):])
                elif n.endswith(".mrpack"):
                    try:
                        _consume(zipfile.ZipFile(io.BytesIO(z.read(n))), names)
                    except Exception:
                        pass

        pcl_names = set()
        with zipfile.ZipFile(out) as outer:
            _consume(outer, pcl_names)
            print("pcl-outer", [n for n in outer.namelist()[:20]])
        our_names = set()
        if r:
            with zipfile.ZipFile(r) as z:
                _consume(z, our_names)
        missing = sorted(pcl_names - our_names)
        report = {"pcl_time": pcl_time, "pcl_size": pcl_size,
                  "our_time": our_time, "our_size": r.stat().st_size if r else 0,
                  "pcl_files": len(pcl_names), "our_files": len(our_names),
                  "missing": missing[:50], "missing_count": len(missing)}
        (pcl / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("report", json.dumps(report, ensure_ascii=False)[:1000])
    except Exception as exc:
        print("our-export-error", repr(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
