"""
md_viewer.py — Markdown 弹窗（使用自定义 Modal）
"""

import re

import customtkinter as ctk

from ...theme import Color, Font
from .modal import Modal

try:
    import markdown as md_lib
except Exception:
    md_lib = None


class MarkdownModal(Modal):
    def __init__(self, parent, title: str, md_text: str):
        super().__init__(parent, title=title)

        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        box = ctk.CTkTextbox(
            self.body, fg_color=Color.LOG_BG,
            text_color=Color.TEXT_PRIMARY,
            font=Font.BODY, corner_radius=8, border_width=0, wrap="word",
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        box.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        box.insert("1.0", _render_plain(md_text))
        box.configure(state="disabled")


def show_markdown_dialog(parent, title: str, md_text: str):
    MarkdownModal(parent, title, md_text)


def _render_plain(md_text: str) -> str:
    if md_lib is None:
        return md_text
    try:
        html = md_lib.markdown(md_text or "")
        return re.sub(r"<[^>]+>", "", html).strip()
    except Exception:
        return md_text