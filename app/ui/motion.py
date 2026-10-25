"""
motion.py — 窗口 / 弹窗的入场与退场动画
================================================
PCL2 的主窗口出现时是"淡入 + 从下方弹上来（EaseOutBack 轻微过冲）+ 一点点旋转"，
关闭时"淡出 + 缩小 + 下沉"。Tk 做不了旋转和真缩放，这里取其中能做的部分：

    进场：透明度 0 → 1，位置从目标下方 rise 处"弹"到目标（带过冲）
    退场：透明度 1 → 0，位置下沉 fall，可选轻微缩小

三条硬约束（都是踩过坑的）：

  1. **动画不承载逻辑**。`on_done` 只用来收尾（例如 destroy），任何"等动画
     结束才能继续"的流程都不允许 —— 否则没有真实窗口的环境（测试桩）会卡死。
  2. **没有真实 GUI / 关掉动画时直接跳到终态**：`on_frame(1.0)` + `on_done()`
     立刻执行一次，调用方不需要写任何 `if can_animate` 分支。
  3. **控件销毁、窗口已关、重复调用都必须安全**：所有 Tk 调用都包了 try，
     同一个窗口上的旧动画会被取消，`on_done` 最多执行一次。

开关：首选项「窗口动画」(`ui.window_anim`)，或环境变量 `PULSES_NO_ANIM=1`。
"""

import os
import re
import sys
import time
from collections.abc import Callable

from ..theme import Anim

_TRUE = {"1", "true", "True", "yes", "on"}


# ----------------------------------------------------------------------
# 环境判断 / 开关
# ----------------------------------------------------------------------
def _real_gui() -> bool:
    """
    是否运行在真实 GUI 环境。

    测试用的桩把 customtkinter 换成了一个 ModuleType（没有 `__file__`）：
    这种环境里排出来的 `after` 回调永远不会执行，所以必须"跳到终态"。
    """
    mod = sys.modules.get("customtkinter")
    return bool(mod) and getattr(mod, "__file__", None) is not None


def is_enabled() -> bool:
    """动画是否生效（环境 + 设置 + 环境变量三重判断）。"""
    try:
        if str(os.environ.get("PULSES_NO_ANIM", "")).strip() in _TRUE:
            return False
    except Exception:  # noqa: BLE001
        pass
    if not _real_gui():
        return False
    try:
        from ..core import database as db
        opts = db.get_ui_options() or {}
        return str(opts.get("window_anim", 1)).strip() in _TRUE
    except Exception:  # noqa: BLE001
        return True


# ----------------------------------------------------------------------
# 缓动
# ----------------------------------------------------------------------
def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_out_back(t: float, overshoot: float = 1.15) -> float:
    """先冲过头一点再回来 —— "弹"的手感就来自这里。"""
    c = overshoot + 1.0
    return 1.0 + c * (t - 1.0) ** 3 + overshoot * (t - 1.0) ** 2


def ease_in_out_cubic(t: float) -> float:
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


# ----------------------------------------------------------------------
# 补间
# ----------------------------------------------------------------------
class Tween:
    """一次补间；`cancel()` 之后不再回调（可选仍收尾一次）。"""

    __slots__ = ("_alive", "_after_id", "_done", "_finished", "widget")

    def __init__(self, widget, after_id=None):
        self.widget = widget
        self._after_id = after_id
        self._alive = True
        self._finished = False
        self._done: Callable[[], None] | None = None

    @property
    def alive(self) -> bool:
        return self._alive

    def cancel(self, notify: bool = False) -> None:
        self._alive = False
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001, S110
                pass
            self._after_id = None
        if notify:
            self.finish()

    def finish(self) -> None:
        """收尾：保证 on_done 只执行一次。"""
        if self._finished:
            return
        self._finished = True
        self._alive = False
        self._after_id = None
        done, self._done = self._done, None
        if done is not None:
            try:
                done()
            except Exception:  # noqa: BLE001, S110
                pass


