"""
window_shell.py — 无边框窗口：启动尺寸、圆角、展开动画
================================================================
第一帧是**和 logo 一样大的圆角小窗**；状态准备好后窗口自身 ease-out 长到
主窗口尺寸。圆角用 Win32 区域裁剪（SetWindowRgn）：

* 半径固定 12px，窗口放大/缩小时每帧重设，所以弧度看起来不变；
* 圆角外是真正的桌面（像素不属于窗口），不依赖 WebView2 透明；
* 64 位下 ctypes 显式声明 argtypes/restype，避免 HWND/HRGN 被截断；
* 句柄只用 FindWindowW 按标题找，绝不从后台线程碰 window.native
  （.NET 窗体只能在 UI 线程访问，否则会刷 COM 递归错误）。

这里不 import webview，方便单测。
"""

from __future__ import annotations

import ctypes
import math
import sys
import time

SPLASH_W, SPLASH_H = 240, 240
RADIUS_PX = 12
TARGET_DEFAULT = (1440, 860)
MIN_SIZE = (900, 560)
WINDOW_TITLE = "Pulses Easier"

_W32 = None


def screen_size() -> tuple[int, int]:
    if sys.platform == "win32":
        try:
            user32 = ctypes.WinDLL("user32")
            user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
            user32.GetSystemMetrics.restype = ctypes.c_int
            sw = int(user32.GetSystemMetrics(0))
            sh = int(user32.GetSystemMetrics(1))
            if sw > 200 and sh > 200:
                return sw, sh
        except Exception:                          # noqa: BLE001, S110
            pass
    try:
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


def _win32():
    global _W32
    if _W32 is not None:
        return _W32
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.GetWindowRect.argtypes = (ctypes.c_void_p,
                                     ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SetWindowRgn.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.BOOL)
    user32.SetWindowRgn.restype = ctypes.c_int
    user32.IsZoomed.argtypes = (ctypes.c_void_p,)
    user32.IsZoomed.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (ctypes.c_void_p,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_longlong)
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetLayeredWindowAttributes.argtypes = (ctypes.c_void_p,
                                                  wintypes.DWORD,
                                                  ctypes.c_ubyte,
                                                  wintypes.DWORD)
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_uint)
    user32.SetWindowPos.restype = wintypes.BOOL

    gdi32.CreateRoundRectRgn.argtypes = (ctypes.c_int,) * 6
    gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
    gdi32.DeleteObject.restype = wintypes.BOOL

    _W32 = (user32, gdi32, ctypes, wintypes)
    return _W32


def find_hwnd(title: str = WINDOW_TITLE, timeout: float = 0.4) -> int:
    if sys.platform != "win32":
        return 0
    user32, _g, _c, _w = _win32()
    end = time.time() + max(0.0, timeout)
    while True:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return int(hwnd)
        if time.time() >= end:
            return 0
        time.sleep(0.05)


def _window_rect(hwnd: int):
    _u, _g, ctypes_mod, wintypes = _win32()
    rect = wintypes.RECT()
    if not _u.GetWindowRect(hwnd, ctypes_mod.byref(rect)):
        return None
    w = int(rect.right - rect.left)
    h = int(rect.bottom - rect.top)
    if w <= 0 or h <= 0:
        return None
    return int(rect.left), int(rect.top), w, h


def _set_region(hwnd: int, radius: int = RADIUS_PX,
                size: tuple[int, int] | None = None,
                redraw: bool = True) -> bool:
    _u, gdi32, _c, _w = _win32()
    if size is None:
        rect = _window_rect(hwnd)
        if not rect:
            return False
        w, h = rect[2:]
    else:
        w, h = size
    rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1,
                                   radius * 2, radius * 2)
    if not rgn:
        return False
    if not _u.SetWindowRgn(hwnd, rgn, redraw):
        gdi32.DeleteObject(rgn)
        return False
    return True


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002


