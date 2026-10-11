"""
animation.py — 动画工具
------------------------------------------------
基于 after() 的线性/缓动插值动画：
  - 颜色过渡
  - 页面水平滑动（easeInOutCubic，整数像素步进）
  - 折叠 / 展开的高度补间（切换式按钮）
  - 滑动色块（策略按钮组）
  - 数值平滑（进度条）

统一按**真实经过时间**推进（见 `Tween`）：某一帧慢了，动画不会被跟着拖长 ——
按固定帧数推进时，一帧卡 40ms 就会让整段动画多花一倍时间，看起来发涩。

健壮性：widget 被销毁、after 被取消、configure 失败时静默停止。
"""

import time
from collections.abc import Callable

from ..theme import Anim


# ----------------------------------------------------------------------
# 颜色工具
# ----------------------------------------------------------------------
def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    """#RRGGBB → (r, g, b)，返回定长三元组"""
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def lerp(a: float, b: float, t: float) -> float:
    """数值线性插值"""
    return a + (b - a) * t


def lerp_color(c1: str, c2: str, t: float) -> str:
    """颜色线性插值"""
    try:
        a = _hex_to_rgb(c1)
        b = _hex_to_rgb(c2)
    except Exception:  # noqa: BLE001
        return c2
    r = round(lerp(a[0], b[0], t))
    g = round(lerp(a[1], b[1], t))
    b_ = round(lerp(a[2], b[2], t))
    return _rgb_to_hex((r, g, b_))


def ease_in_out_cubic(t: float) -> float:
    """缓入缓出：开头慢、中段快、结尾慢（手机滑屏手感）"""
    if t < 0.5:
        return 4 * t * t * t
    return 1 - (-2 * t + 2) ** 3 / 2


# ----------------------------------------------------------------------
# 安全 configure
# ----------------------------------------------------------------------
def _safe_configure(widget, **kwargs) -> bool:
    """安全配置 widget；widget 已销毁时返回 False"""
    try:
        if not widget.winfo_exists():
            return False
        widget.configure(**kwargs)
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# 补间核心：按真实时间推进
# ----------------------------------------------------------------------
class Tween:
    """
    一次补间动画。每帧回调 `on_frame(t_eased, t_raw)`：

      * `t_raw`  = 已经过时间 / 总时长（0~1，真实时间）
      * `t_eased`= 缓动后的值

    与"跑固定帧数"的写法相比：慢帧只影响帧率，不影响**总时长**。
    """

    def __init__(self, widget, duration_ms: int, on_frame: Callable,
                 on_done: Callable[[], None] | None = None,
                 ease: Callable[[float], float] = ease_in_out_cubic,
                 frame_ms: int = Anim.FRAME_MS):
        self.widget = widget
        self.duration_ms = max(1, int(duration_ms))
        self.on_frame = on_frame
        self.on_done = on_done
        self.ease = ease
        self.frame_ms = max(1, int(frame_ms))
        self._after_id = None
        self._cancelled = False
        self._t0 = time.perf_counter()
        self._tick()

    # -- 控制 ----------------------------------------------------------
    @property
    def running(self) -> bool:
        return not self._cancelled and self._after_id is not None

    def cancel(self):
        self._cancelled = True
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001, S110
                pass
            self._after_id = None

    # -- 内部 ----------------------------------------------------------
    def _tick(self):
        if self._cancelled:
            return
        try:
            if not self.widget.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return

        raw = (time.perf_counter() - self._t0) * 1000.0 / self.duration_ms
        raw = max(0.0, min(1.0, raw))
        try:
            self.on_frame(self.ease(raw), raw)
        except Exception:  # noqa: BLE001
            return

        if raw >= 1.0:
            self._after_id = None
            if self.on_done is not None:
                try:
                    self.on_done()
                except Exception:  # noqa: BLE001, S110
                    pass
            return

        try:
            self._after_id = self.widget.after(self.frame_ms, self._tick)
        except Exception:  # noqa: BLE001
            self._after_id = None