def _animate(widget, duration_ms: int, frame: Callable[[float], None],
             on_done: Callable[[], None] | None,
             ease: Callable[[float], float] = ease_out_cubic) -> Tween:
    """在 widget 上跑一次补间；不可动画时立刻跳到终态。"""
    tw = Tween(widget)
    tw._done = on_done

    if not is_enabled() or duration_ms <= 0:
        try:
            frame(1.0)
        except Exception:  # noqa: BLE001, S110
            pass
        tw.finish()
        return tw

    started = time.perf_counter()

    def _tick():
        if not tw._alive:
            return
        try:
            if not widget.winfo_exists():
                tw.finish()
                return
        except Exception:  # noqa: BLE001
            tw.finish()
            return
        raw = min(1.0, (time.perf_counter() - started) * 1000.0
                  / float(duration_ms))
        try:
            frame(ease(raw))
        except Exception:  # noqa: BLE001
            tw.finish()
            return
        if raw >= 1.0:
            tw.finish()
            return
        try:
            tw._after_id = widget.after(Anim.FRAME_MS, _tick)
        except Exception:  # noqa: BLE001
            tw.finish()

    try:
        tw._after_id = widget.after(0, _tick)
    except Exception:  # noqa: BLE001
        tw.finish()
    return tw


def _slot(window) -> list:
    """同一个窗口上的动画槽：新动画开始前先把旧的取消掉。"""
    runs = getattr(window, "_motion_runs", None)
    if runs is None:
        runs = []
        try:
            window._motion_runs = runs
        except Exception:  # noqa: BLE001
            return []
    return runs


def cancel_all(window) -> None:
    for tw in list(_slot(window)):
        try:
            tw.cancel()
        except Exception:  # noqa: BLE001, S110
            pass
    try:
        window._motion_runs = []
    except Exception:  # noqa: BLE001, S110
        pass


# ----------------------------------------------------------------------
# 几何 / 透明度
# ----------------------------------------------------------------------
_GEO_RE = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")


def _geometry(window) -> tuple[int, int, int, int] | None:
    """
    读窗口当前几何 (w, h, x, y)；还没布局出来（1x1）时返回 None。

    注意：`geometry()` 在窗口刚创建、还没 update 时可能返回 "1x1+0+0"，
    这时必须先 update_idletasks 再读一次，否则会按 1x1 去"弹"，整窗会崩成一条线。
    """
    try:
        window.update_idletasks()
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        spec = str(window.geometry())
    except Exception:  # noqa: BLE001
        spec = ""
    m = _GEO_RE.match(spec.strip())
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        x, y = int(m.group(3)), int(m.group(4))
        if w > 1 and h > 1:
            return w, h, x, y
    try:                                  # 兜底：直接问控件
        w, h = int(window.winfo_width()), int(window.winfo_height())
        x, y = int(window.winfo_rootx()), int(window.winfo_rooty())
        if w > 1 and h > 1:
            return w, h, x, y
    except Exception:  # noqa: BLE001
        pass
    return None


def _place(window, w: int, h: int, x: int, y: int) -> None:
    try:
        window.geometry(f"{max(1, int(w))}x{max(1, int(h))}"
                        f"+{int(x)}+{int(y)}")
    except Exception:  # noqa: BLE001, S110
        pass


def _alpha(window, value: float) -> None:
    """设置窗口透明度；平台不支持时静默跳过（Linux 无合成器会抛错）。"""
    try:
        window.attributes("-alpha", max(0.0, min(1.0, float(value))))
    except Exception:  # noqa: BLE001, S110
        pass


def prepare(window) -> None:
    """
    在 `deiconify()` / 首次显示**之前**调用：先透明，避免"闪一下再淡入"。
    """
    if not is_enabled():
        return
    _alpha(window, 0.0)


