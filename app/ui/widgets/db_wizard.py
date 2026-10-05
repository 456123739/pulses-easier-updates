"""
db_wizard.py — 数据库向导
------------------------------------------------
首次启动或数据库失效时弹出：
  - 新建数据库（选一个空文件夹作为 db_root）
  - 定位已有数据库（选已有 db_root）
  - 跳过（后续可在开发者端再配置）

UI 为 CTkToplevel 自定义弹窗，不使用系统对话框做选择面板。
"""

from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ...core import database as db
from ...theme import Color, Font, Size


class DatabaseWizard(ctk.CTkToplevel):
    def __init__(self, parent,
                 on_done: Callable[[bool], None] | None = None):
        super().__init__(parent)

        self.on_done = on_done

        self.title("Pulses Easier — 数据库设置")
        self.geometry("560x320")
        self.configure(fg_color=Color.WINDOW_BG)
        self.transient(parent)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 标题栏
        header = ctk.CTkFrame(self, fg_color=Color.SIDEBAR_BG,
                              corner_radius=0, height=48)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        ctk.CTkLabel(header, text="数据库设置",
                     font=Font.TITLE, text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT)\
            .pack(side="left", padx=16)

        # 内容区
        body = ctk.CTkFrame(self, fg_color=Color.CARD_BG,
                            corner_radius=Size.RADIUS_CARD)
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=16)
        body.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            body,
            text="Pulses Easier 需要一份数据库来保存配置、项目与缓存。\n"
                 "你可以新建一份数据库，或定位已有的数据库文件夹。",
            font=Font.BODY, text_color=Color.TEXT_PRIMARY,
            fg_color=Color.TRANSPARENT, justify="left", anchor="w")\
            .grid(row=0, column=0, sticky="w",
                  padx=20, pady=(20, 16))

        # 当前路径显示
        self.path_var = ctk.StringVar(value="尚未选择")
        self.path_label = ctk.CTkLabel(
            body, textvariable=self.path_var,
            font=Font.SMALL, text_color=Color.TEXT_SECONDARY,
            fg_color=Color.LOG_BG, corner_radius=8,
            anchor="w", wraplength=460)
        self.path_label.grid(row=1, column=0, sticky="ew",
                             padx=20, pady=(0, 16), ipady=8)

        # 按钮组
        btn_bar = ctk.CTkFrame(body, fg_color=Color.TRANSPARENT)
        btn_bar.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 20))

        ctk.CTkButton(
            btn_bar, text="新建数据库", width=120, height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
            text_color=Color.TEXT_ON_ACCENT,
            command=self._on_create)\
            .pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_bar, text="定位已有", width=120, height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_PRIMARY,
            command=self._on_locate)\
            .pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_bar, text="跳过", width=80, height=36,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.TRANSPARENT, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._on_skip)\
            .pack(side="right")

    # ------------------------------------------------------------------
    def _on_create(self):
        folder = filedialog.askdirectory(title="选择一个空文件夹作为数据库位置")
        if not folder:
            return
        root = Path(folder) / "pulses_easier_db"
        if root.exists():
            # 若已存在，直接当已有库处理
            if db.is_valid_database(root):
                db.set_db_path(root)
                self.path_var.set(str(root))
                self._finish(True)
                return
        if db.create_database(root):
            db.set_db_path(root)
            self.path_var.set(str(root))
            self._finish(True)
        else:
            self.path_var.set("创建失败，请换一个路径重试")

    def _on_locate(self):
        folder = filedialog.askdirectory(title="选择数据库文件夹")
        if not folder:
            return
        root = Path(folder)
        if db.is_valid_database(root):
            db.set_db_path(root)
            self.path_var.set(str(root))
            self._finish(True)
        else:
            self.path_var.set("该文件夹不是有效数据库（缺 config.json 或 projects/）")

    def _on_skip(self):
        self._finish(False)

    def _finish(self, ok: bool):
        if self.on_done:
            self.on_done(ok)
        try:
            self.after(120, self.destroy)
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
# 辅助：确保有数据库，否则弹向导
# ----------------------------------------------------------------------
def ensure_database(parent) -> bool:
    """
    检查数据库是否就绪。
      - 已就绪 → 返回 True
      - 未就绪 → 弹向导，阻塞等待用户选择，返回结果
    """
    if db.get_db_path() is not None:
        return True

    result = {"ok": False}
    wizard = DatabaseWizard(parent, on_done=lambda ok: result.update(ok=ok))
    parent.wait_window(wizard)
    return result["ok"]