"""
strategy_group.py — 三连策略按钮组
------------------------------------------------
三个策略按钮紧贴排列，每个按钮都是独立圆角矩形。
选中态：亮黄背景 + 深色文字。
切换时按钮颜色 150ms 渐变。
"""

from collections.abc import Callable

import customtkinter as ctk

from ...config import STRATEGY_LIST, Strategy
from ...theme import Anim, Color, Font, Size
from ...utils.animation import lerp_color


class StrategyGroup(ctk.CTkFrame):
    def __init__(self, master, value: Strategy = Strategy.FULL_MATCH,
                 on_change: Callable[[Strategy], None] | None = None,
                 btn_width: int = 72, height: int = 28, **kwargs):
        # 整体宽度 = 三按钮宽之和（无间隙，紧贴）
        total_w = btn_width * len(STRATEGY_LIST)

        super().__init__(master, fg_color=Color.TRANSPARENT,
                         corner_radius=0, width=total_w, height=height,
                         **kwargs)
        self.grid_propagate(False)
        self.pack_propagate(False)

        self.on_change = on_change
        self._value = value
        self._btn_w = btn_width
        self._height = height
        self._buttons: list[ctk.CTkButton] = []
        self._anim_tokens: list[int] = []

        # 三列等宽，无间隙
        for i in range(len(STRATEGY_LIST)):
            self.grid_columnconfigure(i, weight=1, minsize=btn_width)

        for i, strat in enumerate(STRATEGY_LIST):
            selected = (strat == value)

            btn = ctk.CTkButton(
                self, text=strat.label,
                width=btn_width, height=height,
                font=Font.SMALL,
                corner_radius=Size.RADIUS_BUTTON,   # 每个按钮都是完整圆角
                fg_color=(Color.STRATEGY_SELECTED if selected
                          else Color.CARD_BG),
                hover_color=(Color.STRATEGY_SELECTED if selected
                             else Color.BORDER),
                text_color=(Color.STRATEGY_SELECTED_TX if selected
                            else Color.TEXT_SECONDARY),
                border_width=0,
                command=lambda s=strat: self.set_value(s, animate=True))
            btn.grid(row=0, column=i, sticky="nsew", padx=0, pady=0)
            self._buttons.append(btn)

    # ------------------------------------------------------------------
    def set_value(self, value: Strategy, animate: bool = True,
                  notify: bool = True):
        if value == self._value:
            return
        self._value = value
        idx = STRATEGY_LIST.index(value)

        # 每轮动画使用独立 token，互不干扰
        token = id(self) ^ (idx << 8)
        self._anim_tokens = [token]

        for i, btn in enumerate(self._buttons):
            selected = (i == idx)
            target_bg = (Color.STRATEGY_SELECTED if selected
                         else Color.CARD_BG)
            target_tx = (Color.STRATEGY_SELECTED_TX if selected
                         else Color.TEXT_SECONDARY)

            # 预先设定 hover 色（渐变过程中不影响）
            try:
                btn.configure(
                    hover_color=(Color.STRATEGY_SELECTED if selected
                                 else Color.BORDER))
            except Exception:
                pass

            if animate:
                self._fade(btn, target_bg, target_tx, token)
            else:
                try:
                    btn.configure(fg_color=target_bg, text_color=target_tx)
                except Exception:
                    pass

        if notify and self.on_change:
            self.on_change(value)

    def _fade(self, btn, target_bg: str, target_tx: str, token: int):
        try:
            cur_bg = btn.cget("fg_color")
            cur_tx = btn.cget("text_color")
            if isinstance(cur_bg, (tuple, list)):
                cur_bg = cur_bg[0]
            if isinstance(cur_tx, (tuple, list)):
                cur_tx = cur_tx[0]
        except Exception:
            cur_bg, cur_tx = target_bg, target_tx

        steps = max(1, Anim.HOVER_MS // Anim.FRAME_MS)

        def _step(i: int):
            # token 不匹配说明被新动画取代，停
            if token not in self._anim_tokens:
                return
            t = i / steps
            try:
                if not btn.winfo_exists():
                    return
                btn.configure(
                    fg_color=lerp_color(cur_bg, target_bg, t),
                    text_color=lerp_color(cur_tx, target_tx, t))
            except Exception:
                return
            if i < steps:
                try:
                    btn.after(Anim.FRAME_MS, _step, i + 1)
                except Exception:
                    return

        _step(0)

    def get(self) -> Strategy:
        return self._value