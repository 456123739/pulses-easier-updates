"""
developer_view.py — 开发者界面
------------------------------------------------
左工作区：拖入导出包 + 策略表 + 主按钮
右工作区：更新日志编辑器 + 导出选项 + 进度 + 日志

策略表支持文件夹 + 根目录散文件（如 options.txt）
"""

import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import filedialog

import customtkinter as ctk

from ..core import database as db
from ..core.eapack import export_eapack
from ..core.mrpack import (
    MRPack,
    locate_content_root,
    merge_files,
    parse_mrpack,
    top_level_folders,
)
from ..theme import Color, Font, Size
from ..utils.files import human_size
from .widgets.drop_zone import DropZone
from .widgets.export_options import ExportOptionsPanel
from .widgets.log_panel import LogPanel
from .widgets.markdown_editor import MarkdownEditor
from .widgets.progress_panel import ProgressPanel
from .widgets.strategy_table import StrategyTable


class DeveloperView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=Color.WORKSPACE_BG,
                         corner_radius=0, **kwargs)

        self.source_zip: Path | None = None
        self.tmp_dir: TemporaryDirectory | None = None
        self.extract_dir: Path | None = None
        # 解析期与导出期各用一个临时目录，互不覆盖
        self.parse_tmp_dir: TemporaryDirectory | None = None
        self.export_tmp_dir: TemporaryDirectory | None = None
        self.pack: MRPack | None = None
        self.merged: list[dict] = []
        # 导出时写进 manifest 的版本：优先用源包自己的 versionId
        # （旧实现硬编码 "1.0.0"，manifest 里的版本永远是假的）
        self.version = ""
        self._locked = False
        self._parsing = False

        self._build()

    # ------------------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=1,
                                  minsize=Size.DEVELOPER_RIGHT_MIN_W)
        self.grid_rowconfigure(0, weight=1)

        self._build_left()
        self._build_right()

    def _build_left(self):
        self.left = ctk.CTkFrame(self, fg_color=Color.WORKSPACE_BG,
                                 corner_radius=0)
        self.left.grid(row=0, column=0, sticky="nsew",
                       padx=(Size.PAD_WORKSPACE, Size.GAP // 2),
                       pady=Size.PAD_WORKSPACE)
        self.left.grid_columnconfigure(0, weight=1)
        self.left.grid_rowconfigure(1, weight=1)

        drop_wrap = ctk.CTkFrame(self.left, fg_color=Color.TRANSPARENT)
        drop_wrap.grid(row=0, column=0, sticky="ew")
        drop_wrap.grid_columnconfigure(0, weight=1)

        self.drop_pack = DropZone(
            drop_wrap,
            title="拖入启动器导出的整合包 ZIP",
            subtitle="支持 .mrpack / .zip",
            height=Size.DROP_H_WORK,
            mode="zip",
            on_drop=self._on_pack_dropped,
            highlight=True)
        self.drop_pack.grid(row=0, column=0, sticky="ew")

        self.clear_btn = ctk.CTkButton(
            drop_wrap, text="清除", width=80, height=28,
            font=Font.SMALL, corner_radius=Size.RADIUS_BUTTON,
            fg_color=Color.LOG_BG, hover_color=Color.BORDER,
            text_color=Color.TEXT_SECONDARY,
            command=self._on_clear_click)
        self.clear_btn.grid(row=1, column=0, sticky="e", pady=(6, 0))

        self.strategy_table = StrategyTable(
            self.left, title="更新策略",
            on_profile_imported=self._on_profile_imported)
        self.strategy_table.grid(row=1, column=0, sticky="nsew",
                                 pady=(Size.GAP, 0))

        self.main_btn = ctk.CTkButton(
            self.left, text="开始制作更新包", height=Size.MAIN_BTN_H,
            font=Font.BTN_LARGE, corner_radius=Size.RADIUS_DROP,
            fg_color=Color.ACCENT, hover_color=Color.ACCENT_HOVER,
            text_color=Color.TEXT_ON_ACCENT,
            command=self._on_build_click)
        self.main_btn.grid(row=2, column=0, sticky="ew",
                           pady=(Size.GAP, 0))

    def _build_right(self):
        self.right = ctk.CTkFrame(self, fg_color=Color.WORKSPACE_BG,
                                  corner_radius=0)
        self.right.grid(row=0, column=1, sticky="nsew",
                        padx=(Size.GAP // 2, Size.PAD_WORKSPACE),
                        pady=Size.PAD_WORKSPACE)
        self.right.grid_columnconfigure(0, weight=1)
        self.right.grid_rowconfigure(0, weight=1)

        self.editor = MarkdownEditor(self.right)
        self.editor.grid(row=0, column=0, sticky="nsew")

        self.export_options = ExportOptionsPanel(
            self.right, on_change=self._on_export_options_changed)
        self.export_options.grid(row=1, column=0, sticky="ew",
                                 pady=(Size.GAP, 0))

        self.progress_panel = ProgressPanel(self.right)
        self.progress_panel.grid(row=2, column=0, sticky="ew",
                                 pady=(Size.GAP, 0))
        self.progress_panel.grid_remove()

        self.log = LogPanel(self.right, title="日志",
                            on_clear=self._on_log_clear)
        self.log.grid(row=3, column=0, sticky="ew", pady=(Size.GAP, 0))

    # ------------------------------------------------------------------
    def _on_export_options_changed(self):
        """勾选即写库（旧实现只在点"导出"时才写，中途关掉就丢了）。"""
        try:
            if db.get_db_path() is None:
                return
            db.set_export_options(self.export_options.get_options())
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_pack_dropped(self, paths: list[Path]):
        if self._locked or self._parsing:
            self.log.log("warn", "正在处理上一个导出包，请稍候")
            return

        zip_candidates = [
            p for p in paths
            if p.suffix.lower() in (".zip", ".mrpack", ".eapack")
        ]
        if not zip_candidates:
            self.log.log("error", "请拖入 .zip / .mrpack / .eapack 文件")
            return

        # 先把旧包状态清干净：否则解析失败后点"导出"会用
        # 「新 ZIP 的 index + 旧包的策略/名称」产出自相矛盾的包
        self._clear_pack_state(keep_log=True)

        self.source_zip = zip_candidates[0]
        try:
            size = self.source_zip.stat().st_size
        except OSError:
            size = 0
        self.drop_pack.set_text(title=self.source_zip.name,
                                subtitle=human_size(size))
        # 导入成功：取消引导高亮
        try:
            self.drop_pack.set_highlight(False)
        except Exception:  # noqa: BLE001, S110
            pass

        self.log.clear()
        self.log.log("info", "解析导出包...")
        self._parsing = True
        self._set_locked(True)
        self.main_btn.configure(text="解析中...", state="disabled")

        zip_path = self.source_zip

        def worker():
            # 解析 + 解压都在工作线程里做：大包解压放到 Tk 主线程会冻结界面
            pack, err = parse_mrpack(zip_path)
            if err or pack is None:
                self.after(0, lambda e=err: self._on_parse_error(e))
                return
            extract_dir = None
            file_items: set[str] = set()
            tmp = None
            try:
                tmp = TemporaryDirectory(prefix="pulses_dev_parse_")
                extract_dir = Path(tmp.name)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)
                content_root, is_fallback = locate_content_root(extract_dir)
                if not is_fallback:
                    for e in content_root.iterdir():
                        if e.is_file():
                            file_items.add(e.name)
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda m=str(e):
                           self.log.log("warn", f"解压失败：{m}"))
                extract_dir = None
            self.after(0, lambda p=pack, t=tmp, d=extract_dir,
                       fi=file_items: self._on_parse_done(p, t, d, fi))

        threading.Thread(target=worker, daemon=True).start()

    def _on_parse_error(self, err: str):
        self.log.log("error", f"解析失败：{err}")
        self._parsing = False
        self._set_locked(False)
        self.source_zip = None
        self.pack = None
        self.merged = []
        self.extract_dir = None
        self.strategy_table.set_folders([])
        self.main_btn.configure(text="开始制作更新包", state="normal")
        try:
            self.drop_pack.set_highlight(True)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_parse_done(self, pack: MRPack,
                       parse_tmp: TemporaryDirectory | None = None,
                       extract_dir: Path | None = None,
                       file_items: set[str] | None = None):
        self._parsing = False
        self._set_locked(False)
        self.pack = pack
        self.merged = merge_files(pack)
        folders = top_level_folders(self.merged)

        self.parse_tmp_dir = parse_tmp
        self.extract_dir = extract_dir if extract_dir is not None else (
            Path(self.tmp_dir.name) if self.tmp_dir else None)
        if file_items is None:
            file_items = set()

        all_items = list(folders) + sorted(file_items)

        # 黑名单要**先**设置：strategy_table 建行时会读它
        blacklist = db.get_blacklist() if db.get_db_path() else []
        self.strategy_table.set_blacklist(blacklist)
        self.strategy_table.set_folders(all_items, file_items=file_items)

        self.export_options.set_options(db.get_export_options())
        self._log_pack_summary(pack, folders, file_items)

        self.main_btn.configure(text="开始制作更新包", state="normal")

    def _on_profile_imported(self, profile: dict):
        """导入配置后刷新导出选项面板（白名单已由策略表写库）。"""
        try:
            self.export_options.set_options(db.get_export_options())
            self.log.log("info", "已导入配置（策略 / 黑名单 / 白名单 / "
                                 "导出选项）")
        except Exception:  # noqa: BLE001, S110
            pass

    def _log_pack_summary(self, pack: MRPack, folders: list[str],
                          file_items: set[str]):
        self.log.log("info", "─── 导出包信息 ───")
        self.log.log("info", f"名称：{pack.name or '未命名'}")
        self.log.log("info", f"版本：{pack.version_id or '未知'}")
        self.log.log("info", f"依赖：{pack.dependencies or '无'}")

        counts: dict[str, int] = {}
        for item in self.merged:
            path = item.get("path", "")
            top = path.split("/", 1)[0] if "/" in path else "（根目录）"
            counts[top] = counts.get(top, 0) + 1

        if counts:
            self.log.log("info", "─── 文件统计 ───")
            for name in sorted(counts, key=lambda x: -counts[x]):
                self.log.log("info", f"{name}：{counts[name]} 个")
            self.log.log("info", f"合计：{len(self.merged)} 个文件")
        else:
            self.log.log("warn", "导出包中没有可更新的文件")

        self.log.log("info",
                     f"共 {len(folders)} 个文件夹，"
                     f"{len(file_items)} 个根文件参与策略")

    # ------------------------------------------------------------------
    def _clear_pack_state(self, keep_log: bool = False):
        """清空当前导出包状态（不动日志时可保留日志）。"""
        self.source_zip = None
        self.pack = None
        self.merged = []
        self.extract_dir = None
        for attr in ("parse_tmp_dir", "export_tmp_dir", "tmp_dir"):
            tmp = getattr(self, attr, None)
            if tmp is not None:
                try:
                    tmp.cleanup()
                except Exception:  # noqa: BLE001, S110
                    pass
                setattr(self, attr, None)

        try:
            self.strategy_table.set_folders([])
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.drop_pack.set_text(
                title="拖入启动器导出的整合包 ZIP",
                subtitle="支持 .mrpack / .zip")
            self.drop_pack.set_highlight(True)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.editor.set_markdown("")
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.progress_panel.grid_remove()
            self.progress_panel.reset("就绪")
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.main_btn.configure(text="开始制作更新包", state="normal")
        except Exception:  # noqa: BLE001, S110
            pass
        if not keep_log:
            try:
                self.log.clear()
            except Exception:  # noqa: BLE001, S110
                pass

    def _on_clear_click(self):
        if self._locked or self._parsing:
            return
        self._clear_pack_state()

        try:
            self.export_options.set_options(db.get_export_options())
        except Exception:  # noqa: BLE001, S110
            pass

        self.log.clear()
        self.log.log("info", "已清除当前导出包，回到初始状态")

    def _on_log_clear(self):
        self.log.clear()
        if self.pack is not None:
            self.log.log("info", f"导出包：{self.pack.name or '未命名'}")

    # ------------------------------------------------------------------
    def _on_build_click(self):
        pack = self.pack
        source_zip = self.source_zip

        if pack is None or source_zip is None:
            self.log.log("error", "尚未导入导出包")
            return

        if db.get_db_path() is None:
            self.log.log("warn", "尚未设置数据库，部分配置可能无法保存")

        base = source_zip.stem or "pack"
        default_name = f"{base}_update.eapack"

        out = filedialog.asksaveasfilename(
            title="保存更新包",
            defaultextension=".eapack",
            filetypes=[("Pulses Easier 更新包", "*.eapack"),
                       ("所有文件", "*.*")],
            initialfile=default_name)
        if not out:
            return

        out_path = Path(out)
        self._set_locked(True)
        self.main_btn.configure(text="制作中...", state="disabled")
        self.progress_panel.reset("准备中...")
        self.progress_panel.grid()

        strategies = self.strategy_table.get_strategies()
        changelog = self.editor.get_markdown()
        opts = self.export_options.get_options()
        disabled_items = self.strategy_table.get_blacklist()

        if db.get_db_path() is not None:
            try:
                db.set_export_options(opts)
                db.set_blacklist(disabled_items)
            except Exception:  # noqa: BLE001, S110
                pass

        def worker():
            ok = self._do_export(out_path, source_zip, pack,
                                 strategies, changelog,
                                 disabled_items)
            self.after(0, lambda: self._on_build_done(ok, out_path))

        threading.Thread(target=worker, daemon=True).start()

    def _do_export(self, out_path: Path, source_zip: Path, pack: MRPack,
                   strategies: dict, changelog: str,
                   excluded_folders: list[str]) -> Path | None:
        try:
            self._cleanup_export_tmp()
            self.export_tmp_dir = TemporaryDirectory(prefix="pulses_dev_")
            self.extract_dir = Path(self.export_tmp_dir.name)
            with zipfile.ZipFile(source_zip, "r") as zf:
                zf.extractall(self.extract_dir)

            def _log(level: str, msg: str) -> None:
                self.after(0, self.log.log, level, msg)

            def _prog(done: int, total: int, label: str) -> None:
                self.after(0, self.progress_panel.update_progress,
                           done, total, label)

            return export_eapack(
                source_dir=self.extract_dir,
                source_zip=source_zip,
                out_path=out_path,
                pack=pack,
                merged_files=self.merged,
                strategies=strategies,
                changelog_md=changelog,
                version=self.version,
                excluded_folders=excluded_folders,
                log=_log,
                progress=_prog)
        except Exception as e:  # noqa: BLE001
            self.after(0, self.log.log, "error", f"导出失败：{e}")
            self.after(0, self._cleanup_export_tmp)
            return None

    def _cleanup_export_tmp(self):
        tmp = self.export_tmp_dir
        self.export_tmp_dir = None
        self.tmp_dir = None
        if tmp is not None:
            try:
                tmp.cleanup()
            except Exception:  # noqa: BLE001, S110
                pass

    def _on_build_done(self, result, out_path: Path):
        self._set_locked(False)
        self._cleanup_export_tmp()
        self.main_btn.configure(text="开始制作更新包", state="normal")

        if result is not None:
            self.progress_panel.update_progress(1, 1, "完成 ✓")
            self.log.log("info", "更新包制作完成 ✓")
            self._open_folder(out_path)
        else:
            self.progress_panel.update_progress(0, 1, "失败")
            self.log.log("error", "更新包制作失败")

    @staticmethod
    def _open_folder(path: Path):
        try:
            folder = path.parent
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _set_locked(self, locked: bool):
        self._locked = locked
        state = "disabled" if locked else "normal"
        try:
            self.clear_btn.configure(state=state)
        except Exception:  # noqa: BLE001, S110
            pass