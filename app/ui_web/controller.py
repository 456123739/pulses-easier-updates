"""
controller.py — 无 UI 的应用控制器
------------------------------------------------
Tk 版把"界面 + 状态机"揉在 player_view.py 里。Web 版把状态机搬到这里：
控制器只认数据与事件，不认识任何控件；前端只渲染它推出去的快照。

线程模型：
  - js_api 方法在 pywebview 的线程里被调用 → 只做轻活（读状态、提交任务）
  - 扫描/下载/应用等长任务走 JobRunner 单写者队列
  - 所有对外通知都通过 EventBus（EventPump 负责批量推给前端）
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..core import __version__ as APP_VERSION
from ..core import database, engines, pack_info, pack_locator, recent
from .api import ApiError
from .events import EventBus
from .dev import DevFlow
from .flow import Flow


class AppController:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._lock = threading.RLock()
        self._role = "player"
        self._pack: dict | None = None
        self._busy = False
        self._dialogs: dict[str, dict] = {}
        self._dialog_seq = 0
        self.flow = Flow(self, bus)
        self.dev = DevFlow(self, bus)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": APP_VERSION,
                "role": self._role,
                "pack": dict(self._pack) if self._pack else None,
                "recent": self._recent_list(),
                "settings": self.settings(),
                "busy": self._busy or self.flow.busy() or self.dev.busy(),
                "job": None,
                "flow": self.flow.snapshot(),
                "dev": self.dev.snapshot(),
            }

    def bootstrap(self) -> dict:
        state = self.snapshot()
        self.bus.state(state)
        self.bus.log(f"Pulses Easier v{APP_VERSION} 启动", "SUCCESS")
        db = database.get_db_path()
        if db:
            self.bus.log(f"数据库位置：{db}", "INFO")
        else:
            self.bus.log("尚未设置数据库位置", "WARNING")
        return state

    def _recent_list(self) -> list[dict]:
        out = []
        try:
            for p in recent.load_recent():
                out.append({"name": Path(p).name, "path": str(p)})
        except Exception:                              # noqa: BLE001, S110
            pass
        return out

    # ------------------------------------------------------------------
    # 身份 / 设置
    # ------------------------------------------------------------------
    def set_role(self, role: str) -> dict:
        role = "developer" if role == "developer" else "player"
        with self._lock:
            self._role = role
        self._push_state()
        return {"role": role}

    def settings(self) -> dict:
        out: dict = {}
        try:
            dl = database.get_download_options()
            ui = database.get_ui_options()
            out.update({
                "engine": dl.get("engine", "ridex"),
                "mirror_first": dl.get("mirror_first", 1),
                "multi_slots": dl.get("multi_slots", 8),
                "window_anim": ui.get("window_anim", 1),
                "log_max_lines": ui.get("log_max_lines", 500),
            })
        except Exception:                              # noqa: BLE001, S110
            pass
        return out

    def update_settings(self, patch: dict) -> dict:
        dl_keys = {"engine", "mirror_first", "multi_slots"}
        ui_keys = {"window_anim", "log_max_lines"}
        dl_patch = {k: v for k, v in patch.items() if k in dl_keys}
        ui_patch = {k: v for k, v in patch.items() if k in ui_keys}
        if dl_patch:
            database.set_download_options({**database.get_download_options(), **dl_patch})
        if ui_patch:
            database.set_ui_options({**database.get_ui_options(), **ui_patch})
        self._push_state()
        return self.settings()

    # ------------------------------------------------------------------
    # 整合包
    # ------------------------------------------------------------------
    def open_path(self, raw: str) -> dict:
        if not raw:
            raise ApiError("INVALID_INPUT", "缺少路径")
        root = pack_locator.locate_modpack(raw)
        if not root:
            raise ApiError("NOT_A_PACK", "这个路径里没有找到整合包（需要 mods/ 与 config/）")
        return self._load_pack(Path(root))

    def pick_pack(self, raw: str) -> dict:
        return self.open_path(raw)

    def _load_pack(self, root: Path) -> dict:
        info = pack_info.read_modpack_info(root)
        self.flow.pack_root = Path(root)
        with self._lock:
            self._pack = {
                "path": str(root),
                "name": info.name,
                "version": info.version,
                "fileCount": info.mod_count,
                "updatePack": None,
            }
        try:
            recent.add_recent(root)
        except Exception:                              # noqa: BLE001, S110
            pass
        self.bus.log(f"整合包已就绪：{info.name}（{info.version}）", "SUCCESS")
        self._push_state()
        state = self.snapshot()
        return {"state": state}

    def clear_pack(self) -> dict:
        try:
            self.flow.clear()
        except Exception:                              # noqa: BLE001, S110
            pass
        self.flow.pack_root = None
        with self._lock:
            self._pack = None
        self.bus.log("已清除整合包", "INFO")
        self._push_state()
        return {"state": self.snapshot()}

    def clear_recent(self) -> dict:
        try:
            recent.clear_recent()
        except Exception:                              # noqa: BLE001, S110
            pass
        self._push_state()
        return {"recent": []}

    # ------------------------------------------------------------------
    # 其它
    # ------------------------------------------------------------------
    def dialog_respond(self, dialog_id: str, button: str, checked: bool) -> dict:
        spec = self._dialogs.pop(dialog_id, None)
        resolver = (spec or {}).get("_resolve")
        if callable(resolver):
            resolver(button, checked)
        return {"ok": True}

    def ask(self, kind: str, message: str, *, title: str = "",
            buttons=None, checkbox=None, detail: str = "") -> None:
        """给控制器内部用的非阻塞提示（前端弹出即可，不需要回值）。"""
        self._dialog_seq += 1
        spec = {"id": f"d{self._dialog_seq}", "kind": kind,
                "message": message, "title": title}
        if buttons:
            spec["buttons"] = buttons
        if checkbox:
            spec["checkbox"] = checkbox
        if detail:
            spec["detail"] = detail
        self.bus.dialog(spec)

    # ------------------------------------------------------------------
    # 白名单 / 数据库位置
    # ------------------------------------------------------------------
    def whitelist(self) -> dict:
        try:
            items = database.get_whitelist()
        except Exception:                              # noqa: BLE001
            items = []
        return {"whitelist": list(items)}

    def set_whitelist(self, items) -> dict:
        clean = []
        for raw in items or []:
            name = str(raw).strip().strip("/\\")
            if name and "/" not in name and name not in clean:
                clean.append(name)
        try:
            database.set_whitelist(clean)
        except Exception as exc:                       # noqa: BLE001
            raise ApiError("DB_WRITE", f"保存白名单失败：{exc}") from exc
        self.push_state()
        return {"whitelist": clean}

    def db_path(self) -> dict:
        try:
            raw = database.get_db_path()
        except Exception:                              # noqa: BLE001
            raw = None
        return {"path": str(raw) if raw else ""}

    def set_db_path(self, raw: str) -> dict:
        path = Path(str(raw)).expanduser()
        # 允许前端传"文件"，但数据库是一整个目录（config.json 等都在里面）
        if path.suffix:
            path = path.parent
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                database.create_database(path)
            database.set_db_path(path)
        except Exception as exc:                       # noqa: BLE001
            raise ApiError("DB_OPEN", f"无法使用这个位置：{exc}") from exc
        self.bus.log(f"数据库位置：{path}", "INFO")
        self.push_state()
        return {"path": str(path)}

    def clear_log(self) -> dict:
        return {"cleared": True}

    def open_external(self, url: str) -> dict:
        import webbrowser
        if not url.lower().startswith(("http://", "https://")):
            raise ApiError("INVALID_INPUT", "只允许打开 http(s) 链接")
        webbrowser.open(url)
        return {"opened": url}

    # ------------------------------------------------------------------
    def set_busy(self, busy: bool) -> None:
        with self._lock:
            self._busy = bool(busy)
        self._push_state()

    def push_state(self) -> None:
        self.bus.state(self.snapshot())

    # 旧名字保留：内部还有调用点
    _push_state = push_state

    # ------------------------------------------------------------------
    # 更新流程：给 Flow 用的能力
    # ------------------------------------------------------------------
    def cache_root(self) -> Path:
        try:
            opts = database.get_download_options()
            raw = str(opts.get("cache_dir", "") or "").strip()
            if raw:
                p = Path(raw).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                return p
        except Exception:                              # noqa: BLE001, S110
            pass
        p = Path.home() / ".pulses-easier" / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def engine_name(self) -> str:
        try:
            return str(database.get_download_options().get("engine", "ridex"))
        except Exception:                              # noqa: BLE001
            return "ridex"

    def engine(self):
        return engines.load(self.engine_name())

    def threads(self) -> int:
        try:
            return max(1, min(32, int(
                database.get_download_options().get("multi_workers", 8) or 8)))
        except Exception:                              # noqa: BLE001
            return 8

    def refresh_pack(self) -> None:
        with self._lock:
            pack = dict(self._pack) if self._pack else None
        if not pack:
            return
        try:
            info = pack_info.read_modpack_info(Path(pack["path"]))
            with self._lock:
                self._pack = {**pack, "name": info.name,
                              "version": info.version,
                              "fileCount": info.mod_count}
        except Exception:                              # noqa: BLE001, S110
            pass
        self.push_state()

    def attach_update(self, snap: dict) -> dict:
        with self._lock:
            if self._pack is not None:
                self._pack["updatePack"] = snap.get("update")
        self.push_state()
        return self.snapshot()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def can_close(self) -> bool:
        """关闭守卫：更新流程进行中不允许直接关闭窗口。"""
        with self._lock:
            busy = self._busy
        if not busy:
            return True
        self.ask("warn", "更新流程正在进行，暂时无法关闭。",
                 title="请稍候")
        return False

    def shutdown(self) -> None:
        """窗口关闭后的清理钩子（任务队列由 JobRunner 自己回收）。"""
        with self._lock:
            self._dialogs.clear()
