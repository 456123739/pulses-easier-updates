"""
win_native_probe.py — Windows 真机窗口验收（不依赖浏览器模拟）
================================================================
启动打包后的 exe，检查：
1. 启动轨迹：先 240 小窗，再在若干帧内长到 >=900x560（不是"卡在小窗"）；
2. 圆角：GetWindowRgn 确实设置了区域，且 (1,1) 不在区域内、中心在；
3. 像素：窗口四角颜色 != 中心颜色（圆角外是桌面），并保存截图；
4. 控件：点自绘按钮 最大化/还原/最小化/关闭，以及拖右边缘 resize；
5. 页面就绪只作为附带信息，不再单独作为"通过"依据。

任一失败 → 退出码 1；全部通过 → 0。只在 Windows 上运行。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

TITLE = "Pulses Easier"


def _win32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)

    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = (ctypes.c_void_p,)
    user32.IsZoomed.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (ctypes.c_void_p,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetLayeredWindowAttributes.argtypes = (ctypes.c_void_p,
                                                  ctypes.POINTER(wintypes.DWORD),
                                                  ctypes.POINTER(ctypes.c_ubyte),
                                                  ctypes.POINTER(wintypes.DWORD))
    user32.GetLayeredWindowAttributes.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = (ctypes.c_void_p,)
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT)
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.c_void_p)
    user32.mouse_event.restype = None
    user32.GetWindowRgn.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    user32.GetWindowRgn.restype = ctypes.c_int
    user32.GetDpiForWindow.argtypes = (ctypes.c_void_p,)
    user32.GetDpiForWindow.restype = wintypes.UINT

    gdi32.CreateRectRgn.argtypes = (ctypes.c_int,) * 4
    gdi32.CreateRectRgn.restype = ctypes.c_void_p
    gdi32.PtInRegion.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
    gdi32.PtInRegion.restype = wintypes.BOOL
    gdi32.GetRgnBox.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.RECT))
    gdi32.GetRgnBox.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
    gdi32.DeleteObject.restype = wintypes.BOOL

    return user32, gdi32, dwm


def find_hwnd(timeout: float = 30.0) -> int:
    user32, _g, _d = _win32()
    end = time.time() + timeout
    while time.time() < end:
        hwnd = user32.FindWindowW(None, TITLE)
        if hwnd:
            return int(hwnd)
        time.sleep(0.05)
    return 0


def rect_of(hwnd: int):
    user32, _g, _d = _win32()
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)


def click(x: int, y: int) -> None:
    user32, _g, _d = _win32()
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, None)   # left down
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, None)   # left up


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    user32, _g, _d = _win32()
    user32.SetCursorPos(x1, y1)
    time.sleep(0.08)
    user32.mouse_event(0x0002, 0, 0, 0, None)
    steps = 10
    for i in range(1, steps + 1):
        user32.SetCursorPos(int(x1 + (x2 - x1) * i / steps),
                            int(y1 + (y2 - y1) * i / steps))
        time.sleep(0.02)
    user32.mouse_event(0x0004, 0, 0, 0, None)


def grab(path: Path, box=None) -> bool:
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=box) if box else ImageGrab.grab()
        img.save(path)
        return True
    except Exception as exc:                      # noqa: BLE001
        print(f"截图失败 {path}: {exc}")
        return False


def pixel(img_path: Path, x: int, y: int):
    try:
        from PIL import Image
        im = Image.open(img_path).convert("RGB")
        return im.getpixel((x, y))
    except Exception:                              # noqa: BLE001
        return None


class Probe:
    def __init__(self, out: Path):
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.checks: list[dict] = []
        self.metrics: dict = {}
        self.cat = "geometry"
        self.screenshots: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail,
                            "category": self.cat})
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  → {detail}" if detail else ""))
        return bool(ok)

    def save(self) -> dict:
        fails = [c for c in self.checks if not c["ok"]]
        cats = {}
        for cat in ("geometry", "visual", "actions"):
            sub = [c for c in self.checks if c["category"] == cat]
            cats[cat] = {"ok": all(c["ok"] for c in sub), "checks": sub}
        cats["visual"]["screenshots"] = list(self.screenshots)
        report = {"ok": not fails, "checks": self.checks,
                  "geometry": cats["geometry"], "visual": cats["visual"],
                  "actions": cats["actions"],
                  "metrics": self.metrics, "fails": fails}
        (self.out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--out", default="native-verify")
    ap.add_argument("--timeout", type=float, default=40.0)
    args = ap.parse_args()

    if sys.platform != "win32":
        print("win_native_probe 只能在 Windows 上运行")
        return 2
    # 必须 DPI aware：否则 GetWindowRect/ImageGrab/mouse_event 三者坐标系不一致，
    # 截图会截错区域、按钮也会点偏（CI 150% 缩放下尤其明显）
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # PER_MONITOR_AWARE_V2
    except Exception:                                # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:                            # noqa: BLE001
            pass
    exe = Path(args.exe).resolve()
    if not exe.is_file():
        print(f"找不到 exe：{exe}")
        return 2

    probe = Probe(Path(args.out))
    user32, gdi32, _dwm = _win32()
    log = probe.out / "app.log"
    env = {**os.environ, "PULSES_WEB_LOG": str(log)}
    env.pop("PULSES_UI", None)
    env.pop("PULSES_SPLASH", None)
    proc = subprocess.Popen([str(exe)], env=env)

    t0 = time.time()
    hwnd = 0
    while time.time() - t0 < args.timeout:
        hwnd = find_hwnd(0.2)
        if hwnd:
            break
        if proc.poll() is not None:
            break
    probe.metrics["hwnd_ms"] = int((time.time() - t0) * 1000)
    if not probe.check("窗口在 40s 内出现", bool(hwnd), f"hwnd={hwnd or 'none'}"):
        proc.kill()
        return probe.save() and 1

    # 等窗口上屏（启动窗居中，正常立即满足）
    t_vis = time.time()
    while time.time() - t_vis < 20:
        rr = rect_of(hwnd)
        if rr and rr[0] > -1000 and rr[1] > -1000:
            break
        time.sleep(0.05)
    probe.metrics["onscreen_ms"] = int((time.time() - t_vis) * 1000)
    rr = rect_of(hwnd) or (0, 0, 0, 0)
    probe.check("窗口在 20s 内上屏", rr[0] > -1000 and rr[1] > -1000,
                f"rect={rr}")

    user32.SetForegroundWindow(hwnd)

    # ---- 启动轨迹 + alpha 淡入：一起采样，避免等淡入时错过 240 小窗 ----
    samples: list[dict] = []
    splash_png = probe.out / "01-splash.png"
    expand_png = probe.out / "01b-expand.png"
    splash_saved = False
    expand_saved = False
    alpha = 0
    visible = False
    t_start = time.time()
    while time.time() - t_start < 20:
        r = rect_of(hwnd)
        key = wintypes.DWORD()
        flags = wintypes.DWORD()
        a = ctypes.c_ubyte(0)
        alpha = int(a.value)
        if user32.GetLayeredWindowAttributes(hwnd, ctypes.byref(key),
                                             ctypes.byref(a), ctypes.byref(flags)):
            alpha = int(a.value)
        visible = bool(user32.IsWindowVisible(hwnd))
        if r:
            samples.append({"t": round(time.time() - t_start, 3),
                            "x": r[0], "y": r[1], "w": r[2], "h": r[3],
                            "alpha": alpha})
            if alpha >= 250 and r[2] <= 420 and not splash_saved:
                time.sleep(0.35)                     # 淡入完成后等首帧 logo 画出来
                grab(splash_png, (r[0], r[1], r[0] + r[2], r[1] + r[3]))
                probe.metrics["splash_rect"] = r
                probe.screenshots.append(str(splash_png))
                splash_saved = True
            if 420 < r[2] < 900 and not expand_saved:
                grab(expand_png, (r[0], r[1], r[0] + r[2], r[1] + r[3]))
                probe.screenshots.append(str(expand_png))
                expand_saved = True
        time.sleep(0.04)
        if (r and alpha >= 250 and r[2] >= 900 and r[3] >= 560
                and len(samples) > 8
                and len({(q["w"], q["h"]) for q in samples[-6:]}) == 1):
            break
    probe.metrics["samples"] = samples[:200]
    probe.metrics["alpha_wait_ms"] = int((time.time() - t_start) * 1000)
    widths = [q["w"] for q in samples]
    final_w, final_h = (samples[-1]["w"], samples[-1]["h"]) if samples else (0, 0)
    probe.check("窗口真正可见", visible, f"visible={visible}")
    probe.check("窗口在 25s 内淡入（alpha=255）", alpha >= 250, f"alpha={alpha}")
    probe.check("先出现小窗（<=420px）", bool(samples) and widths[0] <= 420,
                f"first={widths[0] if widths else 'none'}")
    probe.check("展开到主窗口尺寸（>=900x560）",
                final_w >= 900 and final_h >= 560,
                f"final={final_w}x{final_h}")
    distinct = len(set(widths))
    probe.check("展开是连续动画（>=6 个不同宽度）", distinct >= 6,
                f"distinct={distinct}")
    probe.cat = "visual"
    if expand_png.is_file():
        try:
            from PIL import Image
            im = Image.open(expand_png).convert("RGB")
            ew, eh = im.size
            mid = im.getpixel((ew // 2, eh // 2))
            # 展开时 logo 应已模糊淡出成 #121214 底色，不能是被拉大的 logo。
            bg_ok = all(abs(int(a) - int(b)) <= 8
                        for a, b in zip(mid, (18, 18, 20)))
            probe.check("展开中 logo 已淡出为纯色底", bg_ok,
                        f"center={mid} size={ew}x{eh}")
        except Exception as exc:                  # noqa: BLE001
            probe.check("展开中 logo 已淡出为纯色底", False, str(exc))
    if not splash_saved:
        # 兜底：至少留一张窗口图，避免"缺截图也算过"
        r = rect_of(hwnd)
        if r:
            grab(splash_png, (r[0], r[1], r[0] + r[2], r[1] + r[3]))
    probe.check("启动/展开截图已保存", splash_png.is_file(), str(splash_png))

    # ---- 圆角区域 ----
    rgn = gdi32.CreateRectRgn(0, 0, 0, 0)
    kind = user32.GetWindowRgn(hwnd, rgn)
    box = wintypes.RECT()
    gdi32.GetRgnBox(rgn, ctypes.byref(box))
    corner_in = bool(gdi32.PtInRegion(rgn, 1, 1))
    center_in = bool(gdi32.PtInRegion(rgn, max(2, final_w // 2), max(2, final_h // 2)))
    gdi32.DeleteObject(rgn)
    probe.metrics["rgn"] = {"kind": kind,
                            "box": [box.left, box.top, box.right, box.bottom],
                            "corner_in": corner_in, "center_in": center_in}
    probe.check("窗口设置了圆角区域", kind in (2, 3), f"kind={kind}")
    probe.check("左上角像素在区域外", not corner_in, f"corner_in={corner_in}")
    probe.check("中心在区域内", center_in, f"center_in={center_in}")

    probe.cat = "visual"
    # ---- 截图与像素（主界面）----
    time.sleep(1.2)                      # 等 Shell 渲染出标题栏
    r = rect_of(hwnd)
    x, y, w, h = r
    final_png = probe.out / "02-main.png"
    grab(final_png, (x, y, x + w, y + h))
    corner = pixel(final_png, 1, 1)
    center = pixel(final_png, max(2, w // 2), int((34 + 40) * max(1.0, float(user32.GetDpiForWindow(hwnd)) / 96.0)))
    probe.metrics["pixels"] = {"corner": corner, "center": center}
    if final_png.is_file():
        probe.screenshots.append(str(final_png))
    probe.check("截图像素可读", corner is not None and center is not None,
                f"{corner} / {center}")
    probe.check("圆角外与窗口内颜色不同", bool(corner and center and corner != center),
                f"{corner} vs {center}")
    # 自绘按钮按 CSS 像素 34/8/2 排布，坐标必须**每次按当前 rect** 算：
    # 最大化后窗口宽度变了，沿用最大化前的坐标会点空。
    def btn(kind: str) -> tuple[int, int]:
        rr = rect_of(hwnd)
        sc = max(1.0, float(user32.GetDpiForWindow(hwnd)) / 96.0)
        off = {"close": 25, "max": 61, "min": 97}[kind]
        return rr[0] + rr[2] - int(off * sc), rr[1] + int(17 * sc)

    probe.cat = "actions"
    # 最小化/还原/拖拽/关闭前都先激活窗口：ShowWindow(SW_RESTORE) 不会把
    # 窗口带到前台，第一次点击只会被用来激活，按钮/拖拽会"点空"。
    def force_foreground() -> None:
        # SW_RESTORE 不会把窗口带到前台；topmost 切一下再撤，强制激活
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        activate()

    def activate() -> None:
        rr = rect_of(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        click(rr[0] + min(80, rr[2] // 3), rr[1] + int(17 * scale0(rr)))
        time.sleep(0.2)

    def scale0(rr) -> float:
        return max(1.0, float(user32.GetDpiForWindow(hwnd)) / 96.0)

    activate()
    # ---- 拖右边缘 resize：先缩再放，避免顶到屏幕边界 ----
    force_foreground()
    before = rect_of(hwnd)
    # 拖到 900 最小宽度再放大：跨度大，mouse_event 的中间移动不会被丢掉
    drag(before[0] + before[2] - 2, before[1] + before[3] // 2,
         before[0] + before[2] - 140, before[1] + before[3] // 2)
    time.sleep(0.8)
    shrunk = rect_of(hwnd)
    drag(shrunk[0] + shrunk[2] - 2, shrunk[1] + shrunk[3] // 2,
         shrunk[0] + shrunk[2] + 140, shrunk[1] + shrunk[3] // 2)
    time.sleep(0.8)
    grown = rect_of(hwnd)
    probe.metrics["resize"] = {"before": before, "shrunk": shrunk, "grown": grown}
    probe.check("拖右边缘可缩小", shrunk[2] <= before[2] - 30,
                f"{before[2]} -> {shrunk[2]}")
    probe.check("拖右边缘可放大", grown[2] >= shrunk[2] + 40,
                f"{shrunk[2]} -> {grown[2]}")

    # ---- 最大化 / 还原 ----
    mx, my = btn("max")
    click(mx, my)
    time.sleep(0.8)
    probe.check("点最大化按钮生效", bool(user32.IsZoomed(hwnd)),
                f"IsZoomed={bool(user32.IsZoomed(hwnd))}")
    grab(probe.out / "03-maximized.png")
    probe.screenshots.append(str(probe.out / "03-maximized.png"))
    mx, my = btn("max")                 # 最大化后按钮在屏幕右侧
    click(mx, my)
    time.sleep(0.9)
    probe.check("点还原按钮复原", not bool(user32.IsZoomed(hwnd)),
                f"IsZoomed={bool(user32.IsZoomed(hwnd))}")
    grab(probe.out / "04-restored.png")
    probe.screenshots.append(str(probe.out / "04-restored.png"))

    # ---- 最小化 ----
    force_foreground()
    mnx, mny = btn("min")
    click(mnx, mny)
    time.sleep(0.7)
    if not bool(user32.IsIconic(hwnd)):          # 首次点击可能只激活窗口
        mnx, mny = btn("min")
        click(mnx, mny)
        time.sleep(0.7)
    probe.check("点最小化按钮生效", bool(user32.IsIconic(hwnd)),
                f"IsIconic={bool(user32.IsIconic(hwnd))}")
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    time.sleep(0.9)
    activate()

    # ---- 关闭：换一个新实例测（最小化/还原后窗口的前台状态不可靠，
    # 容易把"第一次点击只是激活"误判成按钮失效）----
    try:
        proc.kill()
        proc.wait(timeout=8)
    except Exception:                            # noqa: BLE001, S110
        pass
    proc2 = subprocess.Popen([str(exe)], env=env)
    hw2 = find_hwnd(30)
    ok_exit = False
    if hw2:
        # 等页面画出主界面（alpha=255 且 >=900x560），再点关闭
        end_wait = time.time() + 45
        while time.time() < end_wait:
            rr = rect_of(hw2)
            key = wintypes.DWORD(); flags = wintypes.DWORD(); a = ctypes.c_ubyte(0)
            user32.GetLayeredWindowAttributes(hw2, ctypes.byref(key),
                                              ctypes.byref(a), ctypes.byref(flags))
            if rr and rr[2] >= 900 and int(a.value) >= 250:
                break
            time.sleep(0.1)
        # WebView2 冷启动时窗口已 alpha=255 但页面还是空的；
        # 等页面真正画出标题栏再点关闭，否则点到空白处会误判。
        time.sleep(4)
        user32.SetForegroundWindow(hw2)
        time.sleep(0.5)
        for off in (25, 16, 34, 25, 16, 34):
            rr = rect_of(hw2)
            if not rr:
                break
            sc = max(1.0, float(user32.GetDpiForWindow(hw2)) / 96.0)
            click(rr[0] + rr[2] - int(off * sc), rr[1] + int(17 * sc))
            end_click = time.time() + 3
            while time.time() < end_click:
                if proc2.poll() is not None:
                    ok_exit = True
                    break
                time.sleep(0.15)
            if ok_exit:
                break
    probe.check("新实例点关闭按钮进程退出", ok_exit, f"exit={proc2.poll()}")
    if not ok_exit:
        proc2.kill()

    # 页面就绪只记录
    probe.metrics["page_ready"] = log.is_file() and "page-ready" in log.read_text(
        encoding="utf-8", errors="ignore")
    report = probe.save()
    print(f"\n{len(report['fails'])} 项失败；报告：{probe.out / 'report.json'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
