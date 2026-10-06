"""
scroll_frame.py — 细滚动条容器
------------------------------------------------
宽度 6px，圆角 3px，颜色 #3E3E42，悬停变青。
基于 CTkScrollableFrame 但覆写滚动条视觉。
"""

import customtkinter as ctk

from ...theme import Color, Size


class ThinScrollFrame(ctk.CTkScrollableFrame):
    def __init__(self, master, **kwargs):
        kwargs.setdefault("fg_color", Color.TRANSPARENT)
        kwargs.setdefault("corner_radius", 0)
        kwargs.setdefault("scrollbar_button_color", Color.BORDER)
        kwargs.setdefault("scrollbar_button_hover_color", Color.ACCENT)
        super().__init__(master, **kwargs)

        # 覆写内部滚动条宽度与圆角
        try:
            sb = self._scrollbar
            sb.configure(width=Size.SCROLLBAR_W, corner_radius=3)
        except Exception:  # noqa: BLE001, S110
            pass