"""
whitelist_panel.py — 白名单配置面板
------------------------------------------------
折叠式表单：
  - 标题行："文件级校验白名单" + 三角折叠按钮 + "＋"新增按钮
  - 展开后列出条目：路径文字 + ×删除
  - 拖入策略表条目到面板 → 自动添加该文件夹名
  - 双击条目 → 进入编辑
  - 失焦/回车 → 校验后保存；校验失败弹窗提示
  - × 删除

填写规则：
  - 相对整合包根目录，例如 mods
  - 子文件夹用 / 分隔，例如 config/foo
  - 反斜杠自动转正斜杠
  - 不允许空、不允许 ..、不允许重复

数据保存到数据库 config.json 的 whitelist 字段。
"""

import re
from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ...core import database as db
from ...theme import Color, Font, Size

try:
    from tkinterdnd2 import DND_FILES
    _DND_READY = True
except Exception:
    DND_FILES = None
    _DND_READY = False


class WhitelistPanel(ctk.CTkFrame):
    def __init__(self, master,
                 on_change: Callable[[], None] | None = None,
                 **kwargs):
        super().__init__(master, fg_color=Color.CARD_BG,
                         corner_radius=Size.RADIUS_CARD, **kwargs)

        self.on_change = on_change
        self._items: list[str] = []
        self._collapsed = True
        self._rows: list[ctk.CTkFrame] = []
        self._registered = False
        self._editing_path: str | None = None

        self.grid_columnconfigure(0, weight=1)

        # 标题行
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

        ctk.CTkLabel(header, text="文件级校验白名单",
                     font=Font.BODY_B, text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=1, sticky="w", padx=(4, 0))

        self.add_btn = ctk.CTkButton(
            header, text="＋", width=24, height=24,
            font=Font.BODY, corner_radius=6,
            fg_color=Color.TRANSPARENT, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._on_add)
        self.add_btn.grid(row=0, column=2, sticky="e")

        # 规则提示（折叠时不显示）
        self.hint = ctk.CTkLabel(
            self,
            text="规则：相对整合包根；子文件夹用 / 分隔（如 config/foo）；\n"
                 "拖入策略条目或输入路径；双击编辑，× 删除",
            font=Font.TINY, text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT, anchor="w", justify="left",
            wraplength=200)
        self.hint.grid(row=1, column=0, sticky="ew",
                       padx=12, pady=(0, 6))
        self.hint.grid_remove()

        # 列表容器
        self.list_frame = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        self.list_frame.grid(row=2, column=0, sticky="ew",
                             padx=8, pady=(0, 8))
        self.list_frame.grid_columnconfigure(0, weight=1)
        self.list_frame.grid_remove()

        # 加载已有
        self.reload()

        # 延迟注册拖拽
        self.after(80, self._register_dnd)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def reload(self):
        self._items = db.get_whitelist()
        self._rebuild_rows()

    def _save(self):
        db.set_whitelist(self._items)
        if self.on_change:
            self.on_change()

    @staticmethod
    def _normalize(s: str) -> str:
        return str(s).strip().replace("\\", "/").strip("/")

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def _validate(self, raw: str, old: str | None = None) -> tuple[bool, str, str]:
        """
        校验一条路径。
        返回 (ok, normalized, error_message)。
        old 传入时，允许与 old 相同（编辑时视为无变化）。
        """
        s = self._normalize(raw)
        if not s:
            return False, "", "路径不能为空。"

        # 非法字符
        if ".." in s.split("/"):
            return False, "", "路径不能包含 “..”。"
        if re.search(r'[<>:"|?*]', s):
            return False, "", "路径不能包含 < > : \" | ? * 等字符。"
        if s.startswith("/"):
            return False, "", "请使用相对路径，不要以 / 开头。"

        # 重复检测
        if old is None:
            if s in self._items:
                return False, "", f"条目“{s}”已存在，不能重复添加。"
        else:
            # 编辑：允许与自己相同
            if s != old and s in self._items:
                return False, "", f"条目“{s}”已存在，不能重复。"

        return True, s, ""

    # ------------------------------------------------------------------
    # 增删改
    # ------------------------------------------------------------------
    def add_item(self, path: str, silent: bool = False) -> bool:
        ok, norm, err = self._validate(path)
        if not ok:
            if not silent:
                self._toast("无法添加", err)
            return False
        self._items.append(norm)
        self._rebuild_rows()
        self._save()
        return True

    def remove_item(self, path: str):
        if path in self._items:
            self._items.remove(path)
            self._rebuild_rows()
            self._save()

    def update_item(self, old: str, new: str) -> bool:
        ok, norm, err = self._validate(new, old=old)
        if not ok:
            self._toast("无法保存", err)
            return False
        if norm == old:
            # 无变化，直接刷新即可
            self._rebuild_rows()
            return True
        try:
            idx = self._items.index(old)
            self._items[idx] = norm
        except ValueError:
            self._rebuild_rows()
            return False
        self._rebuild_rows()
        self._save()
        return True

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _toggle(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.arrow.configure(text="▶")
            self.hint.grid_remove()
            self.list_frame.grid_remove()
        else:
            self.arrow.configure(text="▼")
            self.hint.grid()
            self.list_frame.grid()

    def _rebuild_rows(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._rows.clear()
        self._editing_path = None

        if not self._items:
            ctk.CTkLabel(self.list_frame, text="暂无条目",
                         font=Font.TINY, text_color=Color.TEXT_MUTED,
                         fg_color=Color.TRANSPARENT)\
                .grid(row=0, column=0, sticky="w", padx=4, pady=4)
            return

        for i, path in enumerate(self._items):
            self._build_row(i, path)

    def _build_row(self, index: int, path: str):
        row = ctk.CTkFrame(self.list_frame, fg_color=Color.TRANSPARENT)
        row.grid(row=index, column=0, sticky="ew", pady=1)
        row.grid_columnconfigure(0, weight=1)
        self._rows.append(row)

        label = ctk.CTkLabel(
            row, text=path, font=Font.SMALL,
            text_color=Color.TEXT_PRIMARY,
            fg_color=Color.TRANSPARENT, anchor="w")
        label.grid(row=0, column=0, sticky="ew", padx=(6, 4), pady=2)
        label.bind("<Double-Button-1>",
                   lambda e, r=row, p=path: self._start_edit(r, p))

        del_btn = ctk.CTkButton(
            row, text="×", width=22, height=22,
            font=(Font.FAMILY, 14), corner_radius=4,
            fg_color=Color.TRANSPARENT, hover_color=Color.ERROR,
            text_color=Color.TEXT_MUTED,
            command=lambda p=path: self.remove_item(p))
        del_btn.grid(row=0, column=1, sticky="e", padx=(0, 4))

    # ------------------------------------------------------------------
    # 编辑
    # ------------------------------------------------------------------
    def _start_edit(self, row: ctk.CTkFrame, old_path: str):
        """把 label 换成 Entry，回车/失焦提交，Escape 取消"""
        if self._editing_path is not None:
            return
        self._editing_path = old_path

        for w in row.winfo_children():
            w.destroy()

        var = ctk.StringVar(value=old_path)
        entry = ctk.CTkEntry(
            row, textvariable=var, font=Font.SMALL,
            fg_color=Color.LOG_BG, border_width=0,
            text_color=Color.TEXT_PRIMARY, height=24,
            placeholder_text="如 config/foo")
        entry.grid(row=0, column=0, sticky="ew", padx=(6, 4), pady=2)
        entry.focus_set()
        entry.select_range(0, "end")

        # 防止 Enter 与 FocusOut 双触发
        committed = {"done": False}

        def commit(_e=None):
            if committed["done"]:
                return
            committed["done"] = True
            ok = self.update_item(old_path, var.get())
            if not ok:
                # 校验失败：重新把编辑框还原，并把焦点抢回
                self._editing_path = None
                try:
                    entry.focus_set()
                except Exception:  # noqa: BLE001, S110
                    pass

        def cancel(_e=None):
            if committed["done"]:
                return
            committed["done"] = True
            self._editing_path = None
            self._rebuild_rows()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", cancel)

    # ------------------------------------------------------------------
    # 新增
    # ------------------------------------------------------------------
    def _on_add(self):
        if self._collapsed:
            self._toggle()

        dialog = ctk.CTkToplevel(self)
        dialog.title("新增白名单")
        dialog.geometry("400x200")
        dialog.configure(fg_color=Color.WINDOW_BG)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="输入路径（相对整合包根）\n"
                 "子文件夹用 / 分隔，例如 config/foo",
            font=Font.SMALL, text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT, justify="left", anchor="w")\
            .pack(anchor="w", padx=16, pady=(16, 4))

        var = ctk.StringVar()
        entry = ctk.CTkEntry(
            dialog, textvariable=var, font=Font.BODY,
            fg_color=Color.LOG_BG, border_width=0,
            text_color=Color.TEXT_PRIMARY, height=32,
            placeholder_text="例如 mods 或 config/foo")
        entry.pack(fill="x", padx=16, pady=(0, 8))
        entry.focus_set()

        err_label = ctk.CTkLabel(
            dialog, text="", font=Font.TINY,
            text_color=Color.ERROR,
            fg_color=Color.TRANSPARENT, anchor="w")
        err_label.pack(anchor="w", padx=16, pady=(0, 8))

        def commit():
            ok, norm, err = self._validate(var.get())
            if not ok:
                err_label.configure(text=err)
                return
            self._items.append(norm)
            self._rebuild_rows()
            self._save()
            dialog.destroy()

        ctk.CTkButton(
            dialog, text="添加", height=32,
            font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
            text_color=Color.TEXT_ON_ACCENT,
            command=commit)\
            .pack(fill="x", padx=16, pady=(0, 16))

        entry.bind("<Return>", lambda e: commit())

    # ------------------------------------------------------------------
    # 错误提示弹窗
    # ------------------------------------------------------------------
    def _toast(self, title: str, message: str):
        """轻量错误提示弹窗"""
        try:
            dialog = ctk.CTkToplevel(self)
            dialog.title(title)
            dialog.geometry("360x160")
            dialog.configure(fg_color=Color.WINDOW_BG)
            dialog.transient(self.winfo_toplevel())
            dialog.grab_set()

            ctk.CTkLabel(dialog, text=title, font=Font.BODY_B,
                         text_color=Color.ERROR,
                         fg_color=Color.TRANSPARENT, anchor="w")\
                .pack(anchor="w", padx=16, pady=(16, 4))

            ctk.CTkLabel(dialog, text=message, font=Font.SMALL,
                         text_color=Color.TEXT_PRIMARY,
                         fg_color=Color.TRANSPARENT, anchor="w",
                         justify="left", wraplength=320)\
                .pack(anchor="w", padx=16, pady=(0, 12))

            ctk.CTkButton(
                dialog, text="知道了", height=32,
                font=Font.BUTTON, corner_radius=Size.RADIUS_BUTTON,
                fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
                text_color=Color.TEXT_ON_ACCENT,
                command=dialog.destroy)\
                .pack(fill="x", padx=16, pady=(0, 16))
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    # 拖拽
    # ------------------------------------------------------------------
    def _register_dnd(self):
        if not _DND_READY or self._registered:
            return
        try:
            targets = (self, self.hint, self.list_frame, self.arrow)
            for w in targets:
                register = getattr(w, "drop_target_register", None)
                bind = getattr(w, "dnd_bind", None)
                if register is None or bind is None:
                    continue
                register(DND_FILES)
                bind("<<Drop>>", self._handle_drop)
                bind("<<DropEnter>>", lambda e: self._hover(True))
                bind("<<DropLeave>>", lambda e: self._hover(False))
            self._registered = True
        except Exception:  # noqa: BLE001
            self._registered = False

    def _handle_drop(self, event):
        try:
            raw = getattr(event, "data", "")
            text = str(raw)
        except Exception:  # noqa: BLE001
            return

        added = False
        skipped: list[str] = []
        for c in self._parse_drop_text(text):
            ok, _norm, _err = self._validate(c)
            if ok:
                if self.add_item(c, silent=True):
                    added = True
            else:
                skipped.append(c)

        if added and self._collapsed:
            self._toggle()

        if skipped:
            self._toast("部分条目未添加",
                        "以下条目不符合规则或已存在：\n" +
                        "\n".join(skipped))

    @staticmethod
    def _parse_drop_text(data: str) -> list[str]:
        if not data:
            return []
        items = re.findall(r"\{([^}]*)\}|(\S+)", data)
        result: list[str] = []
        for braced, plain in items:
            raw = (braced or plain).strip()
            if not raw:
                continue
            p = Path(raw)
            if p.exists():
                if p.is_dir():
                    result.append(p.name)
            else:
                result.append(raw)
        return result

    def _hover(self, entering: bool):
        try:
            self.configure(
                fg_color=Color.CARD_BG if not entering else Color.BORDER)
        except Exception:  # noqa: BLE001, S110
            pass