#!/usr/bin/env python3
"""
gui_smoke.py — 真实 GUI 冒烟测试（有显示环境才跑，否则自动跳过）
------------------------------------------------
之前所有 UI 测试都跑在 `tests/stubs.py` 的假 tkinter 上：能验证逻辑，
**证明不了**真实 customtkinter / tkinterdnd2 的调用是对的。

这个脚本用真实库 + 真实窗口跑一遍关键交互：

  1. 真正创建主窗口（MainWindow + TkinterDnD）
  2. 定位整合包 → 拖入更新包 → 比对 → 应用（真实事件循环 pump）
  3. 校验整合包内容、按钮状态、提示文案、残留清理
  4. 真正创建"更新受阻"子窗口（PendingWindow，CTkToplevel）
  5. 可选截图（PIL 能抓到 X11 时）

没有 tkinter / 没有 DISPLAY 时打印"跳过"并退出 0。

运行（本机验证环境）：
  export DISPLAY=:99
  export PYTHONPATH=/tmp/pylibs:/tmp/tkroot/usr/lib/python3.11:/tmp/tkroot/usr/lib/python3.11/lib-dynload
  export LD_LIBRARY_PATH=/tmp/tkroot/usr/lib:/tmp/tkroot/usr/lib/x86_64-linux-gnu
  export TCL_LIBRARY=/tmp/tkroot/usr/share/tcltk/tcl8.6 TK_LIBRARY=/tmp/tkroot/usr/share/tcltk/tk8.6
  python3 tests/gui_smoke.py
"""

import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

_ok: list[str] = []
_bad: list[str] = []


def check(cond, label, extra=""):
    if cond:
        _ok.append(label)
        print(f"  [PASS] {label}")
    else:
        _bad.append(label)
        print(f"  [FAIL] {label}" + (f"  ← {extra}" if extra else ""))


