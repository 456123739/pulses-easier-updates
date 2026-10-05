"""
change_tree.py — 变更树
"""

from collections import defaultdict
from pathlib import Path

import customtkinter as ctk

from ...core.differ import DiffResult
from ...theme import Color, Font, Size
from .scroll_frame import ThinScrollFrame


class ChangeTree(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="变更内容", font=Font.SUBTITLE,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=0, column=0, sticky="w",
                  padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 8))

        self.scroll = ThinScrollFrame(self)
        self.scroll.grid(row=1, column=0, sticky="nsew",
                         padx=8, pady=(0, Size.PAD_PANEL))
        self.scroll.grid_columnconfigure(0, weight=1)

        self._diff: DiffResult | None = None
        self._expanded: bool = False
        self._body_frame: ctk.CTkFrame | None = None
        self._toggle_btn: ctk.CTkButton | None = None

    def load(self, diff: DiffResult | None):
        self._diff = diff
        self._expanded = False
        self._rebuild()

    def _rebuild(self):
        for w in self.scroll.winfo_children():
            w.destroy()

        if self._diff is None:
            ctk.CTkLabel(self.scroll,
                         text="尚无变更，请先拖入更新包并扫描",
                         font=Font.SMALL, text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT).pack(pady=20)
            return

        d = self._diff
        root_frame = ctk.CTkFrame(self.scroll, fg_color=Color.TRANSPARENT)
        root_frame.pack(fill="x", pady=(0, 4))
        root_frame.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(root_frame, fg_color=Color.TRANSPARENT)
        header.grid(row=0, column=0, sticky="ew")

        toggle = ctk.CTkButton(
            header, text="▶  整合包根目录", anchor="w",
            font=Font.BODY_B, fg_color=Color.TRANSPARENT,
            hover_color=Color.BORDER, text_color=Color.TEXT_PRIMARY,
            corner_radius=6, height=30,
            command=self._toggle)
        toggle.pack(side="left", fill="x", expand=True)
        self._toggle_btn = toggle

        summary = ctk.CTkFrame(header, fg_color=Color.TRANSPARENT)
        summary.pack(side="right", padx=6)
        self._summary_label(summary, f"+{len(d.added)}", Color.ADDED)
        self._summary_label(summary, f"~{len(d.modified)}", Color.MODIFIED)
        self._summary_label(summary, f"-{len(d.deleted)}", Color.DELETED)

        self._body_frame = ctk.CTkFrame(self.scroll, fg_color=Color.TRANSPARENT)
        self._build_body()

    def _summary_label(self, parent, text, color):
        ctk.CTkLabel(parent, text=text, font=Font.SMALL,
                     text_color=color, fg_color=Color.TRANSPARENT)\
            .pack(side="left", padx=2)

    def _build_body(self):
        if self._diff is None or self._body_frame is None:
            return

        groups: dict[str, dict[str, list[Path]]] = defaultdict(
            lambda: {"added": [], "modified": [], "deleted": []})

        for kind_name, changes in (("added", self._diff.added),
                                   ("modified", self._diff.modified),
                                   ("deleted", self._diff.deleted)):
            for ch in changes:
                top = (ch.rel_path.parts[0]
                       if len(ch.rel_path.parts) > 1 else "（根目录）")
                groups[top][kind_name].append(ch.rel_path)

        for folder in sorted(groups):
            g = groups[folder]
            total = len(g["added"]) + len(g["modified"]) + len(g["deleted"])
            row = ctk.CTkFrame(self._body_frame, fg_color=Color.TRANSPARENT)
            row.pack(fill="x", padx=(16, 0), pady=1)

            ctk.CTkLabel(row, text=f"📁 {folder}", font=Font.BODY,
                         text_color=Color.TEXT_PRIMARY,
                         fg_color=Color.TRANSPARENT)\
                .pack(side="left")
            ctk.CTkLabel(row, text=f"({total})", font=Font.SMALL,
                         text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT)\
                .pack(side="left", padx=4)

            for kind_name, color in (("added", Color.ADDED),
                                     ("modified", Color.MODIFIED),
                                     ("deleted", Color.DELETED)):
                for rel in g[kind_name]:
                    name = rel.name if len(rel.parts) > 1 else str(rel)
                    ctk.CTkLabel(self._body_frame,
                                 text=f"    {name}",
                                 font=Font.SMALL, text_color=color,
                                 fg_color=Color.TRANSPARENT, anchor="w")\
                        .pack(fill="x", padx=(32, 0))

    def _toggle(self):
        if self._body_frame is None or self._toggle_btn is None:
            return
        self._expanded = not self._expanded
        if self._expanded:
            self._body_frame.pack(fill="x", pady=(4, 0))
            self._toggle_btn.configure(text="▼  整合包根目录")
        else:
            self._body_frame.pack_forget()
            self._toggle_btn.configure(text="▶  整合包根目录")