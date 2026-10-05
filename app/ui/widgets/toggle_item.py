"""
toggle_item.py — 可点击渐变色条目
------------------------------------------------
- 点击在选中/未选中切换
- 背景色 150ms 线性渐变，文字颜色同步
- 固定宽度，超长文本截断，悬停显示完整
"""

from collections.abc import Callable

import customtkinter as ctk

from ...theme import Anim, Color, Font
from ...utils.animation import lerp_color

_MAX_LABEL_CHARS = 25


def _truncate(text: str, max_chars: int = _MAX_LABEL_CHARS) -> str:
    """超过 max_chars 时截断，加省略号"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1] + "…"


class ToggleItem(ctk.CTkFrame):
    def __init__(self, master, text: str, selected: bool = False,
                 on_toggle: Callable[[bool], None] | None = None,
                 width: int = 200,
                 **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT,
                         corner_radius=0, **kwargs)

        self.on_toggle = on_toggle
        self._selected = selected
        self._full_text = text
        self._display_text = _truncate(text)

        # 内层色块
        self.block = ctk.CTkButton(
            self, text=self._display_text, height=32,
            width=width, anchor="w",
            font=Font.BODY, corner_radius=8,
            fg_color=Color.ACCENT if selected else Color.CARD_BG,
            hover_color=Color.ACCENT_HOVER if selected else Color.BORDER,
            text_color=Color.TEXT_ON_ACCENT if selected else Color.TEXT_SECONDARY,
            command=self.toggle)
        self.block.pack(fill="x")

        # 悬停显示完整名称（用 tooltip）
        self._tooltip: ctk.CTkToplevel | None = None
        if len(text) > _MAX_LABEL_CHARS:
            self.block.bind("<Enter>", self._show_tooltip, add="+")
            self.block.bind("<Leave>", self._hide_tooltip, add="+")
            self.block.bind("<ButtonPress>", self._hide_tooltip, add="+")

    # ------------------------------------------------------------------
    def toggle(self):
        self.set_selected(not self._selected)
        if self.on_toggle:
            self.on_toggle(self._selected)

    def set_selected(self, value: bool, animate: bool = True):
        if value == self._selected:
            return
        self._selected = value

        start_bg = Color.CARD_BG if value else Color.ACCENT
        end_bg = Color.ACCENT if value else Color.CARD_BG
        start_tx = Color.TEXT_SECONDARY if value else Color.TEXT_ON_ACCENT
        end_tx = Color.TEXT_ON_ACCENT if value else Color.TEXT_SECONDARY

        if not animate:
            self.block.configure(
                fg_color=end_bg,
                hover_color=Color.ACCENT_HOVER if value else Color.BORDER,
                text_color=end_tx)
            return

        steps = max(1, Anim.TOGGLE_MS // Anim.FRAME_MS)

        def _step(i: int):
            t = i / steps
            try:
                self.block.configure(
                    fg_color=lerp_color(start_bg, end_bg, t),
                    text_color=lerp_color(start_tx, end_tx, t))
            except Exception:  # noqa: BLE001
                return
            if i < steps:
                self.after(Anim.FRAME_MS, _step, i + 1)
            else:
                self.block.configure(
                    hover_color=Color.ACCENT_HOVER if value else Color.BORDER)

        _step(0)

    def get(self) -> bool:
        return self._selected

    # ------------------------------------------------------------------
    # 悬停 tooltip
    # ------------------------------------------------------------------
    def _show_tooltip(self, _event=None):
        if self._tooltip is not None:
            return
        try:
            x = self.block.winfo_rootx() + 10
            y = self.block.winfo_rooty() + self.block.winfo_height() + 4

            self._tooltip = ctk.CTkToplevel(self)
            self._tooltip.wm_overrideredirect(True)
            self._tooltip.configure(fg_color=Color.LOG_BG)
            self._tooltip.geometry(f"+{x}+{y}")

            ctk.CTkLabel(
                self._tooltip, text=self._full_text,
                font=Font.SMALL, text_color=Color.TEXT_PRIMARY,
                fg_color=Color.LOG_BG)\
                .pack(padx=8, pady=4)
        except Exception:  # noqa: BLE001
            self._tooltip = None

    def _hide_tooltip(self, _event=None):
        if self._tooltip is not None:
            try:
                self._tooltip.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
            self._tooltip = None