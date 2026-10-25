"""
segmented_switch.py — 多段式切换（玩家·开发者、编辑·预览…）
------------------------------------------------
替代 `CTkSegmentedButton`：它的高亮块画在 canvas 上，切换时只能**瞬间跳**过去。

这里等效实现，不用 canvas：
  * 一排平铺按钮（选中态底色 ACCENT、未选中 LOG_BG）；
  * 按钮下沿一条细高亮条，切换时**滑**到目标段（约 200ms 缓动）；
  * 两个按钮的底色 / 文字色同时交叉渐变。

对外接口与 `CTkSegmentedButton` 对齐：`values` / `command` / `set()` / `get()`。
"""

from collections.abc import Callable

import customtkinter as ctk

from ...theme import Anim, Color, Font, Size
from ...utils.animation import animate_color, tween


class SegmentedSwitch(ctk.CTkFrame):
    def __init__(self, master, values: list[str],
                 command: Callable[[str], None] | None = None,
                 height: int = Size.SEG_BTN_H,
                 font=None, **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_BUTTON,
                         height=height, **kwargs)
        self.grid_propagate(False)
        self.pack_propagate(False)

        self._values = list(values) or [""]
        self._command = command
        self._height = int(height)
        self._value = self._values[0]
        self._index = 0
        self._tween = None
        self._colors: list[tuple[str, str]] = []

        self.grid_rowconfigure(0, weight=1)
        for i in range(len(self._values)):
            self.grid_columnconfigure(i, weight=1, uniform="seg")

        self._btns: list[ctk.CTkButton] = []
        for i, value in enumerate(self._values):
            btn = ctk.CTkButton(
                self, text=value, height=max(20, self._height - 8),
                font=font or Font.BODY,
                corner_radius=max(2, Size.RADIUS_BUTTON - 3),
                fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                text_color=Color.TEXT_PRIMARY,
                command=lambda v=value: self.select(v))
            btn.grid(row=0, column=i, sticky="nsew", padx=2, pady=(2, 7))
            self._btns.append(btn)
            self._colors.append((Color.LOG_BG, Color.TEXT_PRIMARY))

        # 高亮条放在按钮下沿：不遮文字，所以不需要"真透明"
        # （CTk 不允许在 place() 里传 height，高度只能给构造函数）
        self._bar = ctk.CTkFrame(self, height=3, corner_radius=2,
                                 fg_color=Color.ACCENT)
        self._bar.place(x=0, y=max(0, self._height - 5),
                        relwidth=1.0 / len(self._values))
        self.bind("<Configure>", self._on_resize, add="+")
        self._apply_colors(self._index, animate=False)

    # ------------------------------------------------------------------
    # 对外接口（与 CTkSegmentedButton 对齐）
    # ------------------------------------------------------------------
    def get(self) -> str:
        return self._value

    def set(self, value: str):
        """外部同步状态：只改显示，不发回调。"""
        self.select(value, notify=False)

    def select(self, value: str, notify: bool = True) -> bool:
        """
        选中某一段。

        `command` 返回 **False** 表示"这次切换被拒绝"（例如页面正在做切换
        动画）：此时把高亮条/底色**退回原来的段**，绝不出现"开关显示开发者、
        页面还停在玩家"的错位。
        """
        if value not in self._values:
            return False
        prev, prev_idx = self._value, self._index
        if value == prev:
            return True

        if notify and self._command is not None:
            try:
                accepted = self._command(value)
            except Exception:  # noqa: BLE001
                accepted = True
            if accepted is False:
                self._value, self._index = prev, prev_idx
                self._slide_bar(prev_idx)
                self._apply_colors(prev_idx, animate=True)
                return False

        idx = self._values.index(value)
        self._value = value
        self._index = idx
        self._slide_bar(idx)
        self._apply_colors(idx, animate=True)
        return True

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _seg_width(self) -> float:
        try:
            w = int(self.winfo_width())
        except Exception:  # noqa: BLE001
            w = 0
        if w <= 1:                      # 还没布局出来：给个估值
            w = 160 * len(self._values)
        return max(1.0, w / len(self._values))

    def _bar_target_x(self, idx: int) -> int:
        return int(round(idx * self._seg_width()))

    def _slide_bar(self, idx: int):
        target = self._bar_target_x(idx)
        try:
            cur = int(self._bar.winfo_x())
        except Exception:  # noqa: BLE001
            cur = target
        if self._tween is not None:
            self._tween.cancel()
            self._tween = None
        if abs(cur - target) < 1:
            try:
                self._bar.place_configure(x=target)
            except Exception:  # noqa: BLE001, S110
                pass
            return

        def _frame(t_eased: float, _raw: float):
            try:
                self._bar.place_configure(
                    x=int(round(cur + (target - cur) * t_eased)))
            except Exception:  # noqa: BLE001, S110
                pass

        self._tween = tween(self, Anim.SLIDER_MS, _frame,
                            on_done=self._clear_tween)

    def _clear_tween(self):
        self._tween = None

    def _apply_colors(self, idx: int, animate: bool):
        for i, btn in enumerate(self._btns):
            on = (i == idx)
            fg = Color.ACCENT if on else Color.LOG_BG
            tc = Color.TEXT_ON_ACCENT if on else Color.TEXT_PRIMARY
            old_fg, old_tc = self._colors[i]
            self._colors[i] = (fg, tc)
            if animate and old_fg != fg:
                animate_color(btn, "fg_color", old_fg, fg,
                              duration_ms=Anim.SLIDER_MS)
            if animate and old_tc != tc:
                animate_color(btn, "text_color", old_tc, tc,
                              duration_ms=Anim.SLIDER_MS)
            if not animate:
                try:
                    btn.configure(fg_color=fg, text_color=tc)
                except Exception:  # noqa: BLE001, S110
                    pass
            try:
                btn.configure(hover_color=(Color.ACCENT_HOVER if on
                                           else Color.BORDER))
            except Exception:  # noqa: BLE001, S110
                pass

    def _on_resize(self, _event=None):
        """窗口变宽变窄时，高亮条跟着当前段重新落位。"""
        try:
            self._bar.place_configure(x=self._bar_target_x(self._index))
        except Exception:  # noqa: BLE001, S110
            pass
