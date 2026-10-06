"""
more_panel.py — 侧边栏"更多"折叠面板
------------------------------------------------
折叠式，标题行"更多" + 三角折叠按钮。
放置「首选项」「清理缓存」。
支持 set_locked 在更新期间禁用操作。

（v0.5.0 去掉了"回滚上次更新"：向后恢复需要假设"更新后用户什么都没
改过"，无法验证，容易把版本搞乱；恢复方向统一为"重新拖入更新包再更新"。）
"""

from collections.abc import Callable

import customtkinter as ctk

from ...core import cache as cache_mod
from ...theme import Color, Font, Size
from .dialog import alert
from .preferences_dialog import open_preferences


class MorePanel(ctk.CTkFrame):
    def __init__(self, master,
                 on_change: Callable[[], None] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self.on_change = on_change
        self._collapsed = True
        self._locked = False

        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        header.grid(row=0, column=0, sticky="ew",
                    padx=12, pady=(12, 6))
        header.grid_columnconfigure(1, weight=1)

        self.arrow = ctk.CTkButton(
            header, text="▶", width=24, height=24,
            font=Font.SMALL, corner_radius=6,
            fg_color=Color.TRANSPARENT, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._toggle)
        self.arrow.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(header, text="更多",
                     font=Font.BODY_B, text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=1, sticky="w", padx=(4, 0))

        self.body = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        self.body.grid(row=1, column=0, sticky="ew",
                       padx=12, pady=(0, 12))
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_remove()

        self.prefs_btn = ctk.CTkButton(
            self.body, text="首选项",
            height=32, font=Font.SMALL,
            corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_prefs_click)
        self.prefs_btn.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        self.clean_btn = ctk.CTkButton(
            self.body, text="清理缓存",
            height=32, font=Font.SMALL,
            corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_clean_click)
        self.clean_btn.grid(row=1, column=0, sticky="ew", pady=(0, 4))

        self.db_btn = ctk.CTkButton(
            self.body, text="数据库位置",
            height=32, font=Font.SMALL,
            corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_db_click)
        self.db_btn.grid(row=2, column=0, sticky="ew", pady=(0, 4))

        self.size_label = ctk.CTkLabel(
            self.body, text="",
            font=Font.TINY, text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT, anchor="w")
        self.size_label.grid(row=3, column=0, sticky="ew")

    def _on_db_click(self):
        """数据库位置：切换 / 迁移（原目录永不自动删除）。"""
        if self._locked:
            return
        try:
            from .db_panel import open_db_settings
            open_db_settings(self.winfo_toplevel(),
                             on_done=self._on_db_changed)
        except Exception as e:  # noqa: BLE001
            alert(self.winfo_toplevel(), "打不开设置", str(e), level="error")

    def _on_db_changed(self):
        """数据库换了之后：刷新缓存大小等依赖它的显示。"""
        try:
            self._refresh_size()
            if self.on_change is not None:
                self.on_change()
        except Exception:  # noqa: BLE001, S110
            pass

    def _toggle(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.arrow.configure(text="▶")
            self.body.grid_remove()
        else:
            self.arrow.configure(text="▼")
            self.body.grid()
            self._refresh_size()

    def _refresh_size(self):
        try:
            from ...utils.files import human_size
            size = cache_mod.cache_size_bytes()
            self.size_label.configure(
                text=f"当前缓存：{human_size(size)}" if size else "缓存为空")
        except Exception:  # noqa: BLE001, S110
            pass

    def set_locked(self, locked: bool):
        self._locked = locked
        try:
            state = "disabled" if locked else "normal"
            for btn in (self.prefs_btn, self.clean_btn, self.db_btn):
                btn.configure(
                    state=state,
                    fg_color=Color.LOG_BG,
                    text_color=(Color.TEXT_MUTED if locked
                                else Color.TEXT_PRIMARY),
                    hover_color=(Color.LOG_BG if locked else Color.BORDER))
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_prefs_click(self):
        if self._locked:
            return
        try:
            open_preferences(self.winfo_toplevel())
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_clean_click(self):
        if self._locked:
            return
        ok, msg = cache_mod.clean_cache()
        self._refresh_size()
        parent = self.winfo_toplevel()
        if ok:
            if "跳过" in msg:
                alert(parent, "清理缓存", msg, level="warn")
            else:
                alert(parent, "清理缓存", msg, level="success")
        else:
            alert(parent, "清理缓存", msg, level="error")
        if self.on_change:
            self.on_change()