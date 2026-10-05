"""
option_panel.py — 更新选项面板
------------------------------------------------
左列：可点击渐变色条目（ToggleItem）替代复选框
右列：三连策略按钮组（StrategyGroup）
底部：统一策略（循环切换） / 推荐策略（状态感知）
"""

from collections.abc import Callable

import customtkinter as ctk

from ...config import (
    DEFAULT_CHECKED,
    DEFAULT_STRATEGY,
    STRATEGY_LIST,
    ContentType,
    Strategy,
)
from ...theme import Color, Font, Size
from .strategy_group import StrategyGroup
from .toggle_item import ToggleItem


class OptionPanel(ctk.CTkFrame):
    def __init__(self, master, title: str = "更新选项",
                 on_change: Callable[[], None] | None = None, **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self.on_change = on_change
        self._items: dict[ContentType, ToggleItem] = {}
        self._groups: dict[ContentType, StrategyGroup] = {}

        # 推荐值快照（用于"推荐策略"按钮的启用判定）
        self._recommended_checked: dict[ContentType, bool] = dict(DEFAULT_CHECKED)
        self._recommended_strategies: dict[ContentType, Strategy] = dict(DEFAULT_STRATEGY)

        # 恢复期间抑制 on_change，避免状态判定被中间态干扰
        self._suppress = False

        self._build(title)

    # ------------------------------------------------------------------
    def _build(self, title: str):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text=title, font=Font.SUBTITLE,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=0, column=0, columnspan=2, sticky="w",
                  padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 10))

        for i, ct in enumerate(ContentType):
            row = i + 1

            item = ToggleItem(self, text=ct.label,
                              selected=DEFAULT_CHECKED[ct],
                              on_toggle=lambda _v: self._fire_change())
            item.grid(row=row, column=0, sticky="w",
                      padx=(Size.PAD_PANEL, 10), pady=5)
            self._items[ct] = item

            group = StrategyGroup(self, value=DEFAULT_STRATEGY[ct],
                                  on_change=lambda _s: self._fire_change())
            group.grid(row=row, column=1, sticky="w", pady=5)
            self._groups[ct] = group

        # 底部按钮栏
        btn_row = len(ContentType) + 1
        bar = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        bar.grid(row=btn_row, column=0, columnspan=2, sticky="w",
                 padx=Size.PAD_PANEL, pady=(12, Size.PAD_PANEL))

        self.unify_btn = ctk.CTkButton(
            bar, text="统一策略", width=100, height=32,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_unify)
        self.unify_btn.pack(side="left", padx=(0, 8))

        self.recommend_btn = ctk.CTkButton(
            bar, text="推荐策略", width=100, height=32,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_recommend)
        self.recommend_btn.pack(side="left")

        # 初始状态：与推荐值一致，按钮灰色禁用
        self._update_recommend_state()

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def get_checked(self) -> dict[ContentType, bool]:
        return {ct: item.get() for ct, item in self._items.items()}

    def get_strategies(self) -> dict[ContentType, Strategy]:
        return {ct: g.get() for ct, g in self._groups.items()}

    # ------------------------------------------------------------------
    # 外部设置
    # ------------------------------------------------------------------
    def set_checked(self, mapping: dict):
        self._suppress = True
        try:
            for ct, val in mapping.items():
                if ct in self._items:
                    self._items[ct].set_selected(bool(val))
        finally:
            self._suppress = False
        self._fire_change()

    def set_strategies(self, mapping: dict):
        self._suppress = True
        try:
            for ct, strat in mapping.items():
                if ct in self._groups:
                    self._groups[ct].set_value(strat, notify=False)
        finally:
            self._suppress = False
        self._fire_change()

    # ------------------------------------------------------------------
    # 推荐值管理
    # ------------------------------------------------------------------
    def set_recommended(self, checked: dict | None = None,
                        strategies: dict | None = None):
        """
        设置推荐值快照（由外部在读取更新包元数据后调用）。
        传入 None 时使用默认值。
        """
        self._recommended_checked = dict(checked or DEFAULT_CHECKED)
        self._recommended_strategies = dict(strategies or DEFAULT_STRATEGY)
        self._update_recommend_state()

    def _is_at_recommended(self) -> bool:
        """当前配置是否与推荐值一致"""
        cur_c = self.get_checked()
        cur_s = self.get_strategies()
        return (cur_c == self._recommended_checked
                and cur_s == self._recommended_strategies)

    def _update_recommend_state(self):
        """
        根据当前配置是否等于推荐值，切换按钮的可用状态：
          一致 → 灰色禁用
          不一致 → 青色可点击
        """
        try:
            at_rec = self._is_at_recommended()
        except Exception:
            at_rec = True

        if at_rec:
            self.recommend_btn.configure(
                state="disabled",
                fg_color=Color.LOG_BG,
                text_color=Color.TEXT_MUTED,
                hover_color=Color.LOG_BG)
        else:
            self.recommend_btn.configure(
                state="normal",
                fg_color=Color.ACCENT,
                text_color=Color.TEXT_ON_ACCENT,
                hover_color=Color.ACCENT_HOVER)

    # ------------------------------------------------------------------
    # 按钮事件
    # ------------------------------------------------------------------
    def _on_unify(self):
        """
        统一策略：所有条目的策略同时推进到下一个。
        以第一个条目的当前策略为基准 +1，全部同步。
        """
        first = next(iter(ContentType))
        cur = self._groups[first].get()
        idx = STRATEGY_LIST.index(cur)
        next_strat = STRATEGY_LIST[(idx + 1) % len(STRATEGY_LIST)]

        # 不抑制 on_change，让每个 group 正常跑动画；
        # 结束后统一触发一次外部回调。
        for g in self._groups.values():
            g.set_value(next_strat, animate=True, notify=False)

        self._fire_change()

    def _on_recommend(self):
        """恢复推荐值"""
        self.set_strategies(dict(self._recommended_strategies))
        self.set_checked(dict(self._recommended_checked))

    # ------------------------------------------------------------------
    def _fire_change(self):
        """状态变化：更新推荐按钮状态 + 通知外部"""
        if self._suppress:
            return
        self._update_recommend_state()
        if self.on_change:
            self.on_change()