def tween(widget, duration_ms: int, on_frame: Callable,
          on_done: Callable[[], None] | None = None,
          ease: Callable[[float], float] = ease_in_out_cubic,
          frame_ms: int = Anim.FRAME_MS) -> Tween:
    """`Tween` 的简写入口。"""
    return Tween(widget, duration_ms, on_frame, on_done=on_done, ease=ease,
                 frame_ms=frame_ms)


# ----------------------------------------------------------------------
# 折叠 / 展开（切换式按钮）
# ----------------------------------------------------------------------
def natural_height(widget, refresh: bool = False) -> int:
    """
    内容自然高度（默认量一次就缓存）。

    动画会把 `configure(height=…)` 改成动画值，之后再量就不是自然高度了 ——
    所以量之前先把尺寸传播打开；内容会变的控件（列表）用 `refresh=True`
    强制重新量。
    """
    cached = getattr(widget, "_anim_natural_h", 0)
    if cached and not refresh:
        return int(cached)
    try:
        widget.grid_propagate(True)
        widget.pack_propagate(True)
        widget.update_idletasks()
        h = int(widget.winfo_reqheight())
    except Exception:  # noqa: BLE001
        h = 0
    try:
        widget._anim_natural_h = h  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001, S110
        pass
    return h


def animate_height(widget, start_h: float, end_h: float,
                   duration_ms: int = Anim.TOGGLE_MS,
                   on_done: Callable[[], None] | None = None) -> Tween:
    """高度补间（要求 widget 已关掉尺寸传播，否则会被内容顶回自然高度）。"""
    try:
        widget.grid_propagate(False)
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        widget.pack_propagate(False)
    except Exception:  # noqa: BLE001, S110
        pass

    def _frame(t_eased: float, _raw: float):
        _safe_configure(widget, height=max(0, int(round(
            lerp(start_h, end_h, t_eased)))))

    return tween(widget, duration_ms, _frame, on_done=on_done)


def toggle_height(widget, show: bool,
                  duration_ms: int = Anim.TOGGLE_MS,
                  on_done: Callable[[], None] | None = None,
                  refresh_natural: bool = False) -> Tween:
    """
    展开 / 收起一个 grid 容器：高度 0 ↔ 自然高度。

    展开：先压成 0 再 `grid()`（避免闪一帧全高）→ 补到自然高度。
    收起：补到 0 → `grid_remove()`。

    内容会变的控件（例如白名单列表）传 `refresh_natural=True`，每次都重量。
    """
    target = natural_height(widget, refresh=refresh_natural)
    cur = 0
    try:
        cur = int(widget.winfo_height())
    except Exception:  # noqa: BLE001
        cur = 0
    if cur <= 1:                      # 收起状态（Tk 对未映射控件报 1）
        cur = 0

    if show:
        _safe_configure(widget, height=0)
        try:
            widget.grid()
        except Exception:  # noqa: BLE001, S110
            pass
        start, end = 0, target
    else:
        start, end = (cur or target), 0

    def _done():
        if not show:
            try:
                widget.grid_remove()
            except Exception:  # noqa: BLE001, S110
                pass
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    return animate_height(widget, start, end, duration_ms, on_done=_done)


