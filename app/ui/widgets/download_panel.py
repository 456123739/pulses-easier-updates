"""
download_panel.py — 下载失败断点投入面板（支持实时追加）
------------------------------------------------
- 支持下载进行中实时追加失败项（add_item）
- 支持空列表初始化
- 列表 + 拖入补入框
"""

from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ...theme import Color, Font, Size
from .drop_zone import DropZone


class DownloadPanel(ctk.CTkFrame):
    """
    参数：
      failed_items: list[dict]    初始失败项（可为空）
      on_file_dropped(rel_path, file_path) -> bool
      on_skip_all() -> None
      in_progress: bool           是否下载仍在进行中
    """

    def __init__(self, master,
                 failed_items: list[dict],
                 on_file_dropped: Callable[[str, Path], bool],
                 on_skip_all: Callable[[], None],
                 in_progress: bool = False,
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self._failed: dict[str, dict] = {it["rel"]: it for it in failed_items}
        self.on_file_dropped = on_file_dropped
        self.on_skip_all = on_skip_all
        self._in_progress = in_progress
        self._row_index = 0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 标题 + 跳过按钮
        head = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        head.grid(row=0, column=0, sticky="ew",
                  padx=Size.PAD_PANEL, pady=(Size.PAD_PANEL, 6))
        head.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            head, text="", font=Font.SUBTITLE,
            text_color=Color.WARNING,
            fg_color=Color.TRANSPARENT, anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(head, text="全部跳过", width=90, height=28,
                      font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
                      fg_color=Color.LOG_BG, hover_color=Color.BORDER,
                      text_color=Color.TEXT_SECONDARY,
                      command=self.on_skip_all)\
            .grid(row=0, column=1, sticky="e")

        # 列表
        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color=Color.TRANSPARENT, corner_radius=0,
            scrollbar_button_color=Color.BORDER,
            scrollbar_button_hover_color=Color.ACCENT)
        self.scroll.grid(row=1, column=0, sticky="nsew",
                         padx=8, pady=(0, 6))
        self.scroll.grid_columnconfigure(0, weight=1)
        try:
            self.scroll._scrollbar.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass

        self.empty_label = ctk.CTkLabel(
            self.scroll, text="暂无失败项",
            font=Font.SMALL, text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT)
        self.empty_label.grid(row=0, column=0, sticky="w",
                              padx=4, pady=6)

        # 初始项
        for item in failed_items:
            self._append_row(item)

        # 投入框
        self.drop = DropZone(
            self,
            title="把文件拖到这里补入",
            subtitle="将重新校验",
            height=Size.DROP_H_WORK - 20,
            mode="any",
            on_drop=self._on_files_dropped)
        self.drop.grid(row=2, column=0, sticky="ew",
                       padx=Size.PAD_PANEL, pady=(0, Size.PAD_PANEL))

        self._refresh_title()
        self._refresh_empty()

    # ------------------------------------------------------------------
    def add_item(self, item: dict):
        """实时追加失败项"""
        rel = item.get("rel", "")
        if not rel or rel in self._failed:
            return
        self._failed[rel] = item
        self._append_row(item)
        self._refresh_title()
        self._refresh_empty()

    def set_in_progress(self, in_progress: bool):
        self._in_progress = in_progress
        self._refresh_title()

    def has_failures(self) -> bool:
        return bool(self._failed)

    # ------------------------------------------------------------------
    def _refresh_title(self):
        n = len(self._failed)
        if self._in_progress:
            text = f"下载中 · 已失败 {n} 项"
        else:
            text = f"下载失败（{n} 项）"
        try:
            self.title_label.configure(text=text)
        except Exception:  # noqa: BLE001, S110
            pass

    def _refresh_empty(self):
        try:
            if self._failed:
                self.empty_label.grid_remove()
            else:
                self.empty_label.grid(row=0, column=0, sticky="w",
                                      padx=4, pady=6)
        except Exception:  # noqa: BLE001, S110
            pass

    def _append_row(self, item: dict):
        row = ctk.CTkFrame(self.scroll, fg_color=Color.TRANSPARENT)
        row.grid(row=self._row_index + 1, column=0, sticky="ew", pady=2)
        row.grid_columnconfigure(0, weight=1)
        self._row_index += 1

        ctk.CTkLabel(row, text=item["rel"], font=Font.SMALL,
                     text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=0, sticky="ew", padx=(4, 4))

        err = item.get("error", "")
        if err:
            ctk.CTkLabel(row, text=err, font=Font.TINY,
                         text_color=Color.ERROR,
                         fg_color=Color.TRANSPARENT, anchor="w")\
                .grid(row=1, column=0, sticky="ew", padx=(4, 4))

        urls = item.get("urls") or []
        if urls:
            ctk.CTkLabel(row, text=urls[0], font=Font.TINY,
                         text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT, anchor="w")\
                .grid(row=2, column=0, sticky="ew", padx=(4, 4))

    # ------------------------------------------------------------------
    def _on_files_dropped(self, paths: list[Path]):
        for p in paths:
            handled = False
            for rel, item in list(self._failed.items()):
                if p.name == Path(rel).name:
                    ok = False
                    try:
                        ok = self.on_file_dropped(rel, p)
                    except Exception:  # noqa: BLE001
                        ok = False
                    if ok:
                        self._failed.pop(rel, None)
                        handled = True
                    break
            if handled and not self._failed:
                try:
                    self.grid_remove()
                except Exception:  # noqa: BLE001, S110
                    pass
        self._refresh_title()
        self._refresh_empty()