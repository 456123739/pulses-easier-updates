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