# ----------------------------------------------------------------------
# 颜色过渡
# ----------------------------------------------------------------------
def animate_color(widget, attr: str, start: str, end: str,
                  duration_ms: int = Anim.HOVER_MS,
                  on_done: Callable[[], None] | None = None):
    """对 widget 指定颜色属性做线性过渡"""
    steps = max(1, duration_ms // Anim.FRAME_MS)

    def _step(i: int):
        t = i / steps
        if not _safe_configure(widget, **{attr: lerp_color(start, end, t)}):
            return
        if i < steps:
            try:
                widget.after(Anim.FRAME_MS, _step, i + 1)
            except Exception:  # noqa: BLE001
                return
        elif on_done:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    _step(0)


# ----------------------------------------------------------------------
# 页面滑动（缓入缓出）
# ----------------------------------------------------------------------
def slide_pages(container, old_widget, new_widget, direction: str = "left",
                duration_ms: int = Anim.SWITCH_MS,
                on_done: Callable[[], None] | None = None,
                on_start: Callable[[], None] | None = None):
    """
    卡片式水平滑动：新页面从一侧滑入，旧页面向另一侧滑出。

    两个要点（都是踩过的坑）：
      * 位移用**整数像素** `x`，不用 `relx` 小数 —— 小数位移会让 Tk 每帧
        重绘子控件，Text / 编辑器一类控件会"拉丝"。
      * 按真实时间推进（`Tween`），慢帧不会把动画拖长。
    """
    try:
        width = int(container.winfo_width())
    except Exception:  # noqa: BLE001
        width = 0
    if width <= 1:                      # 容器还没布局出来：直接换页
        try:
            old_widget.place_forget()
            new_widget.place(in_=container, relx=0, rely=0,
                             relwidth=1.0, relheight=1.0)
        except Exception:  # noqa: BLE001, S110
            pass
        if on_done is not None:
            on_done()
        return None

    sign = -1.0 if direction == "left" else 1.0
    try:
        # 初始定位：新页面停在另一侧（relx 必须显式归零，否则会和 x 叠加）
        new_widget.place(in_=container, relx=0, x=int(-sign * width), y=0,
                         relwidth=1.0, relheight=1.0)
        new_widget.lift()
    except Exception:  # noqa: BLE001
        try:
            old_widget.place_forget()
            new_widget.place(in_=container, relx=0, rely=0,
                             relwidth=1.0, relheight=1.0)
        except Exception:  # noqa: BLE001, S110
            pass
        if on_done is not None:
            on_done()
        return None

    if on_start is not None:
        try:
            on_start()
        except Exception:  # noqa: BLE001, S110
            pass

    def _frame(t: float, _raw: float):
        new_x = int(round(lerp(-sign * width, 0.0, t)))
        old_x = int(round(lerp(0.0, sign * width, t)))
        try:
            if old_widget.winfo_exists():
                old_widget.place_configure(relx=0, x=old_x)
            if new_widget.winfo_exists():
                new_widget.place_configure(relx=0, x=new_x)
        except Exception:  # noqa: BLE001, S110
            pass

    def _done():
        try:
            if old_widget.winfo_exists():
                old_widget.place_forget()
            if new_widget.winfo_exists():
                new_widget.place(in_=container, relx=0, rely=0,
                                 relwidth=1.0, relheight=1.0)
        except Exception:  # noqa: BLE001, S110
            pass
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    return tween(container, duration_ms, _frame, on_done=_done)


# ----------------------------------------------------------------------
# 滑动色块：只水平移动，不改尺寸
# ----------------------------------------------------------------------
def slide_block(block, target_x: int, _target_w: int = 0,
                duration_ms: int = Anim.SLIDER_MS,
                on_done: Callable[[], None] | None = None):
    """
    让 place 定位的色块从当前位置滑到目标 x。
    width 不参与动画（CustomTkinter 禁止 place 改尺寸）。
    """
    try:
        cur_x = block.winfo_x()
    except Exception:  # noqa: BLE001
        cur_x = target_x

    steps = max(1, duration_ms // Anim.FRAME_MS)

    def _step(i: int):
        t = i / steps
        x = lerp(cur_x, target_x, t)
        try:
            if not block.winfo_exists():
                return
            block.place_configure(x=int(x))
        except Exception:  # noqa: BLE001
            return
        if i < steps:
            try:
                block.after(Anim.FRAME_MS, _step, i + 1)
            except Exception:  # noqa: BLE001
                return
        elif on_done:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    _step(0)


# ----------------------------------------------------------------------
# 数值平滑
# ----------------------------------------------------------------------
def animate_value(widget, attr: str, start: float, end: float,
                  duration_ms: int = 200,
                  on_done: Callable[[], None] | None = None):
    """对数值属性（如进度条 value）平滑过渡"""
    steps = max(1, duration_ms // Anim.FRAME_MS)

    def _step(i: int):
        t = i / steps
        try:
            if not widget.winfo_exists():
                return
            widget.set(lerp(start, end, t))
        except Exception:  # noqa: BLE001
            return
        if i < steps:
            try:
                widget.after(Anim.FRAME_MS, _step, i + 1)
            except Exception:  # noqa: BLE001
                return
        elif on_done:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    _step(0)