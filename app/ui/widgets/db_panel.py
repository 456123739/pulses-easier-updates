"""
db_panel.py — 「数据库位置」设置页（切换 / 迁移）
------------------------------------------------
玩家反馈：数据库位置只在首次运行向导里选过一次，之后没法改。
这个面板补上：

    · 看当前数据库在哪、多大、有多少个项目
    · 迁移到新位置（复制 + 校验 + 切换，**原目录保留**，不删）
    · 切换到已有数据库
    · 在新位置新建空数据库

迁移在后台线程跑，进度条实时刷新；失败时**不会**切过去，
界面里明确写出原因（磁盘空间不足 / 权限 / 路径不合法…）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ...core import database as db
from ...core import dbmigrate as mig
from ...theme import Color, Font, Size
from .dialog import alert, confirm

_PAD = 16


def _open_folder(path: Path) -> None:
    """在系统文件管理器里打开目录。"""
    p = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)                                  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:  # noqa: BLE001, S110
        pass


class DatabaseDialog(ctk.CTkToplevel):
    """数据库位置设置（非模态，可边看边操作）。"""

    def __init__(self, parent, on_done: Callable[[], None] | None = None):
        super().__init__(parent)
        self._on_done = on_done
        self._busy = False

        self.title("数据库位置")
        self.geometry("560x420")
        self.configure(fg_color=Color.WINDOW_BG)
        self.resizable(False, False)
        try:
            self.transient(parent)
            self.update_idletasks()
            px = parent.winfo_rootx() + (parent.winfo_width() - 560) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - 420) // 2
            self.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            from .. import motion
            motion.slide_in_dialog(self, self.winfo_x(), self.winfo_y())
        except Exception:  # noqa: BLE001, S110
            pass

        wrap = ctk.CTkFrame(self, fg_color=Color.TRANSPARENT)
        wrap.pack(fill="both", expand=True, padx=_PAD, pady=_PAD)
        wrap.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(wrap, text="数据库位置",
                     font=Font.TITLE, text_color=Color.TEXT_PRIMARY,
                     fg_color=Color.TRANSPARENT, anchor="w")\
            .grid(row=0, column=0, sticky="w")

        self.path_label = ctk.CTkLabel(
            wrap, text="", font=Font.SMALL, text_color=Color.ACCENT,
            fg_color=Color.TRANSPARENT, anchor="w", justify="left",
            wraplength=500)
        self.path_label.grid(row=1, column=0, sticky="w", pady=(6, 2))

        self.stat_label = ctk.CTkLabel(
            wrap, text="", font=Font.TINY, text_color=Color.TEXT_MUTED,
            fg_color=Color.TRANSPARENT, anchor="w", justify="left")
        self.stat_label.grid(row=2, column=0, sticky="w")

        self.progress = ctk.CTkProgressBar(
            wrap, height=6, corner_radius=3,
            fg_color=Color.BORDER, progress_color=Color.ACCENT)
        self.progress.set(0)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(10, 4))
        self.progress.grid_remove()

        self.status = ctk.CTkLabel(
            wrap, text="", font=Font.SMALL, text_color=Color.TEXT_SECONDARY,
            fg_color=Color.TRANSPARENT, anchor="w", justify="left",
            wraplength=500)
        self.status.grid(row=4, column=0, sticky="w")

        btns = ctk.CTkFrame(wrap, fg_color=Color.TRANSPARENT)
        btns.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        btns.grid_columnconfigure((0, 1), weight=1)
        self._btns = []
        specs = [
            ("迁移到新位置…", self._on_migrate, True),
            ("切换到已有数据库…", self._on_switch, False),
            ("在新位置新建空数据库…", self._on_create, False),
            ("打开当前目录", self._on_open, False),
        ]
        for i, (text, cmd, primary) in enumerate(specs):
            b = ctk.CTkButton(
                btns, text=text, height=34, font=Font.SMALL,
                corner_radius=Size.RADIUS_BUTTON,
                fg_color=Color.ACCENT if primary else Color.LOG_BG,
                hover_color=(Color.ACCENT_HOVER if primary else Color.BORDER),
                text_color=(Color.TEXT_ON_ACCENT if primary
                            else Color.TEXT_PRIMARY),
                command=cmd)
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=3, pady=3)
            self._btns.append(b)

        self._refresh()

    # ------------------------------------------------------------------
    def _refresh(self):
        root = db.get_db_path()
        if root is None:
            self.path_label.configure(text="（尚未设置数据库）")
            self.stat_label.configure(text="")
            return
        info = mig.describe(root)
        self.path_label.configure(text=str(root))
        extra = ""
        if info["cache_bytes"]:
            extra = f"，其中缓存 {mig.human_size(info['cache_bytes'])}"
        self.stat_label.configure(
            text=f"{info['files']} 个文件 / {mig.human_size(info['bytes'])}"
                 f"{extra}　·　{info['projects']} 个项目")
        if mig.has_incomplete_marker(root):
            self._say("注意：这个库里还有上次没做完的迁移标记",
                      Color.WARNING)

    def _say(self, text: str, color: str | None = None):
        try:
            self.status.configure(text=text,
                                  text_color=color or Color.TEXT_SECONDARY)
        except Exception:  # noqa: BLE001, S110
            pass

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for b in self._btns:
            try:
                b.configure(state=state)
            except Exception:  # noqa: BLE001, S110
                pass
        try:
            if busy:
                self.progress.set(0)
                self.progress.grid()
            else:
                self.progress.grid_remove()
        except Exception:  # noqa: BLE001, S110
            pass
        if text:
            self._say(text)

    def _pick_folder(self, title: str) -> Path | None:
        try:
            from tkinter import filedialog
            chosen = filedialog.askdirectory(title=title, parent=self)
        except Exception:  # noqa: BLE001
            chosen = None
        return Path(chosen) if chosen else None

    # ------------------------------------------------------------------
    def _on_open(self):
        root = db.get_db_path()
        if root is not None:
            _open_folder(Path(root))

    def _on_migrate(self):
        if self._busy:
            return
        src = db.get_db_path()
        if src is None:
            self._say("还没有数据库，先用「新建空数据库」", Color.WARNING)
            return
        dst = self._pick_folder("选择新的数据库位置（会在这里创建数据库）")
        if dst is None:
            return
        dst = Path(dst) / "pulses_easier_db"
        pre = mig.plan(src, dst)
        if pre["target_is_same"]:
            self._say("选的是当前目录", Color.WARNING)
            return
        lines = [
            f"从：{src}",
            f"到：{dst}",
            "",
            f"将复制 {pre['files']} 个文件，约 {mig.human_size(pre['bytes'])}。",
        ]
        if pre["skipped_dirs"]:
            lines.append(f"（跳过 {'、'.join(pre['skipped_dirs'])} 目录："
                         f"里面是下载缓存，新位置会自动重建）")
        if pre["target_valid"]:
            lines.append("目标位置已有一个数据库：同名文件会被覆盖，"
                         "它独有的文件保留。")
        elif pre["target_exists"]:
            lines.append("目标位置已有内容，会被合并进去。")
        lines += ["", "原目录不会被删除，确认没问题后你可以自己清理。"]

        def _go(ok: bool):
            if ok:
                self._run_migrate(src, dst)

        confirm(self, "迁移数据库", "\n".join(lines),
                confirm_text="开始迁移", cancel_text="取消",
                on_result=_go)

    def _run_migrate(self, src: Path, dst: Path):
        self._set_busy(True, "正在迁移…（原数据不动，随时可以取消）")
        cancel = {"flag": False}

        def _progress(done: int, total: int, moved: int, total_bytes: int):
            def _ui():
                try:
                    self.progress.set(done / total if total else 1.0)
                    self._say(f"已复制 {done}/{total} 个文件　"
                              f"{mig.human_size(moved)} / "
                              f"{mig.human_size(total_bytes)}")
                except Exception:  # noqa: BLE001, S110
                    pass
            try:
                self.after(0, _ui)
            except Exception:  # noqa: BLE001, S110
                pass

        def _worker():
            res = mig.migrate(src, dst, progress=_progress,
                              should_abort=lambda: cancel["flag"])
            try:
                self.after(0, lambda: self._after_migrate(src, dst, res))
            except Exception:  # noqa: BLE001, S110
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _after_migrate(self, src: Path, dst: Path, res: dict):
        self._set_busy(False)
        if not res["ok"]:
            self._say(f"迁移未完成：{res['msg']}", Color.ERROR)
            alert(self, "迁移未完成",
                  f"{res['msg']}\n\n已复制 {res['copied']} 个文件。\n"
                  f"程序仍在使用原来的数据库，没有任何东西被删除。\n"
                  f"处理好问题后可以再点一次「迁移到新位置」。",
                  level="error")
            return
        self._say(f"{res['msg']}　正在切换…", Color.ACCENT)

        def _switch(ok: bool):
            if not ok:
                self._say("迁移完成，但你没有切换过去（数据已在新位置）")
                return
            had, msg = mig.switch_to(dst)
            if had:
                self._say(msg + "　（原数据库仍保留在原处）", Color.ACCENT)
                try:
                    if self._on_done is not None:
                        self._on_done()
                except Exception:  # noqa: BLE001, S110
                    pass
                alert(self, "已切换数据库",
                      f"{msg}\n\n原数据库 {src}\n仍然完好保留，"
                      f"确认新位置一切正常后可以自行删除。\n\n"
                      f"部分界面数据建议重启软件一次以完全刷新。",
                      level="info")
            else:
                self._say(msg, Color.ERROR)
            self._refresh()

        confirm(self, "迁移完成", f"{res['msg']}\n\n现在切换到这个数据库吗？",
                confirm_text="切换", cancel_text="先不切", on_result=_switch)

    # ------------------------------------------------------------------
    def _on_switch(self):
        if self._busy:
            return
        dst = self._pick_folder("选择已有的数据库目录")
        if dst is None:
            return
        if not db.is_valid_database(dst):
            self._say("这不是一个有效的数据库（缺 config.json 或 projects/）",
                      Color.WARNING)
            return
        info = mig.describe(dst)
        confirm(self, "切换数据库",
                f"切换到：\n{dst}\n\n"
                f"{info['files']} 个文件 / {mig.human_size(info['bytes'])}"
                f"　·　{info['projects']} 个项目\n\n"
                f"当前数据库不会被删除。",
                confirm_text="切换", cancel_text="取消",
                on_result=lambda ok: self._do_switch(dst) if ok else None)

    def _do_switch(self, dst: Path):
        ok, msg = mig.switch_to(dst)
        self._say(msg, Color.ACCENT if ok else Color.ERROR)
        if ok:
            try:
                if self._on_done is not None:
                    self._on_done()
            except Exception:  # noqa: BLE001, S110
                pass
        self._refresh()

    def _on_create(self):
        if self._busy:
            return
        base = self._pick_folder("选择新数据库的上级目录")
        if base is None:
            return
        dst = Path(base) / "pulses_easier_db"

        def _go(ok: bool):
            if not ok:
                return
            made, msg = mig.create_at(dst)
            if not made:
                self._say(msg, Color.ERROR)
                return
            done, smsg = mig.switch_to(dst)
            self._say(smsg, Color.ACCENT if done else Color.ERROR)
            self._refresh()
            if done:
                try:
                    if self._on_done is not None:
                        self._on_done()
                except Exception:  # noqa: BLE001, S110
                    pass

        confirm(self, "新建数据库",
                f"会在下面这个位置新建一个空数据库并切换过去：\n{dst}\n\n"
                f"当前数据库不会被删除，也不会迁移任何数据。",
                confirm_text="新建并切换", cancel_text="取消", on_result=_go)


def open_db_settings(parent, on_done: Callable[[], None] | None = None):
    return DatabaseDialog(parent, on_done=on_done)
