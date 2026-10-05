"""
text_field.py — 无边框输入框
------------------------------------------------
背景 INPUT_BG，聚焦时背景变 INPUT_FOCUS_BG，
底部出现 2px 青色下划线，圆角 8px。
"""

import customtkinter as ctk

from ...theme import Color, Font, Size


class TextField(ctk.CTkFrame):
    def __init__(self, master, placeholder: str = "", **kwargs):
        super().__init__(master, fg_color=Color.TRANSPARENT,
                         corner_radius=0, **kwargs)

        self.entry = ctk.CTkEntry(
            self, placeholder_text=placeholder,
            font=Font.BODY, corner_radius=Size.RADIUS_INPUT,
            fg_color=Color.INPUT_BG, border_width=0,
            text_color=Color.TEXT_PRIMARY,
            placeholder_text_color=Color.TEXT_MUTED,
            height=36)
        self.entry.pack(fill="x")

        # 下划线（初始隐藏）
        self.underline = ctk.CTkFrame(
            self, fg_color=Color.ACCENT,
            height=Size.INPUT_UNDERLINE_H, corner_radius=0)
        self.underline.pack(fill="x", pady=(1, 0))
        self.underline.pack_forget()

        self.entry.bind("<FocusIn>", self._on_focus)
        self.entry.bind("<FocusOut>", self._on_blur)

    def _on_focus(self, _e):
        self.entry.configure(fg_color=Color.INPUT_FOCUS_BG)
        self.underline.pack(fill="x", pady=(1, 0))

    def _on_blur(self, _e):
        self.entry.configure(fg_color=Color.INPUT_BG)
        self.underline.pack_forget()

    def get(self) -> str:
        return self.entry.get()

    def set(self, text: str):
        self.entry.delete(0, "end")
        self.entry.insert(0, text)