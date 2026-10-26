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


def ease_out_back(t: float, overshoot: float = 1.1) -> float:
    """轻微过冲再回位：展开/出现类动画用它，看起来"有弹性"。"""
    c = overshoot + 1.0
    return 1.0 + c * (t - 1.0) ** 3 + overshoot * (t - 1.0) ** 2


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


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
        self._finished = False
        self._t0 = time.perf_counter()
        self._tick()

    # -- 控制 ----------------------------------------------------------
    @property
    def running(self) -> bool:
        return not self._cancelled and self._after_id is not None

    def cancel(self, notify: bool = False):
        """
        取消动画。

        notify=True 时仍会调用一次 `on_done`：用于"取消也要收尾"的场景
        （例如切页中途被打断，必须把旧页面收掉，否则两个页面叠在一起）。
        """
        self._cancelled = True
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001, S110
                pass
            self._after_id = None
        if notify:
            self._finish()

    def _finish(self):
        """收尾：保证 on_done 只跑一次（正常结束 / 异常 / 控件消失都走这里）。"""
        if self._finished:
            return
        self._finished = True
        self._after_id = None
        if self.on_done is not None:
            try:
                self.on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    # -- 内部 ----------------------------------------------------------
    def _tick(self):
        if self._cancelled or self._finished:
            return
        try:
            if not self.widget.winfo_exists():
                # 控件没了：动画不可能再画，但调用方的"收尾"仍要跑，
                # 否则 _switching 之类的状态会永远卡住
                self._finish()
                return
        except Exception:  # noqa: BLE001
            self._finish()
            return

        raw = (time.perf_counter() - self._t0) * 1000.0 / self.duration_ms
        raw = max(0.0, min(1.0, raw))
        try:
            self.on_frame(self.ease(raw), raw)
        except Exception:  # noqa: BLE001
            self._finish()
            return

        if raw >= 1.0:
            self._finish()
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
                   on_done: Callable[[], None] | None = None,
                   ease: Callable[[float], float] | None = None) -> Tween:
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

    return tween(widget, duration_ms, _frame, on_done=on_done,
                 ease=ease or ease_in_out_cubic)



class _ChildMask:
    """
    折叠动画期间把容器里的子控件**藏起来**，动画结束再按原来的布局管理器恢复。

    为什么需要它：Tk 的容器一改 height，grid/pack 就会每一帧重新排布所有子控件，
    于是展开/折叠时能看到一行行的抖动和"拉丝"残影。把子控件先撤下，
    动画期间只有一块干净的背景在长高，结束再让内容逐行出现。
    """

    def __init__(self, container):
        self.container = container
        self.items: list[tuple] = []

    def hide(self) -> None:
        try:
            children = list(self.container.winfo_children())
        except Exception:  # noqa: BLE001
            children = []
        for child in children:
            try:
                mgr = child.winfo_manager()
            except Exception:  # noqa: BLE001
                continue
            try:
                if mgr == "grid":
                    self.items.append((child, "grid", child.grid_info()))
                    child.grid_remove()
                elif mgr == "pack":
                    self.items.append((child, "pack", child.pack_info()))
                    child.pack_forget()
                elif mgr == "place":
                    self.items.append((child, "place", child.place_info()))
                    child.place_forget()
            except Exception:  # noqa: BLE001, S110
                continue

    def restore(self, stagger_ms: int = 0,
                on_done: Callable[[], None] | None = None) -> None:
        if not self.items:
            if on_done is not None:
                try:
                    on_done()
                except Exception:  # noqa: BLE001, S110
                    pass
            return

        pending = list(self.items)
        self.items = []

        def _restore_one() -> None:
            if not pending:
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:  # noqa: BLE001, S110
                        pass
                return
            child, mgr, info = pending.pop(0)
            try:
                if child.winfo_exists():
                    if mgr == "grid":
                        child.grid(**info)
                    elif mgr == "place":
                        child.place(**info)
                    else:
                        child.pack(**info)
            except Exception:  # noqa: BLE001, S110
                pass
            if stagger_ms > 0:
                try:
                    self.container.after(stagger_ms, _restore_one)
                    return
                except Exception:  # noqa: BLE001
                    pass
            _restore_one()

        if stagger_ms <= 0:
            while pending:
                _restore_one()
            return
        _restore_one()



# ----------------------------------------------------------------------
# 换页：截图滑动（不移动活控件）
# ----------------------------------------------------------------------
def snapshot_slide(container, old_widget, new_widget, *,
                   duration_ms: int | None = None,
                   on_done: Callable[[], None] | None = None) -> bool:
    """
    把旧页截成一张**图片**滑走，新页原地显示。

    为什么这么做：在 Tk 里水平搬一个活的控件树，每一帧都会重绘所有子窗口，
    Text / Canvas / 圆角控件会留下一条条竖着的残影（"拉丝"），
    盖色板只能遮住一部分。这里页面上**没有任何控件在动**，
    动的只是一张 Bitmap —— 从根上消除残影。

    截图不可用（没有显示器、截图失败、控件还没布局）时返回 False，
    调用方退回"盖色板 + 搬页面"的旧实现。
    """
    import tkinter as tk

    try:
        from PIL import ImageGrab, ImageTk
    except Exception:  # noqa: BLE001
        return False

    try:
        w = int(old_widget.winfo_width())
        h = int(old_widget.winfo_height())
        x = int(old_widget.winfo_rootx())
        y = int(old_widget.winfo_rooty())
    except Exception:  # noqa: BLE001
        return False
    if w <= 2 or h <= 2:
        return False

    try:
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        if img.size != (w, h):
            img = img.resize((w, h))
        # 保持引用，否则 PhotoImage 会被回收 → 白块
        photo = ImageTk.PhotoImage(img, master=container)
    except Exception:  # noqa: BLE001
        return False

    try:
        new_widget.place(in_=container, relx=0, rely=0,
                         relwidth=1.0, relheight=1.0)
        canvas = tk.Canvas(container, highlightthickness=0, bd=0,
                           bg="#202024")
        canvas.place(x=0, y=0, width=w, height=h)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas._snapshot = photo          # type: ignore[attr-defined]
        # 注意：Canvas.lift 被 Tk 定义成"提升画布内的 item"，
        # 不带参数会报 wrong # args —— 要提窗口得直接调 raise
        canvas.tk.call("raise", canvas._w)
        old_widget.place_forget()
    except Exception:  # noqa: BLE001
        return False

    ms = Anim.SWITCH_MS if duration_ms is None else int(duration_ms)

    def _frame(t_eased: float, _raw: float):
        try:
            canvas.place_configure(x=int(round(-w * t_eased)))
        except Exception:  # noqa: BLE001, S110
            pass

    def _done():
        try:
            canvas.destroy()
        except Exception:  # noqa: BLE001, S110
            pass
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    tween(canvas, ms, _frame, on_done=_done, ease=ease_out_quart)
    return True


