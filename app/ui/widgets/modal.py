"""
modal.py — 自定义弹窗
------------------------------------------------
背景 CARD_BG，圆角 16px，标题栏自定义，带关闭按钮，
内容区可滚动。不使用 Windows 原生对话框。
"""

import customtkinter as ctk

from ...theme import Color, Font, Size


class Modal(ctk.CTkToplevel):
    def __init__(self, parent, title: str, width: int = 720, height: int = 560):
        super().__init__(parent)

        self.title(title)
        self.geometry(f"{width}x{height}")
        self.configure(fg_color=Color.CARD_BG)
        self.transient(parent)
        self.grab_set()

        # 卡片式容器（无原生边框）
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 标题栏
        header = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                              corner_radius=0, height=48)
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 0))
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text=title, font=Font.TITLE,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=0, column=0, sticky="w", padx=4)

        ctk.CTkButton(header, text="✕", width=32, height=32,
                      font=Font.BODY, corner_radius=Size.RADIUS_BUTTON,
                      fg_color=Color.TRANSPARENT,
                      hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self.destroy)\
            .grid(row=0, column=1, sticky="e", padx=4)

        # 内容区（子类往 self.body 里塞内容）
        self.body = ctk.CTkFrame(self, fg_color=Color.LOG_BG,
                                 corner_radius=Size.RADIUS_BUTTON)
        self.body.grid(row=1, column=0, sticky="nsew",
                       padx=16, pady=(8, 16))