# ----------------------------------------------------------------------
# 进场 / 退场
# ----------------------------------------------------------------------
def pop_in(window, *, rise: int | None = None,
           duration: int | None = None,
           scale_from: float | None = None,
           on_done: Callable[[], None] | None = None) -> Tween:
    """
    窗口出现：淡入 + 从下方弹上来（位置比透明度慢、带过冲）。

    参考 PCL2 的进场：透明度 ~250ms 先到位，Y 位移 600ms 用 EaseOutBack
    慢慢收 —— "弹上来、再稳住"的手感主要来自这两条曲线**不同长**。

    `scale_from` 给一个 0~1 的比例时顺带做"由小到大"（弹窗用；主窗口
    不用，整窗 resize 会触发全量重排）。
    """
    rise = Anim.RISE_PX if rise is None else int(rise)
    fade_ms = Anim.WINDOW_MS if duration is None else int(duration)
    rise_ms = max(fade_ms, Anim.WINDOW_RISE_MS
                  if duration is None else int(duration * 1.6))

    geo = _geometry(window)
    if geo is None:                       # 还没布局出来：不动它
        tw = Tween(window)
        tw._done = on_done
        tw.finish()
        return tw

    w, h, x, y = geo
    cancel_all(window)

    if not is_enabled() or fade_ms <= 0:
        _place(window, w, h, x, y)
        _alpha(window, 1.0)
        tw = Tween(window)
        tw._done = on_done
        tw.finish()
        return tw

    def _fade(t: float):
        _alpha(window, t)

    def _rise(t: float):
        dy = (1.0 - t) * rise
        if scale_from is not None:
            k = scale_from + (1.0 - scale_from) * t
            nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
            nx = x + (w - nw) // 2
            ny = y + (h - nh) // 2 + dy
            _place(window, nw, nh, nx, ny)
        else:
            _place(window, w, h, x, int(round(y + dy)))

    def _done():
        _place(window, w, h, x, y)
        _alpha(window, 1.0)
        if on_done is not None:
            on_done()

    _alpha(window, 0.0)
    tw_fade = _animate(window, fade_ms, _fade, None, ease_out_cubic)
    # 过冲用 1.8（约 10%）：30px 的上浮会多冲 ~3px 再落回，
    # 这才是"弹"的手感；1.15 只有半像素，等于没有
    tw_rise = _animate(window, rise_ms, _rise, _done,
                       lambda t: ease_out_back(t, 1.8))
    _slot(window).extend([tw_fade, tw_rise])
    return tw_rise


def pop_out(window, *, fall: int = 18,
            duration: int | None = None,
            scale_to: float | None = None,
            on_done: Callable[[], None] | None = None) -> Tween:
    """
    窗口消失：淡出 + 下沉（可选轻微缩小），结束后调用 `on_done`（通常 destroy）。

    动画本身失败也一定会调用 `on_done` —— 绝不能"窗口关不掉"。
    """
    duration = Anim.CLOSE_MS if duration is None else int(duration)

    geo = _geometry(window)
    if geo is None:
        tw = Tween(window)
        tw._done = on_done
        tw.finish()
        return tw

    w, h, x, y = geo
    cancel_all(window)

    if not is_enabled() or duration <= 0:
        try:
            _alpha(window, 0.0)
        except Exception:  # noqa: BLE001, S110
            pass
        tw = Tween(window)
        tw._done = on_done
        tw.finish()
        return tw

    def _fade(t: float):
        _alpha(window, 1.0 - t)

    def _drop(t: float):
        dy = t * fall
        if scale_to is not None:
            k = 1.0 - (1.0 - scale_to) * t
            nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
            nx = x + (w - nw) // 2
            ny = y + (h - nh) // 2 + dy
            _place(window, nw, nh, nx, ny)
        else:
            _place(window, w, h, x, int(round(y + dy)))

    tw_fade = _animate(window, max(60, int(duration * 0.8)), _fade, None,
                       ease_out_cubic)
    tw_drop = _animate(window, duration, _drop, on_done, ease_out_cubic)
    _slot(window).extend([tw_fade, tw_drop])
    return tw_drop


def slide_in_dialog(window, x: int | None = None, y: int | None = None,
                    *, rise: int | None = None,
                    on_done: Callable[[], None] | None = None) -> Tween:
    """
    兼容旧调用（db_panel 一直在用）：把弹窗放到 (x, y) 再播入场。

    `x/y` 不传时用窗口当前几何（调用方已经自己算好居中了）。
    """
    geo = _geometry(window)
    if geo is not None and x is not None and y is not None:
        w, h, _cx, _cy = geo
        _place(window, w, h, int(x), int(y))
    return pop_in(window, rise=Anim.DIALOG_RISE_PX if rise is None else rise,
                  duration=Anim.DIALOG_MS, on_done=on_done)
