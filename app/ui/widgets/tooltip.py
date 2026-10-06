"""
tooltip.py — 轻量悬停提示
------------------------------------------------
用法：
    attach_tooltip(widget, "完整路径…")
    attach_tooltip(widget, lambda: 动态文本())

实现方式与项目里既有的写法一致：悬停时弹一个无边框小窗，移开/点击即消失。
所有异常都被吞掉 —— 提示失败绝不能影响主流程。
"""

from collections.abc import Callable

import customtkinter as ctk

from ...theme import Color, Font

_MAX_CHARS = 400


class Tooltip:
    def __init__(self, widget, text: str | Callable[[], str]):
        self.widget = widget
        self._text = text
        self._win: ctk.CTkToplevel | None = None
        try:
            widget.bind("<Enter>", self._show, add="+")
            widget.bind("<Leave>", self._hide, add="+")
            widget.bind("<ButtonPress>", self._hide, add="+")
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _resolve_text(self) -> str:
        try:
            text = self._text() if callable(self._text) else self._text
        except Exception:  # noqa: BLE001
            return ""
        text = str(text or "")
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "…"
        return text

    def _show(self, _event=None):
        text = self._resolve_text()
        if not text or self._win is not None:
            return
        try:
            win = ctk.CTkToplevel(self.widget)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(fg_color=Color.BORDER)
            label = ctk.CTkLabel(
                win, text=text, font=Font.SMALL,
                text_color=Color.TEXT_PRIMARY, fg_color=Color.LOG_BG,
                justify="left", wraplength=520)
            label.pack(padx=1, pady=1)
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            win.geometry(f"+{x}+{y}")
            self._win = win
        except Exception:  # noqa: BLE001
            self._win = None

    def _hide(self, _event=None):
        win, self._win = self._win, None
        if win is None:
            return
        try:
            win.destroy()
        except Exception:  # noqa: BLE001, S110
            pass


def attach_tooltip(widget, text: str | Callable[[], str]) -> Tooltip | None:
    """给控件挂一个悬停提示；失败返回 None（绝不影响调用方）。"""
    try:
        return Tooltip(widget, text)
    except Exception:  # noqa: BLE001
        return None
