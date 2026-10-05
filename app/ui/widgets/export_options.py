"""
export_options.py — 导出选项面板
------------------------------------------------
四个开关（压缩恒开，不暴露）：
  - 自带文件结构校验数据
  - 防篡改
  - 预存 overrides 文件结构
  - 最小体积（不导出被禁用条目，减小体积）

对外接口：
  get_options() -> dict
  set_options(dict)
"""

from collections.abc import Callable

import customtkinter as ctk

from ...core.database import DEFAULT_EXPORT_OPTIONS
from ...theme import Color, Font, Size

_OPTION_LABELS = [
    ("include_hashes", "自带文件结构校验数据", "在更新包内附带哈希清单"),
    ("tamper_proof", "防篡改", "生成签名校验数据，防止更新包被修改"),
    ("precompute_overrides", "预存 overrides 文件结构", "提前存目录结构和哈希，玩家端比对更快"),
    ("min_size", "最小体积", "不导出被禁用条目，减小更新包体积"),
]


class ExportOptionsPanel(ctk.CTkFrame):
    def __init__(self, master,
                 on_change: Callable[[], None] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self.on_change = on_change
        self._vars: dict[str, ctk.BooleanVar] = {}

        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="导出选项", font=Font.SUBTITLE,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=0, column=0, sticky="w",
                  padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 8))

        for i, (key, label, hint) in enumerate(_OPTION_LABELS):
            row = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
            row.grid(row=i + 1, column=0, sticky="ew",
                     padx=Size.PAD_PANEL, pady=(2, 2))

            var = ctk.BooleanVar(
                value=bool(DEFAULT_EXPORT_OPTIONS.get(key, False)))
            self._vars[key] = var

            cb = ctk.CTkCheckBox(
                row, text=label, variable=var,
                font=Font.BODY, text_color=Color.TEXT_PRIMARY,
                fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
                checkmark_color=Color.TEXT_ON_ACCENT,
                border_color=Color.BORDER, corner_radius=4,
                command=self._fire_change)
            cb.pack(anchor="w")

            ctk.CTkLabel(row, text=hint, font=Font.TINY,
                         text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT, anchor="w")\
                .pack(anchor="w", padx=(28, 0))

        ctk.CTkFrame(self, fg_color=Color.TRANSPARENT, height=4)\
            .grid(row=len(_OPTION_LABELS) + 1, column=0)

    def get_options(self) -> dict:
        opts = {key: bool(var.get()) for key, var in self._vars.items()}
        opts["compress"] = True
        return opts

    def set_options(self, opts: dict):
        for key, var in self._vars.items():
            if key in opts:
                var.set(bool(opts[key]))
        self._fire_change()

    def _fire_change(self):
        if self.on_change:
            self.on_change()