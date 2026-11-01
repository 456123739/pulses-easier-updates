"""
api.py — js_api 桥（JS → Python）
------------------------------------------------
pywebview 只暴露三个方法：invoke / pick_path / 内部用 handle_drop。
其余全部走 invoke 的名字白名单，统一信封：

    成功  {"ok": true,  "data": ...}
    失败  {"ok": false, "error": {"code", "message", "detail"}}

约定：这里只做参数校验 + 任务提交，重活全部交给 JobRunner 或控制器。

端到端（PULSES_E2E=1）时才多出这几个口子，平时一个都不注册：
    invoke('e2e.config')   夹具路径（页面内驱动按它决定跑哪几套场景）
    invoke('e2e.fs')       只读探针：夹具目录里的文件内容/大小/哈希
    invoke('e2e.can_close') 关闭守卫（真机上等价于 window.events.closing）
    api.e2e_report(payload) 页面跑完回传结果 → 写 PULSES_E2E_REPORT → 关窗
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .events import EventBus

_E2E_ON = ("1", "true", "yes", "on")


def e2e_enabled() -> bool:
    """是否处于端到端模式（只有这个时候才注册 e2e.* 方法）。"""
    return os.environ.get("PULSES_E2E", "").strip().lower() in _E2E_ON


def _e2e_cfg() -> dict | None:
    """端到端夹具配置：优先 PULSES_E2E_CFG 指定的 JSON。

    兼容老写法（只给 PULSES_E2E_PACK / PULSES_E2E_UPDATE）：那时只跑主流程。
    """
    path = os.environ.get("PULSES_E2E_CFG", "").strip()
    if path and Path(path).is_file():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            data = None
        if isinstance(data, dict):
            return data
    pack = os.environ.get("PULSES_E2E_PACK", "").strip()
    update = os.environ.get("PULSES_E2E_UPDATE", "").strip()
    if pack and update:
        return {"work": str(Path(pack).resolve().parent),
                "basic": {"pack": pack, "update": update}}
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class ApiError(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class Api:
    def __init__(self, controller, bus: EventBus, jobs=None,
                 e2e: bool | None = None):
        self.controller = controller
        self.bus = bus
        self.jobs = jobs
        self.window = None
        # 报告落盘之后才允许关窗：关窗守卫会看这个标记
        self.e2e_done = False
        self.e2e = e2e_enabled() if e2e is None else bool(e2e)
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
            "update.config.export": lambda p: self.controller.flow.export_config(
                p.get("path", "")),
            "update.config.import": lambda p: self.controller.flow.import_config(
                p.get("path", "")),
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
        if self.e2e:
            self._methods.update({
                "e2e.config": self._e2e_config,
                "e2e.fs": self._e2e_fs,
                "e2e.can_close": self._e2e_can_close,
            })
            # 直接暴露成 js_api 方法：页面跑完后 `api.e2e_report(payload)`。
            # 平时不注册 → 产品里没有这个入口。
            setattr(self, "e2e_report", self._e2e_report)

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
    # 端到端（PULSES_E2E=1 才注册；页面内驱动只认这几个口子）
    # ------------------------------------------------------------------
    def _e2e_config(self, params: dict) -> dict:
        """把夹具路径交给页面：驱动按 basic / download 决定跑哪几套。"""
        cfg = _e2e_cfg()
        if not cfg:
            raise ApiError("NO_CONFIG", "没有 e2e 夹具配置（PULSES_E2E_CFG）")
        out = {k: v for k, v in cfg.items() if k != "report"}
        out.setdefault("work", str(Path(str(out.get("work") or ".")).resolve()))
        out.setdefault("suite", "all")
        out["report"] = os.environ.get("PULSES_E2E_REPORT", "")
        return out

    def _e2e_fs(self, params: dict) -> dict:
        """只读探针：页面没有文件系统，磁盘断言得由 Python 代读。

        只允许读夹具 work 目录下的东西（路径逃逸直接拒绝），不写、不删。
        """
        cfg = _e2e_cfg() or {}
        root_raw = str(cfg.get("work") or os.environ.get("PULSES_E2E_WORK") or "")
        if not root_raw.strip():
            raise ApiError("NO_CONFIG", "e2e.fs 需要 PULSES_E2E_CFG 里的 work 目录")
        root = Path(root_raw).resolve()
        paths = [str(p) for p in (params.get("paths") or [])][:64]
        want_text = bool(params.get("text"))
        want_sha = bool(params.get("sha256"))
        files: dict[str, dict] = {}
        for raw in paths:
            target = Path(raw).expanduser().resolve()
            if target != root and root not in target.parents:
                raise ApiError("FORBIDDEN", f"e2e.fs 只能读夹具目录：{target}")
            item: dict = {"path": str(target), "exists": target.exists()}
            if item["exists"]:
                item["isDir"] = target.is_dir()
                if target.is_file():
                    item["size"] = target.stat().st_size
                    if want_sha:
                        item["sha256"] = _sha256(target)
                    if want_text and item["size"] <= 256 * 1024:
                        item["text"] = target.read_text(encoding="utf-8",
                                                        errors="replace")
            files[raw] = item
        return {"files": files}

    def _e2e_can_close(self, params: dict) -> dict:
        """真机上这一问等价于 window.events.closing：拦不拦、为什么拦。"""
        ok = bool(self.controller.can_close())
        return {"canClose": ok, "blocked": not ok}

    def _e2e_report(self, payload=None) -> dict:
        """页面跑完回传结果：先落盘，再关窗。

        顺序很重要：pywebview 是把返回值再用 evaluate_js 送回页面的，这里若先
        关窗，回调就没地方送了（那条路没有超时，会把调用线程吊死）。所以关窗
        放到后台线程，报告已经落盘，关不掉也不影响结论。
        """
        data = payload if isinstance(payload, dict) else {}
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            return {"ok": False,
                    "error": {"code": "INVALID_INPUT",
                              "message": "report.steps 必须是数组"}}
        steps = []
        for item in raw_steps[:500]:
            if not isinstance(item, dict):
                continue
            steps.append({"name": str(item.get("name") or "")[:200],
                          "ok": bool(item.get("ok")),
                          "detail": str(item.get("detail") or "")[:800]})
        artifacts = data.get("artifacts")
        report = {
            "ok": bool(data.get("ok")) and bool(steps)
                  and all(s["ok"] for s in steps),
            "steps": steps,
            "error": str(data.get("error") or "")[:1000],
            "artifacts": artifacts if isinstance(artifacts, dict) else {},
            "suite": str(data.get("suite") or ""),
            "ms": int(data.get("ms") or 0),
        }
        path = Path(os.environ.get("PULSES_E2E_REPORT", "").strip()
                    or "e2e-report.json").resolve()
        written = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
            written = True
        except Exception as exc:                       # noqa: BLE001
            report["error"] = (report["error"]
                               + f"（写报告失败：{exc}）").strip()
        self.e2e_done = True
        try:
            self.bus.log(f"e2e 报告：{path}（{len(steps)} 步，"
                         f"{'通过' if report['ok'] else '失败'}）", "INFO")
        except Exception:                              # noqa: BLE001, S110
            pass
        self._e2e_quit()
        return {"ok": True,
                "data": {"path": str(path), "steps": len(steps),
                         "written": written}}

    def _e2e_quit(self) -> None:
        """后台关窗：报告已落盘，这里失败也不影响结论。"""
        window = self.window
        if window is None:
            return

        def work():
            try:
                time.sleep(0.6)          # 让返回值先送回页面
                window.destroy()
            except Exception:                          # noqa: BLE001, S110
                pass

        threading.Thread(target=work, daemon=True, name="e2e-quit").start()

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
