"""
animation.py — 动画工具
------------------------------------------------
基于 after() 的线性/缓动插值动画：
  - 颜色过渡
  - 页面水平滑动（easeInOutCubic）
  - 滑动色块（策略按钮组）
  - 数值平滑（进度条）

健壮性：widget 被销毁、after 被取消、configure 失败时静默停止。
"""

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
                on_done: Callable[[], None] | None = None):
    """
    卡片式水平滑动：新页面从一侧滑入，旧页面向另一侧滑出。
    使用 place 的 relx 位移实现，不涉及 alpha。
    缓动：easeInOutCubic，视觉类似手机滑屏。
    """
    steps = max(1, duration_ms // Anim.FRAME_MS)

    # 初始定位：新页面在另一侧
    try:
        new_widget.place(in_=container, relx=1.0, rely=0,
                         relwidth=1.0, relheight=1.0)
        new_widget.lift()
    except Exception:  # noqa: BLE001
        # place 失败则直接显示
        try:
            old_widget.place_forget()
            new_widget.place(in_=container, relx=0, rely=0,
                             relwidth=1.0, relheight=1.0)
        except Exception:  # noqa: BLE001, S110
            pass
        if on_done:
            on_done()
        return

    def _step(i: int):
        t_raw = i / steps
        t = ease_in_out_cubic(t_raw)
        if direction == "left":
            old_x, new_x = lerp(0.0, -1.0, t), lerp(1.0, 0.0, t)
        else:
            old_x, new_x = lerp(0.0, 1.0, t), lerp(-1.0, 0.0, t)

        try:
            if old_widget.winfo_exists():
                old_widget.place_configure(relx=old_x)
            if new_widget.winfo_exists():
                new_widget.place_configure(relx=new_x)
        except Exception:  # noqa: BLE001
            return

        if i < steps:
            try:
                container.after(Anim.FRAME_MS, _step, i + 1)
            except Exception:  # noqa: BLE001
                return
        else:
            try:
                if old_widget.winfo_exists():
                    old_widget.place_forget()
                if new_widget.winfo_exists():
                    new_widget.place(in_=container, relx=0, rely=0,
                                     relwidth=1.0, relheight=1.0)
            except Exception:  # noqa: BLE001, S110
                pass
            if on_done:
                try:
                    on_done()
                except Exception:  # noqa: BLE001, S110
                    pass

    _step(0)


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