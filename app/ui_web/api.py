"""
api.py — js_api 桥（JS → Python）
------------------------------------------------
pywebview 只暴露三个方法：invoke / pick_path / 内部用 handle_drop。
其余全部走 invoke 的名字白名单，统一信封：

    成功  {"ok": true,  "data": ...}
    失败  {"ok": false, "error": {"code", "message", "detail"}}

约定：这里只做参数校验 + 任务提交，重活全部交给 JobRunner 或控制器。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from .events import EventBus


class ApiError(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class Api:
    def __init__(self, controller, bus: EventBus, jobs=None):
        self.controller = controller
        self.bus = bus
        self.jobs = jobs
        self.window = None
        self._lock = threading.RLock()
        self._methods: dict[str, Callable[[dict], Any]] = {
            "app.bootstrap": lambda p: self.controller.bootstrap(),
            "app.state": lambda p: self.controller.snapshot(),
            "app.role.set": lambda p: self.controller.set_role(p.get("role", "player")),
            "app.pick_pack": lambda p: self.controller.pick_pack(p.get("path", "")),
            "app.pack.clear": lambda p: self.controller.clear_pack(),
            "app.recent.open": lambda p: self.controller.open_path(p.get("path", "")),
            "app.recent.clear": lambda p: self.controller.clear_recent(),
            "app.settings.get": lambda p: self.controller.settings(),
            "app.settings.set": lambda p: self.controller.update_settings(p.get("patch") or {}),
            "app.dialog.respond": lambda p: self.controller.dialog_respond(
                p.get("id", ""), p.get("button", ""), bool(p.get("checked"))),
            "app.log.clear": lambda p: self.controller.clear_log(),
            "app.open_external": lambda p: self.controller.open_external(p.get("url", "")),
            # ---- 更新流程 ----
            "update.pick": lambda p: self.controller.attach_update(
                self.controller.flow.import_update(p.get("path", ""))),
            "update.clear": lambda p: self.controller.flow.clear(),
            "flow.state": lambda p: self.controller.flow.snapshot(),
            "update.changelog": lambda p: {"changelog": self.controller.flow.changelog},
            "pack.scan": lambda p: self.controller.flow.start_scan(
                p.get("strategies") or {}, p.get("checked") or {}),
            "update.apply": lambda p: self.controller.flow.start_apply(),
            "update.abort": lambda p: self.controller.flow.abort(),
            "update.retry": lambda p: self.controller.flow.retry(p.get("path", "")),
            "update.giveup": lambda p: self.controller.flow.give_up(),
            "update.strategy.set": lambda p: self.controller.flow.set_strategy(
                p.get("name", ""), p.get("value", ""), p.get("checked")),
            "update.strategy.setAll": lambda p: self.controller.flow.set_all(
                p.get("value", ""), p.get("checked"), p.get("names")),
            "update.resume": lambda p: self.controller.flow.resume(),
            # ---- 开发者：制作更新包 ----
            "dev.pick": lambda p: self.controller.dev.import_zip(p.get("path", "")),
            "dev.state": lambda p: self.controller.dev.snapshot(),
            "dev.clear": lambda p: self.controller.dev.clear(),
            "dev.option.set": lambda p: self.controller.dev.set_option(
                p.get("key", ""), bool(p.get("value"))),
            "dev.changelog.set": lambda p: self.controller.dev.set_changelog(
                p.get("text", "")),
            "dev.strategy.set": lambda p: self.controller.dev.set_strategy(
                p.get("name", ""), p.get("value", ""), p.get("excluded")),
            "dev.export": lambda p: self.controller.dev.start_export(p.get("path", "")),
            # ---- 白名单 / 数据库 ----
            "app.whitelist.get": lambda p: self.controller.whitelist(),
            "app.whitelist.set": lambda p: self.controller.set_whitelist(
                p.get("items") or []),
            "app.db.get": lambda p: self.controller.db_path(),
            "app.db.set": lambda p: self.controller.set_db_path(p.get("path", "")),
        }

    def bind_window(self, window) -> None:
        self.window = window

    # ------------------------------------------------------------------
    def invoke(self, method: str, params: dict | None = None) -> dict:
        handler = self._methods.get(str(method))
        if handler is None:
            return _err("UNKNOWN_METHOD", f"未知操作：{method}")
        params = params if isinstance(params, dict) else {}
        try:
            with self._lock:
                data = handler(params)
            return {"ok": True, "data": data}
        except ApiError as exc:
            return _err(exc.code, exc.message, exc.detail)
        except PermissionError as exc:
            return _err("FORBIDDEN", str(exc))
        except (ValueError, FileNotFoundError) as exc:
            return _err("INVALID_INPUT", str(exc))
        except Exception as exc:                      # noqa: BLE001
            try:
                self.bus.log(f"操作 {method} 失败：{exc}", "ERROR")
            except Exception:                          # noqa: BLE001, S110
                pass
            return _err(getattr(exc, "code", "INTERNAL"), str(exc) or "操作失败")

    # ------------------------------------------------------------------
    def pick_path(self, kind: str = "file", filters: str = "") -> dict:
        if self.window is None:
            return _err("NO_WINDOW", "窗口尚未就绪")
        try:
            import webview
            if kind == "folder":
                result = self.window.create_file_dialog(
                    webview.FileDialog.FOLDER)
            elif kind == "save":
                result = self.window.create_file_dialog(
                    webview.FileDialog.SAVE, save_filename=filters or "")
            else:
                patterns = tuple(filters) if filters else ()
                result = self.window.create_file_dialog(
                    webview.FileDialog.OPEN,
                    allow_multiple=False,
                    file_types=patterns)
            path = str(result[0]) if result else ""
            return {"ok": True, "data": {"path": path}}
        except Exception as exc:                       # noqa: BLE001
            return _err("DIALOG_ERROR", str(exc))

    # ------------------------------------------------------------------
    def handle_drop(self, paths) -> dict:
        """由 Python 侧 DOM drop 处理器调用（路径来自 pywebviewFullPath）。"""
        if isinstance(paths, str):
            paths = [paths]
        clean: list[str] = []
        for raw in paths or []:
            try:
                p = Path(str(raw)).expanduser()
                if p.exists():
                    clean.append(str(p.resolve()))
            except Exception:                          # noqa: BLE001, S110
                continue
        if not clean:
            self.bus.dnd("error", "没能拿到拖入文件的路径，请用点击选择")
            return _err("NO_PATH", "没能拿到拖入文件的路径")
        self.bus.emit("dnd", {"kind": "ok", "paths": clean})
        try:
            self.controller.open_path(clean[0])
        except ApiError as exc:
            self.bus.dnd("error", exc.message)
            return _err(exc.code, exc.message)
        return {"ok": True, "data": {"paths": clean}}


def _err(code: str, message: str, detail: str = "") -> dict:
    return {"ok": False, "error": {"code": code, "message": message,
                                   "detail": detail}}