def ease_out_quart(t: float) -> float:
    """先快后慢：换页滑出用它，比 in-out 更"利落"。"""
    return 1.0 - (1.0 - t) ** 4


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
    # 上一次的展开/收起动画还在跑：先取消，否则两条补间同时改 height，
    # 快速连点折叠按钮会看到高度来回抖
    prev = getattr(widget, "_anim_toggle", None)
    if prev is not None:
        try:
            prev.cancel()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            widget._anim_toggle = None     # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001, S110
            pass

    target = natural_height(widget, refresh=refresh_natural)
    cur = 0
    try:
        cur = int(widget.winfo_height())
    except Exception:  # noqa: BLE001
        cur = 0
    if cur <= 1:                      # 收起状态（Tk 对未映射控件报 1）
        cur = 0

    # 动画期间先把子控件撤下：否则每一帧都会重新排布，抖 + 拉丝
    mask = _ChildMask(widget)
    mask.hide()
    reveal_ms = 0 if not show else 8

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
        def _after_restore():
            if show:
                # 内容回来了高度可能略有变化，收个尾
                try:
                    h = natural_height(widget, refresh=True)
                    _safe_configure(widget, height=h)
                except Exception:  # noqa: BLE001, S110
                    pass
            try:
                widget._anim_toggle = None     # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001, S110
                pass
            if on_done is not None:
                try:
                    on_done()
                except Exception:  # noqa: BLE001, S110
                    pass

        mask.restore(stagger_ms=reveal_ms, on_done=_after_restore)

    tw = animate_height(
        widget, start, end, duration_ms, on_done=_done,
        ease=ease_out_back if show else ease_out_cubic)
    try:
        widget._anim_toggle = tw           # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001, S110
        pass
    return tw


# ----------------------------------------------------------------------
# 颜色过渡
# ----------------------------------------------------------------------
def _run_slot(widget, name: str):
    """取（或创建）widget 上的"动画槽"，同一个槽里只留一条动画。"""
    runs = getattr(widget, "_anim_runs", None)
    if runs is None:
        runs = {}
        try:
            widget._anim_runs = runs        # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return {}
    return runs


def _replace(widget, name: str, make):
    """
    用新动画替换同名旧动画（旧的在跑的会被取消）。

    之前 `animate_color` / `slide_block` / `animate_value` 都是"每次调用起
    一条固定帧数链"，快速 hover、快速切段时会几条链同时改同一个属性，
    颜色/位置互相打架，看起来就是闪烁或停在中间色。
    """
    runs = _run_slot(widget, name)
    prev = runs.get(name)
    if prev is not None:
        try:
            prev.cancel()
        except Exception:  # noqa: BLE001, S110
            pass

    def _clear():
        if runs.get(name) is tw:
            runs.pop(name, None)

    tw = make(_clear)
    runs[name] = tw
    return tw


def animate_color(widget, attr: str, start: str, end: str,
                  duration_ms: int = Anim.HOVER_MS,
                  on_done: Callable[[], None] | None = None):
    """对 widget 指定颜色属性做过渡（同一属性只保留一条动画）。"""
    def _frame(t_eased: float, _raw: float):
        _safe_configure(widget, **{attr: lerp_color(start, end, t_eased)})

    def _done():
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    def _make(clear):
        return tween(widget, duration_ms, _frame,
                     on_done=lambda: (clear(), _done()))

    return _replace(widget, f"color:{attr}", _make)


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

    def _frame(t_eased: float, _raw: float):
        try:
            block.place_configure(x=int(round(
                lerp(cur_x, target_x, t_eased))))
        except Exception:  # noqa: BLE001, S110
            pass

    def _done():
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    def _make(clear):
        return tween(block, duration_ms, _frame,
                     on_done=lambda: (clear(), _done()))

    return _replace(block, "slide_x", _make)


# ----------------------------------------------------------------------
# 数值平滑
# ----------------------------------------------------------------------
def animate_value(widget, attr: str, start: float, end: float,
                  duration_ms: int = 200,
                  on_done: Callable[[], None] | None = None):
    """对数值属性（如进度条 value）平滑过渡"""
    def _frame(t_eased: float, _raw: float):
        try:
            widget.set(lerp(start, end, t_eased))
        except Exception:  # noqa: BLE001, S110
            pass

    def _done():
        if on_done is not None:
            try:
                on_done()
            except Exception:  # noqa: BLE001, S110
                pass

    def _make(clear):
        return tween(widget, duration_ms, _frame,
                     on_done=lambda: (clear(), _done()))

    return _replace(widget, f"value:{attr}", _make)