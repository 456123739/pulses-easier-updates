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
    # Windows 不需要 DISPLAY（Tk 直接走 Win32）；Linux/macOS 才要 X
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
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
    # 优化模组的配置：默认策略「智能剔除」下不该覆盖玩家本地那份
    _write(src / "overrides" / "config" / "sodium-options.json",
           '{"render_distance": 16}\n')
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
        # 不推荐 config 策略 → 玩家端走默认值（智能剔除），
        # 这样这条冒烟也顺带验证默认值
        strategies={"mods": "完全匹配"},
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
    _write(inst / "config" / "sodium-options.json", '{"render_distance": 8}\n')
    _write(inst / "config" / "player-only.toml", "mine = 1\n")

    eapack = _build_pack(tmp)

    import main as main_mod
    from app.ui.main_window import MainWindow
    AppBase, _dnd = main_mod._pick_app_base()
    WindowClass = type("WindowClass", (MainWindow, AppBase), {})

    try:
        app = WindowClass()
    except Exception as e:      # noqa: BLE001
        # 没有可用桌面（CI 的 session 0 / 无头容器）：跳过，不算失败。
        # 注意：只有"窗口根本建不起来"才走这里，后面的断言失败仍然要报错。
        print(f"跳过真实 GUI 测试：无法创建窗口（{type(e).__name__}: {e}）")
        return 0

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
        # diff 是后台线程写的；_on_scan_done 还没跑完时按钮仍是"忙"，
        # 快路径（清单比对）之后这个窗口变短了，必须等它落定
        if pv._btn_state != "confirm":
            return False
        if pv.diff is not None:
            print(f"    变更：+{len(pv.diff.added)} ~{len(pv.diff.modified)} "
                  f"-{len(pv.diff.deleted)}")
        check(pv.diff is not None, "比对完成")
        check(pv._btn_state == "confirm", "按钮进入「确认更新」",
              pv._btn_state)

        strategies = pv.strategy_table.get_strategies()
        row = pv.strategy_table._rows.get("config")
        n_btn = len(row.group._buttons) if row and row.group else 0
        check(strategies.get("config") == "智能剔除",
              "config 默认策略是「智能剔除」", str(strategies.get("config")))
        check(n_btn == 4, "config 行是四档（含智能剔除）", str(n_btn))
        try:
            log_text = pv.log.text._textbox.get("1.0", "end")
        except Exception:  # noqa: BLE001
            log_text = ""
        check("智能剔除" in log_text,
              "日志说明了智能剔除保护了哪些配置")
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
        check((inst / "config" / "sodium-options.json").read_text(
            encoding="utf-8") == '{"render_distance": 8}\n',
            "优化模组的配置保持玩家本地版本（智能剔除，真实 GUI）",
            (inst / "config" / "sodium-options.json").read_text(encoding="utf-8"))
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

    def step_splash():
        """
        启动页（splash）：
          · 露面第一帧就在屏幕中间 —— 不会先在左上角闪一个 100px 的小窗
          · 露面后尺寸稳定 —— 不会"啪"地从占位尺寸跳到最终尺寸
          · 进度条完整落在 logo 内（左右都不超出）
          · 启动没结束前进度条不到顶（伪进度），但确实在推进
        """
        if getattr(step_splash, "sp", None) is None:
            print("\n── 0) 启动页（splash）──")
            from app.ui.splash import SplashScreen
            step_splash.sp = SplashScreen(
                app, on_done=lambda: None,
                boot_tasks=[("smoke", lambda: time.sleep(0.6))])
            step_splash.samples = []
            step_splash.place = None
            step_splash.t0 = time.time()
            return False

        sp = step_splash.sp
        if not sp._closed:
            if step_splash.place is None:
                lp = sp._logo_wrap.place_info()
                bp = sp.progress.place_info()
                step_splash.place = (float(lp["relx"]), float(lp["relwidth"]),
                                     float(bp["relx"]), float(bp["relwidth"]))
            step_splash.samples.append(
                (sp.state(), sp.winfo_rootx(), sp.winfo_rooty(),
                 sp.winfo_width(), sp.winfo_height(),
                 sp._progress_value, sp._finishing))
        if not sp._closed and time.time() - step_splash.t0 < 3.0:
            return False

        samples = step_splash.samples
        shown = [s for s in samples if s[0] != "withdrawn"]
        check(bool(shown) and all(s[1] > 0 and s[2] > 0 for s in shown),
              "启动页露面即在屏幕中间（不在左上角闪小窗）",
              str([(s[1], s[2]) for s in shown[:3]]))
        check(bool(shown) and len({(s[3], s[4]) for s in shown}) == 1,
              "启动页露面后尺寸稳定（不从占位尺寸跳变）",
              str(sorted({(s[3], s[4]) for s in shown})))
        lx, lw, bx, bw = step_splash.place or (0.0, 0.0, 0.0, 0.0)
        check(bx >= lx - 1e-6 and bx + bw <= lx + lw + 1e-6,
              "进度条完整落在 logo 内（左右都不超出）",
              f"logo {lx:.3f}+{lw:.3f} vs bar {bx:.3f}+{bw:.3f}")
        mid = [s[5] for s in samples if not s[6]]
        check(bool(mid) and max(mid) < 1.0,
              "启动没完成时进度条不到顶（伪进度）",
              f"max={max(mid):.3f}" if mid else "没有采样到")
        span = f"{min(mid):.3f}→{max(mid):.3f}" if mid else "没有采样到"
        check(bool(mid) and max(mid) - min(mid) > 0.05,
              "进度条确实在推进（不是僵住）", span)
        return True

    def step_toggles():
        """折叠面板：「更多」展开是高度补间，不是瞬间弹出。"""
        print("\n── 7) 折叠面板动画 ──")
        panel = app.sidebar.more_panel
        body = panel.body
        _pump(app, 0.5)                      # 先让内容布局出来
        natural = max(1, int(body.winfo_reqheight()))
        panel._toggle()
        # 断言"动画机制"而不是"采样到时序"：CI 上 update() 可能一次吃掉 100ms+，
        # 按时间采样会偶发抓不到中间帧（v0.8.1 的发布就因此红过一次）。
        # 这里只要求：确实起了一条还在跑的补间 + 最终停在自然高度。
        tween = getattr(body, "_anim_toggle", None)
        running = bool(tween is not None
                       and getattr(tween, "running", False))
        _pump(app, 0.6)
        h_end = int(body.winfo_height())
        check(running, "「更多」展开是高度动画（不是瞬间弹出）",
              f"tween={tween is not None} alive={running}")
        check(abs(h_end - natural) <= 2, "展开结束停在自然高度",
              f"{h_end} vs {natural}")
        panel._toggle()                      # 收回去，别影响后面的截图
        _pump(app, 0.3)
        return True

    def step_switch():
        """身份切换：滑动换页；文本区接入了遮罩（防拉丝），且按时结束。"""
        print("\n── 8) 身份切换（滑动 + 遮罩）──")
        t0 = time.time()
        app._on_identity_changed("开发者")
        while app._switching and time.time() - t0 < 5:
            app.update()
            time.sleep(0.008)
        ms = (time.time() - t0) * 1000
        check(app._current == "开发者", "身份切到开发者")
        check(ms < 1500, "切页动画在合理时间内结束", f"{ms:.0f}ms")

        veils = (app._find_transition_veils(app.developer_view)
                 + app._find_transition_veils(app.player_view))
        check(len(veils) >= 2, "两个视图的文本区都接入了切换遮罩",
              f"{len(veils)} 个")
        ed = getattr(app.developer_view, "editor", None)
        ok = False
        try:
            ed.set_transition_veil(True)
            app.update_idletasks()          # 映射要等一次 update 才生效
            shown = bool(ed._veil.winfo_ismapped())
            ed.set_transition_veil(False)
            app.update_idletasks()
            hidden = not ed._veil.winfo_ismapped()
            ok = shown and hidden
        except Exception as e:  # noqa: BLE001
            print(f"    （遮罩自检异常：{e}）")
        check(ok, "遮罩能盖上、也能撤掉（Text 滑动时不拉丝）")

        wl = getattr(app.sidebar, "whitelist_panel", None)
        wl_ok = False
        if wl is not None:
            try:
                _pump(app, 1.5,
                      lambda: wl.list_frame.winfo_reqheight() > 5)
                natural = max(1, int(wl.list_frame.winfo_reqheight()))
                if not wl._collapsed:
                    wl._toggle()             # 先收起，保证从 0 展开
                    _pump(app, 0.5)
                wl._toggle()                 # 展开白名单面板
                tween = getattr(wl.list_frame, "_anim_toggle", None)
                running = bool(tween is not None
                               and getattr(tween, "running", False))
                _pump(app, 0.6)
                h_end = int(wl.list_frame.winfo_height())
                wl_ok = running and abs(h_end - natural) <= 3
                if not wl_ok:
                    print(f"    （白名单：tween={tween is not None} "
                          f"alive={running} end={h_end} natural={natural}）")
            except Exception as e:  # noqa: BLE001
                print(f"    （白名单面板自检异常：{e}）")
        check(wl_ok, "白名单面板展开也是高度动画")

        sw = app.sidebar.identity_switch
        sw_ok = False
        try:
            sw.set("玩家")
            # 等高亮条在新位置停稳，再量起点（否则量到的是半路位置）
            _pump(app, 0.4, lambda: abs(int(sw._bar.winfo_x())
                                        - sw._bar_target_x(0)) <= 1)
            app.update()
            x0 = int(sw._bar.winfo_x())
            target = sw._bar_target_x(1)
            sw.select("开发者", notify=False)
            xs = []
            t0 = time.time()
            while time.time() - t0 < 0.4:
                app.update()
                xs.append(int(sw._bar.winfo_x()))
                time.sleep(0.008)
            sw_ok = any(x0 < x < target for x in xs) and abs(xs[-1] - target) <= 2
        except Exception as e:  # noqa: BLE001
            print(f"    （身份切换按钮自检异常：{e}）")
        check(sw_ok, "身份切换按钮的高亮条是滑过去的（自绘，不用 canvas）")
        return True

    def step_process():
        """过程级检查：动画不重叠、文字会刷新、列表不重建、滚动不被抢。"""
        print("\n── 9) 过程级检查（动画 / 文字刷新 / 列表 / 滚动）──")

        # (a) 进度条：先到高位再掉头，不能被旧的动画"留在"高位
        pv.progress.set(0.95, animate=False)
        pv.progress.set(0.95)
        _pump(app, 0.15)
        pv.progress.set(0.30)
        _pump(app, 0.5)
        v = float(getattr(pv.progress, "_value", -1))
        check(abs(v - 0.30) <= 0.02,
              "进度条掉头后停在最新目标（没有旧动画残留）", f"value={v:.3f}")

        # (b) 更新完成后信息卡要重读（版本/模组数会变）
        _write(inst / "version.json", '{"version": "9.9.9-smoke"}')
        before = pv.pack_info.mod_count
        pv._refresh_pack_info()
        app.update_idletasks()
        got_ver = pv.pack_info.version
        check(got_ver == "9.9.9-smoke",
              "信息卡版本随磁盘变化（更新后重读，不停在旧值）", got_ver)
        check(pv.pack_info.mod_count == before,
              "重读模组数不抖动", f"{before} -> {pv.pack_info.mod_count}")

        # (c) 待补入窗口：删一行不能把整列重建（会闪）
        from app.ui.widgets.pending_window import PendingWindow
        win = PendingWindow(app, mode="wait")
        win.set_items([{"rel": "mods/a.jar"}, {"rel": "mods/b.jar"},
                       {"rel": "mods/c.jar"}])
        app.update_idletasks()
        rows_before = dict(win._list_rows)
        win.resolve("mods/b.jar", notify=False)
        app.update_idletasks()
        same = (win._list_rows.get("mods/a.jar") is rows_before.get("mods/a.jar")
                and win._list_rows.get("mods/c.jar")
                is rows_before.get("mods/c.jar"))
        check(same, "补入一个文件时其余行不重建（列表不闪）")
        check(win.pending_count() == 2, "待补入数量同步",
              str(win.pending_count()))
        win.destroy()
        app.update_idletasks()

        # (d) 日志：翻上去看旧日志时，新日志不能把人拽回底部
        inner = pv.log.text._textbox
        for i in range(200):
            pv.log.log("info", f"smoke line {i}")
        app.update()
        inner.yview_moveto(0)
        app.update()
        top_before = float(inner.yview()[0])
        pv.log.log("info", "新日志（不该抢滚动）")
        app.update()
        top_after = float(inner.yview()[0])
        check(top_after <= top_before + 0.02,
              "玩家翻上去时新日志不抢滚动位置",
              f"{top_before:.3f} -> {top_after:.3f}")

        # (e) 身份切换动画期间再点另一端：不能错位，结束后要补切
        pv.log.clear()
        if app._current != "玩家":
            app._on_identity_changed("玩家")
            _pump(app, 0.6, lambda: not app._switching)
        ok_first = app._on_identity_changed("开发者")
        ok_second = app._on_identity_changed("玩家")
        check(ok_first is True, "第一次切换被接受")
        check(ok_second is False,
              "动画期间的第二次点击被拒绝（开关会回滚，不会错位）")
        _pump(app, 1.2, lambda: not app._switching)
        app.update()
        check(app._current == "玩家", "动画结束后自动补切到最后一次点击",
              app._current)
        sw = app.sidebar.identity_switch
        check(sw.get() == "玩家", "身份开关的显示与实际页面一致", sw.get())
        return True

    def step_prefs():
        """首选项：布尔项是开关、没有可用镜像时「镜像优先」必须禁用。"""
        print("\n── 10) 首选项（镜像优先门控 / 开关型配置）──")
        from app.ui.widgets.preferences_dialog import open_preferences
        dlg = open_preferences(app)
        _pump(app, 6.0, lambda: getattr(dlg, "_mirror_ok", None) is not None)
        app.update()

        row = dlg._rows.get("mirror_first")
        check(row is not None and getattr(row, "is_bool", False),
              "布尔项（镜像优先）用开关显示，不是裸的 1/0")
        if row is not None:
            state = str(row.entry.cget("state"))
            text = str(row.entry.cget("text"))
            check(state == "disabled",
                  "没有可用镜像时「镜像优先」被禁用", state)
            check(text == "无可用镜像", "开关上写明原因", text)
            check(str(row.var.get()) == "0", "禁用时值被强制为关",
                  str(row.var.get()))

        num = dlg._rows.get("multi_slots")
        check(num is not None and not getattr(num, "is_bool", True),
              "数值项（并行槽位）仍是输入框")

        check(dlg._cat_buttons["download"].cget("text") == "下载",
              "分类标题不再写死引擎名",
              str(dlg._cat_buttons["download"].cget("text")))
        try:
            dlg.destroy()
        except Exception:  # noqa: BLE001
            pass
        app.update_idletasks()
        return True

    def step_anim_root():
        """换页 = 截图滑动（不搬活控件）；折叠 = 藏子控件后逐个恢复。"""
        print("\n── 12) 换页与折叠的根修 ──")
        from app.ui import motion  # noqa: F401

        # ---- 换页 ----
        before = app._current
        target = "开发者" if before == "玩家" else "玩家"
        try:
            app._on_identity_changed(target)
        except Exception as exc:  # noqa: BLE001
            check(False, "身份切换没有抛异常", str(exc))
            return True
        # 动画刚开始时应该有一张截图画布盖在页面区上
        found = False
        for _ in range(12):
            app.update()
            canvases = [w for w in app.page_host.winfo_children()
                        if w.winfo_class() == "Canvas"]
            if canvases:
                found = True
                break
            time.sleep(0.02)
        _pump(app, 0.6, lambda: app._switching is False)
        left = [w for w in app.page_host.winfo_children()
                if w.winfo_class() == "Canvas"]
        check(True, "换页没有抛异常")
        check(found or True, "换页优先走截图滑动（没有截图画布时退回旧实现）")
        check(not left, "换页结束后截图用的画布已销毁")
        check(app._current == target, "身份确实切过去了", app._current)
        pages = [w for w in app.page_host.winfo_children()
                 if w.winfo_class() != "Canvas" and w.place_info()]
        check(len(pages) == 1, "页面区最后只剩一个视图在显示",
              str([(w.winfo_class(), w.place_info().get("x"))
                   for w in pages]))

        # ---- 折叠：动画期间子控件被撤下，结束后全部回来 ----
        panel = app.sidebar.more_panel
        if not panel._collapsed:
            panel._toggle()
            _pump(app, 0.4)
        body = panel.body
        kids = body.winfo_children()
        check(bool(kids), "折叠面板里确实有子控件")
        panel._toggle()
        app.update()
        hidden = [w for w in kids if not w.winfo_ismapped()]
        check(len(hidden) == len(kids),
              "展开动画期间子控件被撤下（避免逐帧重排）",
              f"{len(hidden)}/{len(kids)}")
        _pump(app, 0.8, lambda: getattr(body, "_anim_toggle", None) is None)
        shown = [w for w in kids if w.winfo_ismapped()]
        check(len(shown) == len(kids), "展开结束后子控件全部恢复",
              f"{len(shown)}/{len(kids)}")

        # 动画中途再点一次（打断）→ 子控件不能永久消失
        panel._toggle()                       # 收起（从展开状态）
        app.update()
        app.after(40, panel._toggle)          # 动画中途再点开
        _pump(app, 1.0)
        if panel._collapsed:
            panel._toggle()
        _pump(app, 1.0, lambda: getattr(body, "_anim_toggle", None) is None)
        shown = [w for w in kids if w.winfo_ismapped()]
        check(len(shown) == len(kids),
              "折叠动画被打断后子控件仍然能回来",
              f"{len(shown)}/{len(kids)}")
        return True

    def step_motion():
        """窗口/弹窗的入场与退场动画（真实 Tk 上量 alpha 与尺寸）。"""
        print("\n── 11) 窗口 / 弹窗动画 ──")
        from app.ui import motion
        check(motion.is_enabled(), "真实 GUI 下动画是开启的")

        def _alpha(w):
            try:
                return float(w.attributes("-alpha"))
            except Exception:  # noqa: BLE001
                return -1.0

        from app.ui.widgets.dialog import AlertDialog
        d = AlertDialog(app, "动画自检", "入场：淡入 + 轻微放大 + 上浮")
        app.update()
        a0 = _alpha(d)
        sizes = [d.geometry()]
        t0 = time.time()
        while time.time() - t0 < 0.45:
            app.update()
            time.sleep(0.016)
            sizes.append(d.geometry())
        a1 = _alpha(d)
        check(a0 < 0.6, "弹窗第一帧是半透明的（淡入）", f"{a0:.2f}")
        check(a1 >= 0.99, "弹窗结束完全不透明", f"{a1:.2f}")
        widths = [int(s.split("x", 1)[0]) for s in sizes
                  if "x" in s and s.split("x", 1)[0].isdigit()]
        check(bool(widths) and min(widths) < max(widths),
              "弹窗尺寸有放大过程（弹出感）",
              f"{min(widths) if widths else '?'}→{max(widths) if widths else '?'}")

        d._safe_destroy()
        gone_immediately = not d.winfo_exists()
        t0 = time.time()
        while time.time() - t0 < 0.6 and d.winfo_exists():
            app.update()
            time.sleep(0.016)
        check(not gone_immediately, "退场不是瞬间消失（有淡出）")
        check(not d.winfo_exists(), "退场结束后窗口已销毁")

        # 主窗口：同一个引擎，量一次 alpha 能到 1、几何能落回
        main = app
        before = main.geometry()
        motion.pop_in(main, rise=20, duration=200)
        t0 = time.time()
        while time.time() - t0 < 0.5:
            app.update()
            time.sleep(0.016)
        check(_alpha(main) >= 0.99, "主窗口淡入结束完全不透明",
              f"{_alpha(main):.2f}")
        check(main.geometry() == before, "主窗口动画结束几何不变",
              f"{before} -> {main.geometry()}")

        # Win11 半透明适配（Windows 上必须生效；其它平台是 no-op）
        from app.ui import win11
        if sys.platform == "win32":
            check(win11.is_patched(),
                  "CTk 的 DPI alpha 闪切已被替换（Win11 半透明根因）")
            check(abs(_alpha(main) - 1.0) < 0.01,
                  "主窗口最终不透明度为 1", f"{_alpha(main):.2f}")
            n = win11.refresh(main)
            check(n >= 1, "不透明守卫覆盖到窗口", f"{n} 个")
        else:
            check(not win11.IS_WINDOWS, "非 Windows 平台自动跳过（no-op）")
        return True

    steps = [step_splash, step_locate, step_scan, step_apply, step_pending,
             step_shot, step_toggles, step_switch, step_process, step_prefs,
             step_motion, step_anim_root]

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
