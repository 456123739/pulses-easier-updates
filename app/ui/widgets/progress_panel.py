"""
progress_panel.py — 进度面板
------------------------------------------------
- 顶部一行当前步骤文字，超出用 ... 省略
- 下方进度条 + 百分比
- 面板宽度由父容器决定，不撑开
"""

from pathlib import Path

import customtkinter as ctk

from ...theme import Color, Font, Size

_MAX_LABEL_LEN = 48


def _shorten(text: str, max_len: int = _MAX_LABEL_LEN) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    head_len = int(max_len * 0.6)
    tail_len = max_len - head_len - 1
    return text[:head_len] + "…" + text[-tail_len:]


def _shorten_path(raw: str, max_len: int = _MAX_LABEL_LEN) -> str:
    if not raw:
        return ""
    p = Path(raw)
    parts = p.parts
    if len(raw) <= max_len:
        return raw
    if len(parts) >= 3:
        head = parts[0]
        tail = parts[-1]
        candidate = f"{head}/…/{tail}"
        if len(candidate) <= max_len:
            return candidate
        return _shorten(candidate, max_len)
    return _shorten(raw, max_len)


class ProgressPanel(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        # 高度固定，宽度由父容器决定
        self.grid_propagate(False)

        self.grid_columnconfigure(0, weight=1)

        self.label = ctk.CTkLabel(
            self, text="就绪",
            font=Font.SMALL, text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT, anchor="w",
            height=18)
        self.label.grid(row=0, column=0, sticky="ew",
                        padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 6))

        row = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        row.grid(row=1, column=0, sticky="ew",
                 padx=Size.PAD_PANEL, pady=(0, Size.PAD_PANEL))
        row.grid_columnconfigure(0, weight=1)

        self.bar = ctk.CTkProgressBar(
            row, height=8, corner_radius=4,
            fg_color=Color.BORDER, progress_color=Color.ACCENT)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.bar.set(0)

        self.pct = ctk.CTkLabel(
            row, text="0%",
            font=(Font.FAMILY, 13, "bold"),
            text_color=Color.ACCENT,
            fg_color=Color.TRANSPARENT, width=48, anchor="e")
        self.pct.grid(row=0, column=1, sticky="e", padx=(8, 0))

    def update_progress(self, done: int, total: int, label: str = ""):
        if label:
            text = _shorten_path(label, _MAX_LABEL_LEN)
            try:
                self.label.configure(text=text)
            except Exception:  # noqa: BLE001, S110
                pass
        ratio = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
        try:
            self.bar.set(ratio)
            self.pct.configure(text=f"{int(ratio * 100)}%")
        except Exception:  # noqa: BLE001, S110
            pass

    def reset(self, label: str = "就绪"):
        self.update_progress(0, 1, label)