def _missing_reason() -> str:
    if not os.environ.get("DISPLAY"):
        return "没有 DISPLAY（无显示器 / 未启动 Xvfb）"
    try:
        import tkinter  # noqa: F401

        import customtkinter  # noqa: F401
        import tkinterdnd2  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return f"缺少 GUI 依赖：{e}"
    return ""


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _pump(app, seconds: float, until=None) -> bool:
    """跑真实事件循环：Tk 的 after 回调只有 update() 时才会执行。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


def _build_pack(tmp: Path) -> Path:
    """做一个只带 overrides 的更新包（不联网）。"""
    from app.core import eapack as eapack_mod
    from app.core.mrpack import merge_files, parse_mrpack

    src = tmp / "pack_src"
    src.mkdir(parents=True, exist_ok=True)
    index = {
        "formatVersion": 1, "game": "minecraft", "versionId": "9.9.9",
        "name": "GuiSmoke", "files": [], "dependencies": {},
    }
    (src / "modrinth.index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    _write(src / "overrides" / "mods" / "pack-mod.jar", "PACK MOD")
    _write(src / "overrides" / "config" / "shipped.toml", "key = \"new\"\n")
    _write(src / "overrides" / "资源包" / "readme.txt", "中文目录名也要正常")

    src_zip = tmp / "GuiSmoke-9.9.9.zip"
    with zipfile.ZipFile(src_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    pack, err = parse_mrpack(src_zip)
    assert not err and pack is not None, err
    out = tmp / "GuiSmoke-9.9.9.eapack"
    eapack_mod.export_eapack(
        source_dir=src, source_zip=src_zip, out_path=out, pack=pack,
        merged_files=merge_files(pack),
        strategies={"mods": "完全匹配", "config": "替换重名"},
        changelog_md="# GuiSmoke 9.9.9\n\n- 冒烟测试\n")
    return out


def main() -> int:
    reason = _missing_reason()
    if reason:
        print(f"跳过真实 GUI 测试：{reason}")
        return 0

    from app.core import database as db

    tmp = Path(tempfile.mkdtemp(prefix="easier_gui_"))
    dbdir = tmp / "db"
    db.create_database(dbdir)
    db.set_db_path(dbdir)

    inst = tmp / "versions" / "GuiSmoke"
    _write(inst / "mods" / "old.jar", "OLD")
    _write(inst / "config" / "shipped.toml", "key = \"old\"\n")
    _write(inst / "config" / "player-only.toml", "mine = 1\n")

    eapack = _build_pack(tmp)

    import main as main_mod
    from app.ui.main_window import MainWindow
    AppBase, _dnd = main_mod._pick_app_base()
    WindowClass = type("WindowClass", (MainWindow, AppBase), {})

    app = WindowClass()
    pv = app.player_view
    errors: list[str] = []
    t0 = time.time()
    limit = 240.0

    print("\n── 1) 真实主窗口 ──")
    check(True, "主窗口用真实 tkinter/customtkinter 创建成功")
    check(pv is not None, "玩家端视图已建立")

    def step_locate():
        print("\n── 2) 定位整合包 + 拖入更新包 ──")
        app.app_state.set_modpack(inst)
        app.update_idletasks()
        check(pv.pack_info is not None, "定位成功")
        pv._on_update_pack_dropped([eapack])
        check(pv.update_zip == eapack, "更新包已载入")
        check(pv.pack_kind.value == "update", "识别为更新包")
        return True

    def step_scan():
        if not getattr(step_scan, "started", False):
            print("\n── 3) 比对 ──")
            pv._start_scan()
            step_scan.started = True
            return False
        if pv.diff is None:
            return False
        if pv.diff is not None:
            print(f"    变更：+{len(pv.diff.added)} ~{len(pv.diff.modified)} "
                  f"-{len(pv.diff.deleted)}")
        check(pv.diff is not None, "比对完成")
        check(pv._btn_state == "confirm", "按钮进入「确认更新」",
              pv._btn_state)
        return True

    def step_apply():
        if not getattr(step_apply, "started", False):
            print("\n── 4) 应用（真实事件循环） ──")
            pv._start_download_and_apply()
            step_apply.started = True
            return False
        if pv._phase != "idle":
            return False
        check(True, "应用阶段结束（phase=idle）")
        check((inst / "mods" / "pack-mod.jar").is_file(), "包内 mod 已就位")
        check((inst / "config" / "shipped.toml").read_text(
            encoding="utf-8") == 'key = "new"\n', "config 同名文件被覆盖")
        check((inst / "config" / "player-only.toml").is_file(),
              "玩家自己的文件保留")
        check((inst / "资源包" / "readme.txt").is_file(), "中文目录名正常")
        leftovers = [p.name for p in inst.rglob("*") if "pulses_" in p.name]
        check(not leftovers, "整合包内没有残留临时文件", str(leftovers))
        check(pv.update_zip is None,
              "更新完成后清掉了旧更新包（回到「请拖入更新包」状态）")
        check(pv._btn_state != "ready",
              "不再留着「开始更新」让玩家重复点", pv._btn_state)
        check(pv.diff is None, "变更列表已复位")
        check("下一个更新包" in getattr(pv, "_placeholder_text", ""),
              "提示玩家拖入下一个更新包")
        return True

    def step_pending():
        print("\n── 5) 「更新受阻」子窗口（CTkToplevel） ──")
        from app.core import pending as P
        from app.ui.widgets.pending_window import PendingWindow
        marker = P.build_marker(
            inst, pack_name="GuiSmoke", cache_root=str(tmp / "cache"),
            missing=[{"rel": "mods/missing.jar", "sha1": "0" * 40,
                      "size": 123,
                      "urls": ["https://example.invalid/a.jar"]}])
        win = PendingWindow(app, mode="wait")
        win.set_items(marker["missing"])
        win.show_window()
        app.update_idletasks()
        check(win.pending_count() == 1, "子窗口列出 1 个待补项")
        check(win.current_rel() == "mods/missing.jar", "当前页正确")
        win.add_item({"rel": "mods/missing.jar", "resolved": True})
        win.resolve("mods/missing.jar")
        check(win.pending_count() == 0, "补入后待补项清空")
        win.destroy()
        app.update_idletasks()
        check(True, "子窗口销毁无异常")
        return True

    def step_shot():
        print("\n── 6) 截图 ──")
        try:
            from PIL import ImageGrab
            app.update_idletasks()
            img = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY"))
            shot = _ROOT / "tests" / "logs" / "gui_smoke.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            img.save(shot)
            print(f"    已保存截图：{shot}  {img.size}")
            check(True, "截图成功")
        except Exception as e:  # noqa: BLE001
            print(f"    （截图跳过：{e}）")
        return True

    steps = [step_locate, step_scan, step_apply, step_pending, step_shot]

    def tick():
        try:
            if steps and steps[0]():
                steps.pop(0)
        except Exception as e:  # noqa: BLE001
            import traceback
            errors.append(f"{type(e).__name__}: {e}")
            traceback.print_exc()
            steps.clear()
        if steps and time.time() - t0 < limit:
            app.after(40, tick)
        else:
            if steps:
                errors.append(f"超时，剩余步骤 {len(steps)} 个")
            app.quit()

    app.after(40, tick)
    app.mainloop()

    try:
        app.destroy()
    except Exception:  # noqa: BLE001, S110
        pass

    print("\n" + "=" * 62)
    print(f"通过 {len(_ok)} 项，失败 {len(_bad) + len(errors)} 项")
    for e in errors:
        print(f"  ! {e}")
    if _bad:
        for b in _bad:
            print(f"  ✗ {b}")
    if _bad or errors:
        return 1
    print("真实 GUI 冒烟测试：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
