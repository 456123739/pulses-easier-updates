"""
win11.py — Windows 11 窗口材质 / 透明度适配
================================================
真机现象：Win11 上窗口会自己变成半透明（大约 15%~30%），壁纸直接透进来，
而且**一旦出现就再也回不去**（弹出子窗口、切一下焦点更明显）。

两个根因，都要治：

  1. **customtkinter 的 DPI 轮询会闪 alpha**
     `ScalingTracker.check_dpi_scaling()` 每 100ms 跑一次；发现 DPI 变化就
         window.attributes("-alpha", 0.15) → 更新缩放 → attributes("-alpha", 1)
     Win11 会把这当成"窗口材质变了"，重新评估 Mica/亚克力；而且只要中途
     有一个缩放回调抛异常，alpha 就永远停在 0.15（看起来就是 15%~30% 透明）。
     → 这里换掉这个方法：**保留 DPI 缩放，但不再动 alpha**。

  2. **Win11 默认给窗口套 Mica / Acrylic**
     `DWMWA_SYSTEMBACKDROP_TYPE` 默认是 0（自动），系统会自己挑材质；
     即使 alpha=1，背景也会透出壁纸。
     → 用 DWM 显式设成 1（DWMSBT_NONE），并关掉旧的 1029(MICA_EFFECT)。

另外：DWM 在**失焦、映射、弹窗置顶、切主题**时会重新评估材质，
所以只设一次不够 —— 事件驱动 + 兜底轮询持续重申"不透明"。

（方案来自同工作区的 pulses-swap：`platform.lock_ctk_scaling` /
  `make_window_opaque` / `install_opacity_guard`，这里按 Easier 的深色主题改写。）
"""

import ctypes
import sys
import time
from collections.abc import Callable

IS_WINDOWS = sys.platform.startswith("win")

# DWM 属性编号（Win11 SDK / 未公开常量）
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_SYSTEMBACKDROP_TYPE = 38        # 0=自动 1=无 2=Mica 3=Acrylic 4=Tabbed
DWMWA_MICA_EFFECT = 1029              # Win11 早期版本的未公开开关

DWMSBT_NONE = 1
DWMWCP_ROUND = 2

_WIN32_ATTRS: list[tuple[int, str]] = []   # 供自检用：(attr, "caption"/"text"/"border")


def _color_ref(hex_color: str) -> int:
    """'#RRGGBB' → Win32 COLORREF（0x00BBGGRR）；解析失败返回 -1。"""
    try:
        text = str(hex_color).lstrip("#")
        if len(text) != 6:
            return -1
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
        return (b << 16) | (g << 8) | r
    except Exception:  # noqa: BLE001
        return -1


def _dwm_set(hwnd, attr: int, value: int) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        ref = ctypes.c_int(int(value))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, attr, ctypes.byref(ref), ctypes.sizeof(ref))
        return True
    except Exception:  # noqa: BLE001
        return False


def _hwnd(window) -> int:
    """取顶层窗口句柄（Tk 的子窗口需要先 GetParent）。"""
    try:
        wid = int(window.winfo_id())
    except Exception:  # noqa: BLE001
        return 0
    try:
        parent = ctypes.windll.user32.GetParent(wid)
        return int(parent) or wid
    except Exception:  # noqa: BLE001
        return wid


def apply_opaque_attributes(hwnd, window_bg: str, text_color: str,
                            border_color: str) -> None:
    """把 DWM 材质设成"无"，标题栏/边框/文字按主题上色。"""
    _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
    _dwm_set(hwnd, DWMWA_MICA_EFFECT, 0)
    # 深色标题栏（Easier 只有深色主题）。不设的话 DWM 会按系统主题给标题栏
    # 铺一层材质底，同样会透出壁纸，而且和我们的 #1E1E1E 对不上。
    _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    for attr, color in ((DWMWA_CAPTION_COLOR, window_bg),
                        (DWMWA_TEXT_COLOR, text_color),
                        (DWMWA_BORDER_COLOR, border_color)):
        value = _color_ref(color)
        if value >= 0:
            _dwm_set(hwnd, attr, value)
    _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)


def make_opaque(window) -> bool:
    """
    把一个窗口设成完全不透明（Tk alpha + DWM 材质 + 标题栏配色）。

    可反复调用：DWM 会在失焦/映射/弹窗时重新评估材质，必须能重申。
    """
    if not IS_WINDOWS:
        return False
    # 设计上就要透明的窗口（启动页用 chroma key 做圆角/异形）绝不能碰：
    # 清掉 transparentcolor 会让它变成一整块实心矩形，alpha 也会打断淡入淡出
    try:
        if window.attributes("-transparentcolor"):
            return False
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        window.attributes("-alpha", 1.0)
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        window.attributes("-transparentcolor", "")
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        from ..theme import Color
        window_bg = Color.WINDOW_BG
        text = Color.TEXT_PRIMARY
        border = Color.BORDER
    except Exception:  # noqa: BLE001
        window_bg, text, border = "#1E1E1E", "#E8E8E8", "#3E3E42"
    hwnd = _hwnd(window)
    if not hwnd:
        return False
    apply_opaque_attributes(hwnd, window_bg, text, border)
    return True


