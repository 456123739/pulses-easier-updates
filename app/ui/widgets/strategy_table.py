"""
strategy_table.py — 动态策略表
------------------------------------------------
- 按顶层文件夹 + 根目录散文件动态生成条目
- 文件夹 📁 / 文件 📄 图标
- 名称列宽统一，超长截断 + 悬停显示完整
- 勾选 / 未勾选沉底折叠
- 三连策略按钮（完全匹配 / 替换重名 / 跳过重名）
- 统一策略：受保护文件夹（默认 mods）不参与
- 导入 / 导出配置
- 无滚动条滚动
- 无空状态提示
- on_change 回调带 source 参数（"toggle"/"strategy"/"unify"/"import"/"external"）
"""

from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ...config import STRATEGY_LIST, Strategy
from ...theme import Color, Font, Size
from .strategy_group import StrategyGroup
from .toggle_item import ToggleItem


class _Row:
    def __init__(self, folder: str,
                 checked: bool = True,
                 strategy: Strategy = Strategy.FULL_MATCH):
        self.folder = folder
        self.checked = checked
        self.strategy = strategy
        self.frame: ctk.CTkFrame | None = None
        self.toggle: ToggleItem | None = None
        self.group: StrategyGroup | None = None
        self.body: ctk.CTkFrame | None = None