def set_window_alpha(alpha: int) -> bool:
    """整窗透明度 0..255（WS_EX_LAYERED）。加载期间 alpha=0，
    页面画出 logo 后再淡入，用户不会先看到 WebView2 的黑底。"""
    if sys.platform != "win32":
        return False
    hwnd = find_hwnd(timeout=0.3)
    if not hwnd:
        return False
    user32, _g, _c, _w = _win32()
    ex = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
    if not ex & WS_EX_LAYERED:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
    return bool(user32.SetLayeredWindowAttributes(
        hwnd, 0, max(0, min(255, int(alpha))), LWA_ALPHA))


def fade_in(window=None, duration: float = 0.18, sleep=time.sleep) -> None:
    steps = 8
    for i in range(1, steps + 1):
        set_window_alpha(int(255 * i / steps))
        sleep(duration / steps)


def _set_window_rect(hwnd: int, x: int, y: int, w: int, h: int) -> bool:
    """一次 SetWindowPos 完成移动+缩放，避免每帧两次 pywebview 调用。"""
    _u, _g, _c, _w = _win32()
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    return bool(_u.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h),
                                SWP_NOZORDER | SWP_NOACTIVATE))


def apply_window_rounding(window=None, radius: int = RADIUS_PX) -> str:
    if sys.platform != "win32":
        return ""
    hwnd = find_hwnd(timeout=0.4)
    if not hwnd:
        return ""
    user32, _g, _c, _w = _win32()
    if user32.IsZoomed(hwnd):
        return ""
    return "region" if _set_region(hwnd, radius) else ""


def is_maximized() -> bool:
    if sys.platform != "win32":
        return False
    hwnd = find_hwnd(timeout=0.3)
    if not hwnd:
        return False
    user32, _g, _c, _w = _win32()
    return bool(user32.IsZoomed(hwnd))


def clear_window_rounding() -> None:
    if sys.platform != "win32":
        return
    hwnd = find_hwnd(timeout=0.3)
    if not hwnd:
        return
    user32, _g, _c, _w = _win32()
    user32.SetWindowRgn(hwnd, None, True)


def expand(window, target: tuple[int, int],
           duration: float = 0.56, steps: int | None = None,
           screen: tuple[int, int] | None = None,
           sleep=time.sleep, radius: int = RADIUS_PX,
           clock=time.perf_counter) -> tuple[int, int]:
    sw, sh = screen or screen_size()
    tw = min(int(target[0]), max(MIN_SIZE[0], sw - 80))
    th = min(int(target[1]), max(MIN_SIZE[1], sh - 100))
    sw0, sh0 = SPLASH_W, SPLASH_H
    hwnd = find_hwnd(timeout=3.0) if sys.platform == "win32" else 0
    duration = max(0.0, duration)
    frames = max(1, steps if steps is not None else math.ceil(duration * 60))
    started = clock()
    i = 1
    while i <= frames:
        # 以实际时间推进；原生 resize 的耗时也包含在预算内，不在每帧后累加 sleep。
        # 落后的帧直接追上进度，不能让慢机器在旧尺寸上排队。
        scheduled = duration * i / frames
        delay = started + scheduled - clock()
        if delay > 0:
            sleep(delay)
        elapsed = max(scheduled, clock() - started)
        progress = min(1.0, elapsed / duration) if duration else i / frames
        t = _ease_out_cubic(progress)
        w = int(round(sw0 + (tw - sw0) * t))
        h = int(round(sh0 + (th - sh0) * t))
        x, y = center_rect(w, h, (sw, sh))
        try:
            if hwnd:
                # 区域若每四帧才长一次，会把中间三帧的新窗口裁掉。
                # 复用已算好的尺寸，不再 GetWindowRect；resize 自己负责重绘。
                _set_region(hwnd, radius, size=(w, h), redraw=False)
                _set_window_rect(hwnd, x, y, w, h)
            else:
                window.resize(w, h)
                window.move(x, y)
        except Exception:                             # noqa: BLE001, S110
            break
        if progress >= 1:
            break
        i = max(i + 1, math.floor((clock() - started) * frames / duration) + 1) if duration else i + 1
        i = min(i, frames)
    try:
        window.min_size = MIN_SIZE
    except Exception:                                 # noqa: BLE001, S110
        pass
    apply_window_rounding(window, radius)
    return tw, th


def compute_resize(edge: str, start: dict, dx: int, dy: int,
                   min_size: tuple[int, int] = MIN_SIZE) -> dict:
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
