"""
window_shell.py — 无边框窗口的开始尺寸、居中与展开动画
------------------------------------------------
界面第一帧只显示一个圆角 logo 小窗；后台把状态准备好之后，窗口自身
从 logo 大小平滑长到主窗口尺寸（圆角弧度不变，看起来像 logo 展开）。

这里只放"算尺寸 / 动窗口"的纯逻辑：不 import webview，方便单测。
`window` 只需要有 resize(w, h) / move(x, y) 两个方法（pywebview 的 Window
就是这两个），测试里用假对象即可。
"""

from __future__ import annotations

import sys
import time

SPLASH_W, SPLASH_H = 300, 300
RADIUS = 14
TARGET_DEFAULT = (1440, 860)
MIN_SIZE = (900, 560)


def screen_size() -> tuple[int, int]:
    """屏幕分辨率；拿不到就按 1280×800 处理（只影响窗口位置，不影响布局）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            sw, sh = int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
            if sw > 200 and sh > 200:
                return sw, sh
        except Exception:                          # noqa: BLE001, S110
            pass
    try:                                            # 其他平台：Tk 能问到就用
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        size = (int(root.winfo_screenwidth()), int(root.winfo_screenheight()))
        root.destroy()
        if size[0] > 200 and size[1] > 200:
            return size
    except Exception:                               # noqa: BLE001, S110
        pass
    return 1280, 800


def center_rect(w: int, h: int, screen: tuple[int, int] | None = None) -> tuple[int, int]:
    sw, sh = screen or screen_size()
    return max(0, (sw - w) // 2), max(0, (sh - h) // 3)


def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def expand(window, target: tuple[int, int],
           duration: float = 0.46, steps: int = 26,
           screen: tuple[int, int] | None = None,
           sleep=time.sleep) -> tuple[int, int]:
    """
    把小窗平滑放大到 target 并居中；返回最终尺寸。

    每帧都调 window.resize + window.move —— pywebview 会把它们排到 GUI 线程，
    26 帧约 18ms 一帧，肉眼看是连续的展开而不是"突然变大"。
    """
    sw, sh = screen or screen_size()
    tw = min(int(target[0]), max(MIN_SIZE[0], sw - 80))
    th = min(int(target[1]), max(MIN_SIZE[1], sh - 100))
    sw0, sh0 = SPLASH_W, SPLASH_H
    for i in range(1, steps + 1):
        t = _ease_out_cubic(i / steps)
        w = int(round(sw0 + (tw - sw0) * t))
        h = int(round(sh0 + (th - sh0) * t))
        x, y = center_rect(w, h, (sw, sh))
        try:
            window.resize(w, h)
            window.move(x, y)
        except Exception:                             # noqa: BLE001, S110
            break
        sleep(max(0.0, duration / steps))
    try:
        window.min_size = MIN_SIZE                    # 展开后再限制最小尺寸
    except Exception:                                 # noqa: BLE001, S110
        pass
    apply_window_rounding(window)                     # 区域裁剪方案要按新尺寸重设
    return tw, th


def compute_resize(edge: str, start: dict, dx: int, dy: int,
                   min_size: tuple[int, int] = MIN_SIZE
                   ) -> dict:
    """
    拖边/拖角时的目标几何：`start` = {x,y,width,height}（屏幕坐标）。
    `edge` 取 n/s/e/w/ne/nw/se/sw。返回 {x,y,width,height}，已夹住最小尺寸。
    """
    x, y = start["x"], start["y"]
    w, h = start["width"], start["height"]
    min_w, min_h = min_size
    if "e" in edge:
        w = max(min_w, w + dx)
    if "s" in edge:
        h = max(min_h, h + dy)
    if "w" in edge:
        new_w = max(min_w, w - dx)
        x = x + (w - new_w)
        w = new_w
    if "n" in edge:
        new_h = max(min_h, h - dy)
        y = y + (h - new_h)
        h = new_h
    return {"x": x, "y": y, "width": w, "height": h}


# ----------------------------------------------------------------------
# Windows 圆角：优先 DWM（Win11，抗锯齿、随窗口自动更新），
# 退而求其次用 SetWindowRgn（Win10，区域裁剪，缩放后要重设）。
# ----------------------------------------------------------------------
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3


def _win_hwnd(window, title: str = "Pulses Easier") -> int:
    if sys.platform != "win32":
        return 0
    try:
        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is not None:
            try:
                return int(handle.ToInt64())
            except Exception:                        # noqa: BLE001
                return int(handle)
    except Exception:                                # noqa: BLE001
        pass
    try:
        import ctypes
        return int(ctypes.windll.user32.FindWindowW(None, title) or 0)
    except Exception:                                # noqa: BLE001
        return 0


def _dwm_round(hwnd: int, small: bool = False) -> bool:
    try:
        import ctypes
        pref = ctypes.c_int(DWMWCP_ROUNDSMALL if small else DWMWCP_ROUND)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref))
        return res == 0
    except Exception:                                # noqa: BLE001
        return False


def _region_round(window, radius: int = 14) -> bool:
    """Win10 兜底：用圆角区域裁窗口；窗口缩放后必须重调（调用方负责）。"""
    try:
        import ctypes
        hwnd = _win_hwnd(window)
        if not hwnd:
            return False
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return False
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
            0, 0, w + 1, h + 1, radius * 2, radius * 2)
        if not rgn:
            return False
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        return True
    except Exception:                                # noqa: BLE001
        return False


def apply_window_rounding(window, radius: int = 14) -> str:
    """
    给无边框窗口加圆角。返回用了哪种方式（"dwm" / "region" / ""），
    方便日志和 CI 断言；失败不影响启动。
    """
    if sys.platform != "win32":
        return ""
    hwnd = _win_hwnd(window)
    if not hwnd:
        return ""
    # 区域裁剪优先：DWM 圆角在 Windows Server / 关闭视觉效果时不生效，
    # 不能作为"有没有圆角"的验收依据；区域裁剪在所有 Windows 上都可见。
    if _region_round(window, radius):
        return "region"
    if _dwm_round(hwnd):
        return "dwm"
    return ""
