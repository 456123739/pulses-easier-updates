"""
sidebar.py — 侧边栏
------------------------------------------------
从上到下：
  1. Logo 区
  2. 定位区（拖入框 / 信息卡片 二选一）
  3. 最近打开列表
  4. 白名单面板（仅开发者身份显示）
  5. 更多面板（清理缓存等）
  6. 弹性空白
  7. 底部身份切换

状态由 AppState 驱动。
支持 set_locked 在更新期间锁定"更多"面板。
"""

from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ..core import recent
from ..core.pack_info import ModpackInfo, read_modpack_info
from ..core.pack_locator import locate_modpack
from ..core.state import AppState
from ..theme import Color, Font, Size
from .widgets.drop_zone import DropZone
from .widgets.info_card import InfoCard
from .widgets.more_panel import MorePanel
from .widgets.whitelist_panel import WhitelistPanel

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOGO_PATH = _PROJECT_ROOT / "assets" / "logo.png"


class Sidebar(ctk.CTkFrame):
    def __init__(self, master,
                 app_state: AppState,
                 on_identity_changed: Callable[[str], None] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.SIDEBAR_BG,
                         corner_radius=0, width=Size.SIDEBAR_W, **kwargs)
        self.grid_propagate(False)

        self.app_state = app_state
        self.on_identity_changed = on_identity_changed
        self._identity = "玩家"
        self._logo_image: ctk.CTkImage | None = None
        self._current_info: ModpackInfo | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)   # 弹性空白

        self._build_logo()                    # row 0
        self._build_locate_area()             # row 1
        self._build_recent()                  # row 2
        self._build_whitelist()               # row 3
        self._build_more()                    # row 4
        self._build_identity_switch()         # 底部 place

        self.app_state.on_modpack_changed(self._on_modpack_changed)
        self.app_state.on_lock_changed(self._on_lock_changed)

        self._render_locate_area()
        self._refresh_recent()
        self.set_identity(self._identity)

    # ------------------------------------------------------------------
    def _build_logo(self):
        f = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        f.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 0))
        f.grid_columnconfigure(0, weight=1)

        image = self._load_logo_image()
        if image is not None:
            self._logo_image = image
            ctk.CTkLabel(f, text="", image=image,
                         fg_color=Color.TRANSPARENT)\
                .grid(row=0, column=0, sticky="ew")
        else:
            ctk.CTkLabel(f, text="Pulses Easier",
                         font=(Font.FAMILY, 18, "bold"),
                         text_color=Color.TEXT_PRIMARY,
                         fg_color=Color.TRANSPARENT)\
                .grid(row=0, column=0, sticky="ew", pady=8)

        ctk.CTkLabel(f, text="Produced by Pulses0 Studio.",
                     font=Font.TINY, text_color=Color.TEXT_MUTED,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=1, column=0, sticky="w", pady=(6, 0))
        ctk.CTkLabel(f, text="Made by NimShade & DS.",
                     font=Font.TINY, text_color=Color.TEXT_MUTED,
                     fg_color=Color.TRANSPARENT)\
            .grid(row=2, column=0, sticky="w", pady=(2, 0))

    def _load_logo_image(self) -> ctk.CTkImage | None:
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001
            return None
        try:
            if not _LOGO_PATH.is_file():
                return None
            pil = Image.open(_LOGO_PATH).convert("RGBA")
            target_w = Size.SIDEBAR_W - 32
            w, h = pil.size
            if w <= 0 or h <= 0:
                return None
            target_h = max(1, int(h * (target_w / w)))
            max_h = 120
            if target_h > max_h:
                scale = max_h / target_h
                target_w = int(target_w * scale)
                target_h = max_h
            return ctk.CTkImage(light_image=pil, dark_image=pil,
                                size=(target_w, target_h))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    def _build_locate_area(self):
        self.locate_wrap = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        self.locate_wrap.grid(row=1, column=0, sticky="ew",
                              padx=16, pady=(12, 0))
        self.locate_wrap.grid_columnconfigure(0, weight=1)

        self.drop_zone = DropZone(
            self.locate_wrap,
            title="STEP 1 · 定位整合包",
            subtitle="拖入整合包内任意文件",
            height=Size.DROP_H_SIDEBAR,
            on_drop=self._on_files_dropped,
            highlight=True)
        self.drop_zone.grid(row=0, column=0, sticky="ew")

        self.info_card = InfoCard(self.locate_wrap,
                                  on_clear=self._on_clear_clicked)
        self.info_card.grid(row=1, column=0, sticky="ew")
        self.info_card.grid_remove()

    def _render_locate_area(self):
        path = self.app_state.current_modpack_path
        if path is None:
            self.info_card.grid_remove()
            self.drop_zone.grid()
            try:
                self.drop_zone.set_highlight(True)
            except Exception:  # noqa: BLE001, S110
                pass
        else:
            self.drop_zone.grid_remove()
            self.info_card.grid()

    # ------------------------------------------------------------------
    def _on_modpack_changed(self, path: Path | None):
        if path is None:
            self._current_info = None
        else:
            try:
                self._current_info = read_modpack_info(path)
            except Exception:  # noqa: BLE001
                self._current_info = None

        self._render_locate_area()
        if self._current_info is not None:
            self.info_card.set_info(self._current_info)
            self.info_card.set_locked(self.app_state.is_locked)

    def _on_lock_changed(self, locked: bool):
        self.info_card.set_locked(locked)

    # ------------------------------------------------------------------
    def _on_clear_clicked(self):
        if self.app_state.is_locked:
            return
        self.app_state.clear_modpack()
        try:
            self.drop_zone.set_highlight(True)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_files_dropped(self, paths: list[Path]):
        if self.app_state.is_locked:
            return

        root = None
        for p in paths:
            located = locate_modpack(str(p))
            if located:
                root = Path(located)
                break

        if not root:
            return

        self.app_state.set_modpack(root)
        recent.add_recent(root)
        self._refresh_recent()
        try:
            self.drop_zone.set_highlight(False)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _build_recent(self):
        self.recent_card = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                                        corner_radius=Size.RADIUS_CARD)

        header = ctk.CTkFrame(self.recent_card, fg_color=Color.TRANSPARENT)
        header.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(header, text="最近打开", font=Font.BODY_B,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .pack(side="left")
        ctk.CTkButton(header, text="清空", width=40, height=20,
                      font=Font.TINY, corner_radius=6,
                      fg_color=Color.TRANSPARENT,
                      hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self._on_clear_recent)\
            .pack(side="right")

        self.recent_list = ctk.CTkFrame(self.recent_card,
                                        fg_color=Color.TRANSPARENT)
        self.recent_list.pack(fill="x", padx=8, pady=(0, 12))

    def _refresh_recent(self):
        for w in self.recent_list.winfo_children():
            w.destroy()

        items = recent.load_recent()
        if not items:
            ctk.CTkLabel(self.recent_list, text="暂无记录",
                         font=Font.SMALL, text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT)\
                .pack(anchor="w", padx=4, pady=4)
        else:
            for path in items:
                self._add_recent_item(path)

        self.recent_card.grid(row=2, column=0, sticky="ew",
                              padx=16, pady=(12, 0))

    def _add_recent_item(self, path: Path):
        name = path.name or str(path)
        ctk.CTkButton(
            self.recent_list, text=name, anchor="w",
            font=Font.SMALL, corner_radius=6, height=26,
            fg_color=Color.TRANSPARENT,
            hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=lambda p=path: self._on_recent_clicked(p))\
            .pack(fill="x", padx=4, pady=1)

    def _on_recent_clicked(self, path: Path):
        if self.app_state.is_locked:
            return
        if not path.is_dir():
            recent.remove_recent(path)
            self._refresh_recent()
            return
        located = locate_modpack(str(path))
        if not located:
            recent.remove_recent(path)
            self._refresh_recent()
            return
        self.app_state.set_modpack(Path(located))
        recent.add_recent(Path(located))
        self._refresh_recent()
        try:
            self.drop_zone.set_highlight(False)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_clear_recent(self):
        recent.clear_recent()
        self._refresh_recent()

    # ------------------------------------------------------------------
    def _build_whitelist(self):
        self.whitelist_panel = WhitelistPanel(self)
        self.whitelist_panel.grid(row=3, column=0, sticky="ew",
                                  padx=16, pady=(12, 0))
        self.whitelist_panel.grid_remove()

    def _build_more(self):
        """更多面板：清理缓存等"""
        self.more_panel = MorePanel(self)
        self.more_panel.grid(row=4, column=0, sticky="ew",
                             padx=16, pady=(12, 0))

    def set_identity(self, identity: str):
        """切换身份时同步侧边栏内容"""
        self._identity = identity
        try:
            if identity == "开发者":
                self.whitelist_panel.grid()
                self.drop_zone.set_highlight(False)
            else:
                self.whitelist_panel.grid_remove()
                if self.app_state.current_modpack_path is None:
                    self.drop_zone.set_highlight(True)
        except Exception:  # noqa: BLE001, S110
            pass

    def set_locked(self, locked: bool):
        """更新期间锁定侧边栏的"更多"面板"""
        try:
            self.more_panel.set_locked(locked)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _build_identity_switch(self):
        wrap = ctk.CTkFrame(self, fg_color=Color.SIDEBAR_BG,
                            height=50)
        wrap.place(relx=0, rely=1.0, relwidth=1.0,
                   anchor="sw", x=0, y=-12)
        wrap.grid_propagate(False)
        wrap.pack_propagate(False)

        inner = ctk.CTkFrame(wrap, fg_color=Color.TRANSPARENT)
        inner.pack(fill="both", expand=True, padx=12)

        self.identity_switch = ctk.CTkSegmentedButton(  # type: ignore[call-arg]
            inner, values=["玩家", "开发者"],
            font=Font.BODY, height=Size.SEG_BTN_H,
            fg_color=Color.CARD_BG,
            selected_color=Color.ACCENT,
            selected_hover_color=Color.ACCENT_HOVER,
            unselected_color=Color.CARD_BG,
            unselected_hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_identity)
        self.identity_switch.set("玩家")
        self.identity_switch.pack(fill="x")

    def _on_identity(self, value: str):
        if self.on_identity_changed:
            self.on_identity_changed(value)