class StrategyTable(ctk.CTkFrame):
    def __init__(self, master, title: str = "更新策略",
                 on_change: Callable[[str], None] | None = None,
                 protected_folders: list[str] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self.on_change = on_change
        self._protected = set(protected_folders or ["mods"])

        self._rows: dict[str, _Row] = {}
        self._blacklist: set[str] = set()
        self._file_items: set[str] = set()
        self._suppress = False
        self._collapsed = True
        self._name_col_width: int = 200

        self._disabled_header: ctk.CTkFrame | None = None
        self._disabled_label: ctk.CTkLabel | None = None
        self._disabled_arrow: ctk.CTkButton | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header(title)
        self._build_scroll_area()
        self._build_footer()

    # ------------------------------------------------------------------
    def _build_header(self, title: str):
        bar = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        bar.grid(row=0, column=0, sticky="ew",
                 padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 8))

        ctk.CTkLabel(bar, text=title, font=Font.SUBTITLE,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .pack(side="left")

        ctk.CTkButton(bar, text="导入配置", width=80, height=26,
                      font=Font.TINY, corner_radius=6,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self._on_import)\
            .pack(side="right", padx=(4, 0))

        ctk.CTkButton(bar, text="导出配置", width=80, height=26,
                      font=Font.TINY, corner_radius=6,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self._on_export)\
            .pack(side="right", padx=(4, 0))

    def _build_scroll_area(self):
        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color=Color.TRANSPARENT, corner_radius=0,
            scrollbar_button_color=Color.CARD_BG,
            scrollbar_button_hover_color=Color.CARD_BG,
        )
        self.scroll.grid(row=1, column=0, sticky="nsew",
                         padx=Size.PAD_PANEL)
        self.scroll.grid_columnconfigure(0, weight=1)

        try:
            self.scroll._scrollbar.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass

        self.rows_frame = self.scroll
        self.rows_frame.grid_columnconfigure(0, weight=1)

        self.empty_label = ctk.CTkLabel(
            self.rows_frame, text="",
            font=Font.SMALL, text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT)
        self.empty_label.grid(row=0, column=0, sticky="w", pady=0)
        self.empty_label.grid_remove()

    def _build_footer(self):
        bar = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        bar.grid(row=2, column=0, sticky="w",
                 padx=Size.PAD_PANEL, pady=(12, Size.PAD_PANEL))

        ctk.CTkButton(
            bar, text="统一策略", width=100, height=32,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_unify)\
            .pack(side="left")

    # ------------------------------------------------------------------
    def set_folders(self, folders: list[str],
                    file_items: set[str] | None = None):
        if file_items is None:
            file_items = set()
        self._file_items = file_items

        wanted = set(folders)
        for name in list(self._rows.keys()):
            if name not in wanted:
                r = self._rows.pop(name)
                if r.frame is not None:
                    try:
                        r.frame.destroy()
                    except Exception:  # noqa: BLE001, S110
                        pass

        max_len = 0
        for name in folders:
            is_file = name in self._file_items
            display = (f"📄 {name}" if is_file else f"📁 {name}")
            max_len = max(max_len, min(len(display), 25))
        self._name_col_width = max(160, min(280, max_len * 8 + 24))

        for name in folders:
            if name in self._rows:
                continue
            checked = name not in self._blacklist
            r = _Row(name, checked=checked, strategy=Strategy.FULL_MATCH)
            self._rows[name] = r
            self._create_row_widgets(r)

        self.empty_label.grid_remove()
        self._apply_layout()

    def set_blacklist(self, items: list[str]):
        self._blacklist = set(items or [])

    def get_blacklist(self) -> list[str]:
        return [name for name, r in self._rows.items() if not r.checked]

    def get_checked(self) -> dict[str, bool]:
        return {name: r.checked for name, r in self._rows.items()}

    def get_strategies(self) -> dict[str, str]:
        return {name: r.strategy.value for name, r in self._rows.items()}

    def set_strategies(self, mapping: dict):
        self._suppress = True
        try:
            for name, strat in mapping.items():
                r = self._rows.get(name)
                if not r:
                    continue
                try:
                    s = Strategy(strat) if isinstance(strat, str) else strat
                except ValueError:
                    continue
                r.strategy = s
                if r.group is not None:
                    r.group.set_value(s, animate=False, notify=False)
        finally:
            self._suppress = False
        self._fire_change("external")

    # ------------------------------------------------------------------
    def _create_row_widgets(self, r: _Row):
        frame = ctk.CTkFrame(self.rows_frame, fg_color=Color.TRANSPARENT)
        frame.grid_columnconfigure(1, weight=1)
        r.frame = frame

        is_file = r.folder in self._file_items
        label = f"📄 {r.folder}" if is_file else f"📁 {r.folder}"

        toggle = ToggleItem(
            frame, text=label, selected=r.checked,
            on_toggle=lambda _v, name=r.folder: self._on_toggle(name),
            width=self._name_col_width)
        toggle.grid(row=0, column=0, sticky="w", padx=(0, 10))
        r.toggle = toggle

        body = ctk.CTkFrame(frame, fg_color=Color.TRANSPARENT)
        body.grid(row=0, column=1, sticky="ew")
        body.grid_columnconfigure(0, weight=1)
        r.body = body

        group = StrategyGroup(
            body, value=r.strategy,
            on_change=lambda _s, name=r.folder: self._on_strategy(name))
        group.grid(row=0, column=0, sticky="w")
        r.group = group

    def _apply_layout(self):
        enabled: list[_Row] = []
        disabled: list[_Row] = []
        for r in self._rows.values():
            (enabled if r.checked else disabled).append(r)

        next_row = 0
        for r in enabled:
            if r.frame is None:
                continue
            r.frame.grid(row=next_row, column=0, sticky="ew", pady=3)
            r.frame.grid()
            if r.body is not None:
                r.body.grid()
            next_row += 1

        if disabled:
            self._ensure_disabled_header()
            assert self._disabled_header is not None
            self._disabled_header.grid(row=next_row, column=0,
                                       sticky="ew", pady=(8, 2))
            self._disabled_header.grid()
            next_row += 1

            if self._disabled_label is not None:
                self._disabled_label.configure(
                    text=f"已禁用（{len(disabled)}）")
            if self._disabled_arrow is not None:
                self._disabled_arrow.configure(
                    text="▼" if not self._collapsed else "▶")

            for r in disabled:
                if r.frame is None:
                    continue
                if self._collapsed:
                    r.frame.grid_remove()
                else:
                    r.frame.grid(row=next_row, column=0,
                                 sticky="ew", pady=3)
                    r.frame.grid()
                    if r.body is not None:
                        r.body.grid_remove()
                    next_row += 1
        else:
            if self._disabled_header is not None:
                self._disabled_header.grid_remove()

    def _ensure_disabled_header(self):
        if self._disabled_header is not None:
            return
        header = ctk.CTkFrame(self.rows_frame, fg_color=Color.TRANSPARENT)
        header.grid_columnconfigure(1, weight=1)
        self._disabled_header = header

        arrow = ctk.CTkButton(
            header, text="▶", width=24, height=24,
            font=Font.SMALL, corner_radius=6,
            fg_color=Color.TRANSPARENT, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._toggle_collapsed)
        arrow.grid(row=0, column=0, sticky="w")
        self._disabled_arrow = arrow

        label = ctk.CTkLabel(
            header, text="已禁用（0）",
            font=Font.SMALL, text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT, anchor="w")
        label.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self._disabled_label = label

    # ------------------------------------------------------------------
    def _on_toggle(self, folder: str):
        r = self._rows.get(folder)
        if not r:
            return
        if r.toggle is not None:
            r.checked = r.toggle.get()
        self._apply_layout()
        self._fire_change("toggle")

    def _on_strategy(self, folder: str):
        r = self._rows.get(folder)
        if r and r.group is not None:
            r.strategy = r.group.get()
        self._fire_change("strategy")

    def _toggle_collapsed(self):
        self._collapsed = not self._collapsed
        self._apply_layout()

    def _on_unify(self):
        candidates = [r for r in self._rows.values()
                      if r.checked and r.folder not in self._protected]
        if not candidates:
            return

        cur = candidates[0].strategy
        idx = STRATEGY_LIST.index(cur)
        next_strat = STRATEGY_LIST[(idx + 1) % len(STRATEGY_LIST)]

        for r in candidates:
            r.strategy = next_strat
            if r.group is not None:
                r.group.set_value(next_strat, animate=True, notify=False)
        self._fire_change("unify")

    # ------------------------------------------------------------------
    def _on_export(self):
        from tkinter import filedialog

        from ...core import database as db

        path = filedialog.asksaveasfilename(
            title="导出配置",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile="pulses_profile.json")
        if not path:
            return
        profile = db.build_profile(
            self.get_strategies(),
            db.get_export_options(),
            self.get_blacklist())
        db.export_profile(Path(path), profile)

    def _on_import(self):
        from tkinter import filedialog

        from ...core import database as db

        path = filedialog.askopenfilename(
            title="导入配置",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        profile = db.import_profile(Path(path))
        if not profile:
            return

        strategies = profile.get("strategies", {})
        blacklist = set(profile.get("blacklist", []))
        self._blacklist = blacklist

        for name, r in self._rows.items():
            if name in strategies:
                try:
                    r.strategy = Strategy(strategies[name])
                    if r.group is not None:
                        r.group.set_value(r.strategy, animate=False,
                                          notify=False)
                except (ValueError, KeyError):
                    pass

            r.checked = name not in blacklist
            if r.toggle is not None:
                r.toggle.set_selected(r.checked, animate=False)

        self._apply_layout()
        self._fire_change("import")

    # ------------------------------------------------------------------
    def _fire_change(self, source: str = "external"):
        if self._suppress:
            return
        if self.on_change:
            try:
                self.on_change(source)
            except TypeError:
                # 兼容旧的无参回调
                try:
                    self.on_change()  # type: ignore[call-arg]
                except Exception:  # noqa: BLE001, S110
                    pass
            except Exception:  # noqa: BLE001, S110
                pass