def refresh(root, skip: Callable[[object], bool] | None = None) -> int:
    """
    主窗口 + 所有 Toplevel 子窗口重新刷一遍"不透明"，返回处理数量。

    `skip(window)` 返回 True 的窗口会跳过 —— 正在播出入场动画的窗口
    不能被守卫把 alpha 拽回 1.0。
    """
    if not IS_WINDOWS:
        return 0

    def _skip(w) -> bool:
        if skip is None:
            return False
        try:
            return bool(skip(w))
        except Exception:  # noqa: BLE001
            return False

    count = 0
    if not _skip(root) and make_opaque(root):
        count += 1
    try:
        for child in root.winfo_children():
            try:
                if child.winfo_class() != "Toplevel" or _skip(child):
                    continue
                if make_opaque(child):
                    count += 1
            except Exception:  # noqa: BLE001, S110
                continue
    except Exception:  # noqa: BLE001, S110
        pass
    return count


# ----------------------------------------------------------------------
# 拆掉 CTk DPI 轮询里的 alpha 闪切（保留缩放）
# ----------------------------------------------------------------------
_ctk_patched = False


def neutralize_ctk_alpha_flash() -> bool:
    """
    换掉 customtkinter 的 `ScalingTracker.check_dpi_scaling`。

    原版在 Windows 上会 `attributes("-alpha", 0.15)` → 改缩放 → `1`，
    这是 Win11 半透明的直接来源。替换版做同样的事，**但不碰 alpha**；
    每个窗口/回调都单独 try，任何一个抛异常都不会中断其它窗口。
    """
    global _ctk_patched
    if _ctk_patched or not IS_WINDOWS:
        return _ctk_patched
    try:
        import customtkinter as ctk
        tracker = ctk.ScalingTracker
    except Exception:  # noqa: BLE001
        return False

    def _check(cls):
        """保缩放、去 alpha 的 check_dpi_scaling（行为与 CTk 对齐）。"""
        new_scaling_detected = False
        for window in list(getattr(cls, "window_widgets_dict", {})):
            try:
                if not window.winfo_exists() or window.state() == "iconic":
                    continue
                current = cls.get_window_dpi_scaling(window)
                if current == cls.window_dpi_scaling_dict.get(window):
                    continue
                cls.window_dpi_scaling_dict[window] = current
                try:
                    window.block_update_dimensions_event()
                except Exception:  # noqa: BLE001, S110
                    pass
                try:
                    cls.update_scaling_callbacks_for_window(window)
                except Exception:  # noqa: BLE001, S110
                    pass
                try:
                    window.unblock_update_dimensions_event()
                except Exception:  # noqa: BLE001, S110
                    pass
                new_scaling_detected = True
            except Exception:  # noqa: BLE001, S110
                continue
        # 续订下一次（与原实现一致；找不到窗口就停掉循环）
        for app in list(getattr(cls, "window_widgets_dict", {})):
            try:
                delay = (cls.loop_pause_after_new_scaling if new_scaling_detected
                         else cls.update_loop_interval)
                app.after(delay, cls.check_dpi_scaling)
                return
            except Exception:  # noqa: BLE001, S110
                continue
        try:
            cls.update_loop_running = False
        except Exception:  # noqa: BLE001, S110
            pass

    try:
        tracker.check_dpi_scaling = classmethod(_check)
        _ctk_patched = True
    except Exception:  # noqa: BLE001
        _ctk_patched = False
    return _ctk_patched


def is_patched() -> bool:
    return _ctk_patched


# ----------------------------------------------------------------------
# 不透明度守卫：事件驱动 + 兜底轮询
# ----------------------------------------------------------------------
class OpacityGuard:
    """
    持续重申"窗口不透明"。

    - `<Map> / <FocusIn> / <Visibility> / <Configure>` 触发时补一次（250ms 节流）
    - 每 3 秒兜底一次（DWM 可能在任何时机重新评估材质）
    - 窗口动画正在写 alpha 时**跳过**，否则会把淡入淡出拽回 1.0
    """

    INTERVAL_MS = 3000
    THROTTLE_S = 0.25

    def __init__(self, root, is_animating: Callable[[object], bool] | None = None):
        self.root = root
        self.is_animating = is_animating or (lambda _w=None: False)
        self._last = 0.0
        self._after_id = None
        self._applied = 0
        self._logged = False

    # -- 对外 ---------------------------------------------------------
    def install(self) -> "OpacityGuard":
        if not IS_WINDOWS:
            return self
        for seq in ("<Map>", "<FocusIn>", "<Visibility>", "<Configure>"):
            try:
                self.root.bind(seq, self._on_event, add="+")
            except Exception:  # noqa: BLE001, S110
                pass
        self.apply(force=True)
        self._schedule()
        return self

    def apply(self, force: bool = False) -> int:
        if not IS_WINDOWS:
            return 0
        now = time.perf_counter()
        if not force and now - self._last < self.THROTTLE_S:
            return 0
        self._last = now
        try:
            self._applied = refresh(self.root, skip=self._animating)
        except Exception:  # noqa: BLE001
            self._applied = 0
        return self._applied

    @property
    def applied(self) -> int:
        return self._applied

    def stop(self):
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001, S110
                pass
            self._after_id = None

    # -- 内部 ---------------------------------------------------------
    def _animating(self, window=None) -> bool:
        try:
            return bool(self.is_animating(window))
        except Exception:  # noqa: BLE001
            return False

    def _on_event(self, _event=None):
        self.apply()

    def _schedule(self):
        try:
            self._after_id = self.root.after(self.INTERVAL_MS, self._tick)
        except Exception:  # noqa: BLE001
            self._after_id = None

    def _tick(self):
        self._after_id = None
        try:
            if not self.root.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        self.apply(force=True)
        self._schedule()
