"""
main_window.py — 主窗口
------------------------------------------------
- 三栏布局：侧边栏 + 工作区（玩家/开发者切换）
- 双端预热在屏幕外完成，切换零卡顿
- 启动流程：屏幕外预热 → withdraw → splash → on_boot_done()
"""

from pathlib import Path
from typing import ClassVar

import customtkinter as ctk

from ..core import database as db
from ..core import resume as resume_mod
from ..core.state import AppState
from ..theme import Anim, Color, Size
from ..utils.animation import slide_pages, snapshot_slide
from .transition import clear_overlays, logo_transition
from . import motion, win11
from .developer_view import DeveloperView
from .player_view import PlayerView
from .sidebar import Sidebar
from .widgets.db_wizard import ensure_database
from .widgets.dialog import LocateDialog, alert, confirm

# 屏幕外预热坐标（足够远离任何显示器）
_OFFSCREEN_POS = "+20000+20000"


class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Win11：先把 CTk 的 DPI 轮询里那段"alpha 0.15 → 改缩放 → 1"拆掉，
        # 否则窗口会莫名其妙变成半透明（详见 win11.py 顶部注释）
        win11.neutralize_ctk_alpha_flash()

        self.title("Pulses Easier")
        self._set_window_icon()
        self.geometry(f"{Size.WIN_W}x{Size.WIN_H}")
        self.minsize(Size.WIN_MIN_W, Size.WIN_MIN_H)
        self.configure(fg_color=Color.WINDOW_BG)

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=Size.PLAYER_L_RATIO)
        self.grid_columnconfigure(2, weight=Size.PLAYER_R_RATIO)
        self.grid_rowconfigure(0, weight=1)

        self._current = "玩家"
        # 关闭守卫（可叠加）：owner → 处理函数
        #   player    → 下载/应用进行中：强警告，允许强制关闭
        #   developer → 正在制作更新包：**不允许关闭**
        self._close_guards: dict = {}
        self._closing = False
        self._switching = False
        self._pending_identity = ""
        self._switch_guard = None
        self._switch_finishing = False
        self._boot_done = False
        self._warmed_up = False

        self.app_state = AppState()

        self._build_sidebar()
        self._build_page_host()

        # 默认只放玩家端
        self.player_view.place(in_=self.page_host, relx=0, rely=0,
                               relwidth=1.0, relheight=1.0)

        # Win11 不透明守卫：DWM 会在失焦 / 弹窗 / 映射时重新评估材质，
        # 只设一次不够；动画进行中的窗口会跳过（不抢 alpha）
        self._opacity_guard = win11.OpacityGuard(
            self, is_animating=motion.is_animating).install()

    # ------------------------------------------------------------------
    def _set_window_icon(self):
        """
        标题栏 / 任务栏图标。

        Windows 优先用 `app.ico`（多尺寸，任务栏和资源管理器都清楚），
        其它平台退到 `app_icon.png`；两条路都失败就保持 Tk 默认图标。
        """
        import sys as _sys

        from ..paths import asset as _asset

        if _sys.platform.startswith("win"):
            try:
                ico = _asset("app.ico")
                if ico.is_file():
                    self.iconbitmap(default=str(ico))
            except Exception:  # noqa: BLE001, S110
                pass
        try:
            png = _asset("app_icon.png")
            if png.is_file():
                from tkinter import PhotoImage
                self._icon_photo = PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_photo)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _build_sidebar(self):
        self.sidebar = Sidebar(
            self,
            app_state=self.app_state,
            on_identity_changed=self._on_identity_changed)
        self.sidebar.grid(row=0, column=0, sticky="nsw")

    def _build_page_host(self):
        self.page_host = ctk.CTkFrame(self, fg_color=Color.WORKSPACE_BG,
                                      corner_radius=0)
        self.page_host.grid(row=0, column=1, columnspan=2, sticky="nsew")

        self.player_view = PlayerView(self.page_host, self.sidebar,
                                      self.app_state)
        self.developer_view = DeveloperView(self.page_host)

    # ------------------------------------------------------------------
    # 屏幕外双端预热
    # ------------------------------------------------------------------
    def prepare_offscreen_warmup(self):
        """
        在屏幕外完成玩家端 + 开发者端的首次布局：
          1. 窗口移到屏幕外坐标并映射
          2. 依次 place 两端视图，update_idletasks()，让所有子控件完成布局
          3. 移回屏幕中心（但还没 deiconify，用户看不到）
        """
        if self._warmed_up:
            return
        self._warmed_up = True

        # 1) 挪到屏幕外并映射一次，让 Tk 给窗口真实尺寸
        try:
            self.geometry(f"{Size.WIN_W}x{Size.WIN_H}{_OFFSCREEN_POS}")
            self.update_idletasks()
        except Exception:  # noqa: BLE001, S110
            pass

        # 2) 预热玩家端（已在默认位置，补一次）
        try:
            self.player_view.place(in_=self.page_host, relx=0, rely=0,
                                   relwidth=1.0, relheight=1.0)
            self.page_host.update_idletasks()
        except Exception:  # noqa: BLE001, S110
            pass

        # 3) 预热开发者端（place 到 viewport 内，布局完成后撤回）
        try:
            self.developer_view.place(in_=self.page_host, relx=1.0, rely=0,
                                      relwidth=1.0, relheight=1.0)
            self.page_host.update_idletasks()
        except Exception:  # noqa: BLE001, S110
            pass

        # 4) 恢复玩家端为可见视图
        try:
            self.developer_view.place_forget()
            self.player_view.place(in_=self.page_host, relx=0, rely=0,
                                   relwidth=1.0, relheight=1.0)
            self.page_host.update_idletasks()
        except Exception:  # noqa: BLE001, S110
            pass

        # 5) 窗口移回屏幕中心（仍然 withdraw 状态，用户看不到）
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = max(0, (sw - Size.WIN_W) // 2)
            y = max(0, (sh - Size.WIN_H) // 2)
            self.geometry(f"{Size.WIN_W}x{Size.WIN_H}+{x}+{y}")
            self.update_idletasks()
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    # boot
    # ------------------------------------------------------------------
    def get_boot_tasks(self) -> list:
        """
        boot 任务列表 [(名称, 可调用对象), ...]。
        可调用对象返回 (ok: bool, msg: str) 或 None。
        在 splash 的后台线程执行，不允许操作 UI。
        """
        return [
            ("加载主题资源", self._boot_theme),
            ("检查拖拽支持", self._boot_check_dnd),
            ("探测缓存盘类型", self._boot_probe_disk),
            ("清理下载残留", self._boot_clean_parts),
            ("探测渲染引擎", self._boot_check_renderer),
        ]

    def _boot_theme(self):
        try:
            from ..theme import Color, Font, Size
            _ = (Color, Font, Size)      # 只探测"能不能导入"
            return True, ""
        except Exception:  # noqa: BLE001
            return False, "主题加载失败"

    def _boot_check_dnd(self):
        try:
            from tkinterdnd2 import DND_FILES
            _ = DND_FILES                # 只探测"能不能导入"
            return True, ""
        except Exception:  # noqa: BLE001
            return False, ""

    def _boot_probe_disk(self):
        """
        启动时探一次缓存盘类型（下载目标固定是 <database>/cache）。

        Windows 上 IOCTL 失败要走 PowerShell 兜底，最坏约 2 秒；放在下载前会
        明显拖慢首包，放在启动页（本来就有后台任务）则完全无感。
        探测结果被 downloader 缓存，之后每次下载只做一次字典命中。
        """
        try:
            from ..core import cache as cache_mod
            from ..core import downloader as dl_mod
            root = cache_mod.get_cache_root()
            if root is None:
                return True, ""          # 还没设置数据库目录，等用的时候再探
            info = dl_mod.probe_disk_cache(root)
            self._boot_disk_type = info.get("type", "unknown")
            return True, ""
        except Exception:  # noqa: BLE001
            return False, ""

    def _boot_clean_parts(self):
        try:
            from ..core import cache as cache_mod
            n = cache_mod.clean_orphan_parts()
            self._boot_cleaned = n
            # 上次异常退出留下的解压/合并工作目录（一次更新会残留一整份副本）
            try:
                w = cache_mod.clean_orphan_workdirs()
                self._boot_cleaned_workdirs = w
            except Exception:  # noqa: BLE001
                self._boot_cleaned_workdirs = 0
            return True, ""
        except Exception:  # noqa: BLE001
            return False, ""

    def _boot_check_renderer(self):
        try:
            from ..core.markdown_renderer import is_engine_available
            self._boot_renderer_ok = bool(is_engine_available())
            return True, ""
        except Exception:  # noqa: BLE001
            return False, ""

    # ------------------------------------------------------------------
    # 关闭守卫（两个视图共用一个顶层窗口的 WM_DELETE_WINDOW）
    # ------------------------------------------------------------------
    def install_close_guard(self, owner: str, handler):
        """
        注册一个关闭守卫。owner 用于区分来源：
            player    → 下载/应用进行中：强警告，允许强制关闭
            developer → 正在制作更新包：**不允许关闭**
        多个守卫同时存在时按 developer > player 的优先级派发。
        """
        self._close_guards[owner] = handler
        try:
            self.protocol("WM_DELETE_WINDOW", self._dispatch_close)
        except Exception:  # noqa: BLE001, S110
            pass

    def release_close_guard(self, owner: str):
        self._close_guards.pop(owner, None)
        try:
            # 一律走 _dispatch_close：即便没有守卫，也要播退场动画再关
            self.protocol("WM_DELETE_WINDOW", self._dispatch_close)
        except Exception:  # noqa: BLE001, S110
            pass

    def _dispatch_close(self):
        """把关闭请求交给优先级最高的守卫；一个都没有才真的关窗。"""
        if self._closing:
            return
        for owner in ("developer", "player"):
            handler = self._close_guards.get(owner)
            if handler is None:
                continue
            try:
                handler()
                return
            except Exception:  # noqa: BLE001, S112
                continue
        # 退场：淡出 + 下沉，结束后再销毁（重复点击 X 不会重复播）
        self._closing = True
        try:
            motion.pop_out(self, fall=16, on_done=self._destroy_now)
        except Exception:  # noqa: BLE001
            self._destroy_now()

    def _destroy_now(self):
        try:
            self._opacity_guard.stop()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.destroy()
        except Exception:  # noqa: BLE001, S110
            pass

    def on_exit_cleanup(self):
        """退出前清理工作目录（main.py 在 os._exit 之前调用）。"""
        for view in (getattr(self, "player_view", None),
                     getattr(self, "developer_view", None)):
            if view is None:
                continue
            for name in ("_discard_merged_source", "_cleanup_temp_dir",
                         "_cleanup_export_tmp"):
                fn = getattr(view, name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # noqa: BLE001, S110
                        pass

    def on_boot_done(self):
        """splash 完成后调用：显示主窗口 + 注册延时任务"""
        if self._boot_done:
            return
        self._boot_done = True

        # 先透明再显示，避免"闪一下全白再淡入"
        try:
            motion.prepare(self)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(100, lambda: self.attributes("-topmost", False))
        except Exception:  # noqa: BLE001, S110
            pass
        # 入场：淡入 + 从下方弹上来（带轻微过冲），450ms 内结束
        try:
            motion.pop_in(self, rise=Anim.RISE_PX,
                          duration=Anim.WINDOW_MS)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self._opacity_guard.apply(force=True)
        except Exception:  # noqa: BLE001, S110
            pass

        try:
            n = getattr(self, "_boot_cleaned", 0)
            if n > 0:
                try:
                    self.player_view.log.log(
                        "info", f"已清理 {n} 个未完成的下载文件")
                except Exception:  # noqa: BLE001, S110
                    pass
        except Exception:  # noqa: BLE001, S110
            pass

        try:
            w = getattr(self, "_boot_cleaned_workdirs", 0)
            if w > 0:
                try:
                    self.player_view.log.log(
                        "info", f"已清理 {w} 个上次遗留的临时解压目录")
                except Exception:  # noqa: BLE001, S110
                    pass
        except Exception:  # noqa: BLE001, S110
            pass

        try:
            kind = getattr(self, "_boot_disk_type", "")
            if kind and kind != "unknown":
                try:
                    from ..core import cache as cache_mod
                    from ..core import downloader as dl_mod
                    root = cache_mod.get_cache_root()
                    if root is not None:
                        self.player_view.log.log(
                            "info", dl_mod.disk_policy_summary(
                                root, dl_mod._get_options()))
                except Exception:  # noqa: BLE001, S110
                    pass
        except Exception:  # noqa: BLE001, S110
            pass

        try:
            from ..core import cache as cache_mod
            legacy = cache_mod.find_legacy_trash()
            if legacy:
                self.player_view.log.log(
                    "warn", "检测到旧版本留下的备份目录（新版本不再使用，"
                            "里面的文件是当时被替换的旧内容，可自行查看）：")
                for path in legacy[:3]:
                    self.player_view.log.log("warn", f"    {path}")
        except Exception:  # noqa: BLE001, S110
            pass

        self.after(200, self._check_database)
        self.after(400, self._check_apply_recovery)
        self.after(500, self._check_resume)
        self.after(600, self._log_ignored_count)

    def _check_database(self):
        """没配数据库时引导玩家选一个（首次运行 / 目录被删）。"""
        try:
            ensure_database(self)
        except Exception:  # noqa: BLE001, S110
            pass

    def _log_ignored_count(self):
        """提示还有几条被忽略的"更新中断"记录。"""
        try:
            db_root = db.get_db_path()
            if db_root is None:
                return
            n = resume_mod.count_ignored(db_root)
            if n > 0:
                self.player_view.log.log(
                    "info", f"有 {n} 个已忽略的更新中断记录，"
                            f"如需继续可重新拖入对应更新包")
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    # 上次更新被强杀 → 就地自检残渣 + 提示"向前重跑"
    # ------------------------------------------------------------------
    _RECOVERY_STAGE_LABEL: ClassVar[dict[str, str]] = {
        "apply": "正在替换目录",
        "copy": "正在写入文件",
        "delete": "正在删除旧文件",
        "verify": "正在校验",
    }

    def _check_apply_recovery(self):
        """
        发现"更新中途被强杀"的检查点：

          1) 先就地自检磁盘残渣（回滚没做完的目录替换、删搬运临时文件）
             —— 这些残渣不收拾，整合包会一直带着"半个目录"跑
          2) 再提示玩家"重新拖入同一个更新包再更新一次"

        恢复方向统一为"向前"：重跑时 `build_plan` 按整合包**当前**状态
        重新算 diff，缺什么补什么，天然幂等。"向后整体回滚"需要假设
        "更新后用户什么都没改过"，无法验证，所以不提供。
        """
        try:
            from ..core import checkpoint as cp_mod
            from ..core import recover as recover_mod
            cps = cp_mod.scan_checkpoints()
            if not cps:
                return
            cp = cps[0]
            root = Path(cp.pack_root)
            name = root.name or cp.pack_root
            stage = self._RECOVERY_STAGE_LABEL.get(cp.stage, cp.stage)
            extra = ""
            try:
                stats = recover_mod.recover_leftovers(
                    root, self.player_view.log.log)
                text = recover_mod.summary(stats)
                if text:
                    extra = f"\n\n已经顺手处理掉磁盘上的残留：{text}。"
            except Exception as e:  # noqa: BLE001
                self.player_view.log.log("warn", f"自检残留失败：{e}")
            try:
                self.player_view.log.log(
                    "warn", f"上次更新未正常结束（中断在：{stage}），"
                            f"建议重新拖入同一个更新包再更新一次")
            except Exception:  # noqa: BLE001, S110
                pass
            if cp.stage == "verify":
                tip = ("搬运本身已经跑完（可能在最后校验时被中断）："
                       "点「开始更新」重新比对一次就能确认。")
            else:
                tip = ("恢复办法：重新拖入同一个更新包，再点一次「开始更新」。\n"
                       "Easier 会按整合包**当前**状态重新比对，缺什么补什么，"
                       "一直跑到与更新包一致为止。\n\n")
            alert(
                self, "上次更新没有正常结束",
                f"检测到上次对整合包「{name}」的更新没有跑完"
                f"（中断在：{stage}）。\n\n{tip}{extra}",
                level="warn")
            cp_mod.clear_checkpoint(root)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    # resume 检查
    # ------------------------------------------------------------------
    def _check_resume(self):
        try:
            db_root = db.get_db_path()
            if db_root is None:
                return
            records = resume_mod.scan_all_resumes(db_root)
            if not records:
                return
            data = records[0]
            avail = resume_mod.check_resume_availability(data)
            if avail["ok"]:
                self._show_resume_confirm(data, avail)
            else:
                self._show_resume_locate(data, avail)
        except Exception:  # noqa: BLE001, S110
            pass

    def _show_resume_confirm(self, data: dict, avail: dict):
        pack_name = data.get("pack_name") or "未命名整合包"
        total = data.get("total", 0)
        completed = len(data.get("completed", []))
        remaining = max(0, total - completed)

        state = {"no_remind": False}

        def _on_checkbox(val: bool):
            state["no_remind"] = val

        def _on_result(ok: bool):
            if ok:
                self._on_resume_choice(True, data, avail)
            else:
                if state["no_remind"]:
                    self._mark_ignored(data)

        confirm(
            self,
            "继续上次更新？",
            f"检测到上次更新未完成：\n\n"
            f"整合包：{pack_name}\n"
            f"总文件：{total}\n"
            f"已完成：{completed}\n"
            f"待下载：{remaining}\n\n"
            f"是否继续？",
            confirm_text="继续更新",
            cancel_text="稍后",
            level="info",
            on_result=_on_result,
            extra_checkbox=("不再提醒", _on_checkbox))

    def _show_resume_locate(self, data: dict, avail: dict):
        pack_name = data.get("pack_name") or "未命名整合包"
        zip_reason = avail.get("zip_reason", "")
        pack_reason = avail.get("pack_reason", "")

        lines = [
            (f"检测到上次更新未完成（整合包：{pack_name}），"
            f"但以下项目已无法定位："),
            "",
        ]
        if not avail["zip_ok"]:
            lines.append(f"· 更新包：{zip_reason}")
        if not avail["pack_ok"]:
            lines.append(f"· 整合包：{pack_reason}")
        lines.append("")
        lines.append("可以在这里重新定位，让更新继续。")

        need_zip = not avail["zip_ok"]
        need_pack = not avail["pack_ok"]

        state = {"no_remind": False}

        def _on_checkbox(val: bool):
            state["no_remind"] = val

        def _on_cancel():
            if state["no_remind"]:
                self._mark_ignored(data)

        if need_pack and not need_zip:
            LocateDialog(
                self, "无法继续更新", "\n".join(lines),
                locate_label="重新定位整合包",
                dialog_title="选择整合包根目录",
                level="warn",
                on_located=lambda p, d=data, a=avail:
                    self._on_relocate_pack(d, a, p),
                on_cancel=_on_cancel,
                extra_checkbox=("不再提醒", _on_checkbox))
        elif need_zip and not need_pack:
            LocateDialog(
                self, "无法继续更新", "\n".join(lines),
                locate_label="重新定位更新包",
                dialog_title="选择更新包文件",
                level="warn",
                on_located=lambda p, d=data, a=avail:
                    self._on_relocate_zip(d, a, p),
                on_cancel=_on_cancel,
                extra_checkbox=("不再提醒", _on_checkbox))
        else:
            LocateDialog(
                self, "无法继续更新", "\n".join(lines),
                locate_label="重新定位整合包",
                dialog_title="选择整合包根目录",
                level="warn",
                on_located=lambda p, d=data, a=avail:
                    self._on_relocate_both(d, a, p),
                on_cancel=_on_cancel,
                extra_checkbox=("不再提醒", _on_checkbox))

    @staticmethod
    def _mark_ignored(data: dict):
        try:
            cache_root = Path(data.get("cache_root", ""))
            if cache_root.is_dir():
                resume_mod.mark_ignored(cache_root)
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _on_resume_choice(self, ok: bool, data: dict, avail: dict):
        if not ok:
            return
        try:
            cache_root = Path(data.get("cache_root", ""))
            if cache_root.is_dir():
                cur = resume_mod.read_resume(cache_root) or {}
                if resume_mod.is_ignored(cur):
                    resume_mod.write_resume(
                        cache_root,
                        Path(data.get("zip_path", "")),
                        data.get("pack_root") or None,
                        list(data.get("completed", [])),
                        list(data.get("failed", [])),
                        int(data.get("total", 0)),
                        float(data.get("started_at", 0)),
                        data.get("pack_name", ""),
                        data.get("version", ""),
                        ignored_at=0.0)

            if self._current != "玩家":
                self._on_identity_changed("玩家")
            self.player_view.resume_from(data, avail=avail)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_relocate_pack(self, data: dict, avail: dict,
                          pack_path: Path):
        try:
            if not ((pack_path / "mods").is_dir()
                    or (pack_path / "config").is_dir()):
                alert(self, "无法继续更新",
                      "该目录不是有效的整合包根目录"
                      "（缺少 mods / config 文件夹）。",
                      level="error")
                return
            self._update_resume_pack_root(data, pack_path)
            avail2 = resume_mod.check_resume_availability(data)
            if avail2["ok"]:
                self._on_resume_choice(True, data, avail2)
            else:
                self._show_resume_locate(data, avail2)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_relocate_zip(self, data: dict, avail: dict,
                         zip_path: Path):
        try:
            if zip_path.is_file():
                self._update_resume_zip(data, zip_path)
            avail2 = resume_mod.check_resume_availability(data)
            if avail2["ok"]:
                self._on_resume_choice(True, data, avail2)
            else:
                self._show_resume_locate(data, avail2)
        except Exception:  # noqa: BLE001, S110
            pass

    def _on_relocate_both(self, data: dict, avail: dict,
                          pack_path: Path):
        try:
            if not ((pack_path / "mods").is_dir()
                    or (pack_path / "config").is_dir()):
                alert(self, "无法继续更新",
                      "该目录不是有效的整合包根目录"
                      "（缺少 mods / config 文件夹）。",
                      level="error")
                return
            self._update_resume_pack_root(data, pack_path)
            avail2 = resume_mod.check_resume_availability(data)
            if avail2["ok"]:
                self._on_resume_choice(True, data, avail2)
            else:
                self._show_resume_locate(data, avail2)
        except Exception:  # noqa: BLE001, S110
            pass

    @staticmethod
    def _update_resume_pack_root(data: dict, pack_root: Path):
        try:
            data["pack_root"] = str(Path(pack_root).resolve())
            cache_root = Path(data.get("cache_root", ""))
            if cache_root.is_dir():
                resume_mod.write_resume(
                    cache_root,
                    Path(data.get("zip_path", "")),
                    pack_root,
                    list(data.get("completed", [])),
                    list(data.get("failed", [])),
                    int(data.get("total", 0)),
                    float(data.get("started_at", 0)),
                    data.get("pack_name", ""),
                    data.get("version", ""))
        except Exception:  # noqa: BLE001, S110
            pass

    @staticmethod
    def _update_resume_zip(data: dict, zip_path: Path):
        try:
            data["zip_path"] = str(Path(zip_path).resolve())
            cache_root = Path(data.get("cache_root", ""))
            if cache_root.is_dir():
                resume_mod.write_resume(
                    cache_root,
                    zip_path,
                    data.get("pack_root", "") or None,
                    list(data.get("completed", [])),
                    list(data.get("failed", [])),
                    int(data.get("total", 0)),
                    float(data.get("started_at", 0)),
                    data.get("pack_name", ""),
                    data.get("version", ""))
        except Exception:  # noqa: BLE001, S110
            pass

    # ------------------------------------------------------------------
    def _on_identity_changed(self, identity: str) -> bool:
        """
        返回 True = 接受这次切换；False = 拒绝（开关会自己回滚）。

        切换动画约 250ms，期间再点另一端旧实现是**静默吞掉**：开关的高亮条
        已经移过去了，页面却还停在原处 —— 看起来就是"界面显示开发者、内容
        还是玩家"。现在改成：记下最后一次请求，动画结束立刻补切。
        """
        if identity == self._current:
            # 已经在这个身份上：把之前排队的切换作废，
            # 否则动画结束时又会切回上一次点的那边（"吞掉最后一次点击"）
            self._pending_identity = ""
            self._sync_switch(identity)
            return True
        if self._switching:
            self._pending_identity = identity
            # 开关已经先动了；这里把它掰回当前页面，避免错位
            self._sync_switch(self._current)
            return False

        self._switching = True
        self._pending_identity = ""
        self._current = identity

        self.sidebar.set_identity(identity)
        self._sync_switch(identity)

        if identity == "开发者":
            self._switch(self.player_view, self.developer_view)
        else:
            self._switch(self.developer_view, self.player_view)
        return True

    def _sync_switch(self, identity: str):
        """让侧边栏的身份开关与当前页面一致（程序切换时也要同步）。"""
        sync = getattr(self.sidebar, "sync_identity_switch", None)
        if callable(sync):
            try:
                sync(identity)
            except Exception:  # noqa: BLE001, S110
                pass

    def _switch(self, old, new):
        """
        玩家 ↔ 开发者：水平滑动换页。

        滑动期间把两个视图里的**文本控件**（更新日志编辑区、日志区）盖上
        一块纯色板：Tk 移动 Text 控件会"拉丝"，盖住之后滑动的只是色板。
        """
        suspended: list = []
        veiled: list = []

        self._switch_finishing = False

        def _finish():
            # 三个入口（动画正常结束 / 控件被销毁 / 兜底定时器）只能收尾一次
            if self._switch_finishing:
                return
            self._switch_finishing = True

            if self._switch_guard is not None:
                try:
                    self.after_cancel(self._switch_guard)
                except Exception:  # noqa: BLE001, S110
                    pass
                self._switch_guard = None

            try:
                clear_overlays(self.page_host)
            except Exception:  # noqa: BLE001, S110
                pass

            # 兜底摆位：动画中途被打断时，旧页可能还压在下面没撤
            try:
                if old.winfo_exists():
                    old.place_forget()
                if new.winfo_exists():
                    new.place(in_=self.page_host, relx=0, rely=0,
                              relwidth=1.0, relheight=1.0)
            except Exception:  # noqa: BLE001, S110
                pass

            for obj in veiled:
                try:
                    obj.set_transition_veil(False)
                except Exception:  # noqa: BLE001, S110
                    pass
            for editor in suspended:
                try:
                    editor._resume_render()
                except Exception:  # noqa: BLE001, S110
                    pass
            self._switching = False

            pending = self._pending_identity
            self._pending_identity = ""
            if pending and pending != self._current:
                try:
                    self.after(0,
                               lambda p=pending: self._on_identity_changed(p))
                except Exception:  # noqa: BLE001, S110
                    pass

        # 兜底：万一动画回调没跑（异常/控件提前销毁），也不能让
        # _switching 永久为 True —— 那会让身份切换彻底失效。
        # 必须在 slide_pages 之前挂上：它的"容器还没布局"分支会同步回调
        # _finish，那时要能把这个定时器取消掉。
        try:
            self._switch_guard = self.after(
                Anim.SWITCH_MS + 650, _finish)
        except Exception:  # noqa: BLE001
            self._switch_guard = None

        # 首选：logo 覆盖层（滑入 → 盖住时换页 → 上下对开）。
        # 全程只有两块纯色面板 + 一张 logo 在动，页面上没有控件位移，
        # 既不拉丝也不依赖截图。有弹窗压着时也不影响（覆盖层在最上面）。
        fast_ok = False
        try:
            fast_ok = logo_transition(self.page_host, old, new,
                                      on_done=_finish)
        except Exception:  # noqa: BLE001, S110
            fast_ok = False
        if fast_ok:
            return

        # 次选：把旧页截成一张图滑走（页面上的控件一个都不动 → 不会拉丝）。
        # 但如果此刻有弹窗/模态压在页面上，截图会把别人的像素一起烤进去，
        # 那样滑动的就是"一张带着别人窗口的图" —— 这种情况退回盖色板。
        if not self._has_overlay_window():
            try:
                self.lift()
                fast_ok = snapshot_slide(self.page_host, old, new,
                                         duration_ms=Anim.SWITCH_MS,
                                         on_done=_finish)
            except Exception:  # noqa: BLE001, S110
                fast_ok = False
        if fast_ok:
            return

        # 截图不可用：退回"盖色板 + 搬页面"（会尽力遮住会拉丝的控件）
        for view in (old, new):
            for obj in self._find_transition_veils(view):
                try:
                    obj.set_transition_veil(True)
                    veiled.append(obj)
                except Exception:  # noqa: BLE001, S110
                    pass
            editor = self._find_markdown_editor(view)
            if editor is not None and hasattr(editor, "_suspend_render"):
                try:
                    editor._suspend_render()
                    suspended.append(editor)
                except Exception:  # noqa: BLE001, S110
                    pass

        slide_pages(self.page_host, old, new, direction="left",
                    duration_ms=Anim.SWITCH_MS,
                    on_done=_finish)

    def _has_overlay_window(self) -> bool:
        """页面上是否有弹窗/模态挡着（有就不适合用截屏做换页动画）。"""
        try:
            if self.grab_current() is not None:
                return True
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            for w in self.winfo_children():
                if w.winfo_class() == "Toplevel" and w.winfo_ismapped():
                    return True
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            for w in self.winfo_toplevel().winfo_children():
                if w is self:
                    continue
                if w.winfo_class() == "Toplevel" and w.winfo_ismapped():
                    return True
        except Exception:  # noqa: BLE001, S110
            pass
        return False

    @staticmethod
    def _find_transition_veils(view):
        """视图里需要"滑动时盖住"的控件（Text 类会拉丝）。"""
        found = []
        for attr in ("editor", "markdown_editor", "log", "log_panel"):
            obj = getattr(view, attr, None)
            if obj is not None and hasattr(obj, "set_transition_veil"):
                found.append(obj)
        return found

    @staticmethod
    def _find_markdown_editor(view):
        for attr in ("editor", "markdown_editor"):
            obj = getattr(view, attr, None)
            if obj is not None and hasattr(obj, "_suspend_render"):
                return obj
        return None