"""
flow.py — 更新流程的无头实现（导入 → 比对 → 下载 → 应用）
------------------------------------------------
Tk 界面那套流程（player_view.py，约 3000 行）把"流程"和"控件"拌在一起：
状态散在几十个实例属性上、进度靠 ui_post 回主线程。Web 界面需要
同样的流程但没有 Tk，所以把**判定与编排**收敛到这里：

- 状态只有一份（Flow 实例），UI 通过 snapshot 拿快照、通过事件拿进度；
- 线程只碰 core，绝不碰界面；
- 策略/勾选由 UI 传进来（dict），不在这里读控件。

流程与 Tk 版一致：
    导入更新包 → 解压/清单比对 → 建策略表 → 收集下载任务
    → 引擎下载到缓存 → build_plan/execute_plan → 复核
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from ..config import (Strategy, default_strategy_for_dir,
                      default_strategy_for_file)
from ..core import config_exclude
from ..core import database as db
from ..core import eapack as eapack_mod
from ..core.apply_rules import is_checked as _rule_is_checked
from ..core.apply_rules import resolve_strategy, will_skip
from ..core.differ import DiffResult, diff_from_manifest, diff_packs_parallel
from ..core.downloader import DownloadTask
from ..core.mrpack import (is_reserved_root_name, locate_content_root,
                           parse_mrpack)
from ..core.pack_detector import PackKind, detect_pack_kind
from ..core.packio import _extract_all_members, _extract_root_members, _zip_layout
from ..core.updater import (PlanSource, SourceLayer, build_plan,
                            execute_plan, verify_after_update)

PHASE_IDLE = "idle"
PHASE_SCAN = "scan"
PHASE_DOWNLOAD = "download"
PHASE_APPLY = "apply"
PHASE_DONE = "done"


class FlowError(RuntimeError):
    pass


class Flow:
    """一次更新会话的全部状态。同一个实例串行执行，不并发。"""

    def __init__(self, controller, bus):
        self.controller = controller
        self.bus = bus
        self._lock = threading.RLock()

        self.phase = PHASE_IDLE
        self.pack_root: Path | None = None
        self.update_zip: Path | None = None
        self.pack_kind: PackKind | None = None
        self.update_info: dict = {}
        self.changelog: str = ""

        self.diff: DiffResult | None = None
        self.compare_root: Path | None = None
        self.strategies: dict[str, str] = {}
        self.checked: dict[str, bool] = {}
        self.tops: list[str] = []
        self.root_files: list[str] = []

        self.tasks: list[DownloadTask] = []
        self.results: list = []
        self.pending: list[dict] = []
        self.report: dict = {}
        self.error: str | None = None

        self._temp: Path | None = None
        self._thread: threading.Thread | None = None
        self._abort = threading.Event()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def busy(self) -> bool:
        return self.phase in (PHASE_SCAN, PHASE_DOWNLOAD, PHASE_APPLY)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "update": dict(self.update_info) if self.update_info else None,
                "strategies": self.strategy_rows(),
                "groups": group_changes(self.diff, self.strategies),
                "counts": counts_of(self.diff),
                "changeCounts": change_counts(self.diff),
                "tasks": len(self.tasks),
                "done": sum(1 for r in self.results if getattr(r, "ok", False)),
                "pending": list(self.pending),
                "report": dict(self.report) if self.report else None,
                "error": self.error,
                "changelog": bool(self.changelog),
            }

    def strategy_rows(self) -> list[dict]:
        rows = []
        for name in self.tops:
            rows.append({
                "name": name, "display": name, "isFile": False,
                "strategies": [s.value for s in _dir_strategies()],
                "current": self.strategies.get(name, ""),
                "checked": bool(self.checked.get(name, True)),
            })
        for name in self.root_files:
            rows.append({
                "name": name, "display": name, "isFile": True,
                "strategies": [s.value for s in _file_strategies()],
                "current": self.strategies.get(name, ""),
                "checked": bool(self.checked.get(name, True)),
            })
        return rows

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    def import_update(self, raw: str) -> dict:
        if self.busy():
            raise FlowError("更新流程进行中，暂时不能更换更新包")
        path = Path(raw).expanduser()
        if not path.is_file():
            raise FlowError(f"文件不存在：{path}")
        if path.suffix.lower() not in (".zip", ".mrpack", ".eapack"):
            raise FlowError("请选择 .zip / .mrpack / .eapack 文件")
        kind, err = detect_pack_kind(path)
        if kind == PackKind.UNKNOWN:
            raise FlowError(f"无法识别：{err}")

        self._discard_temp()
        with self._lock:
            self.diff = None
            self.compare_root = None
            self.strategies = {}
            self.checked = {}
            self.tops = []
            self.root_files = []
            self.tasks = []
            self.results = []
            self.pending = []
            self.report = {}
            self.error = None
            self.update_zip = path
            self.pack_kind = kind
            self.changelog = ""

        info = {"path": str(path), "name": path.name,
                "size": path.stat().st_size, "kind": kind.value}
        try:
            ok, reason = eapack_mod.verify_signature(path)
            info["signature"] = "ok" if ok and not reason else (reason or "unsigned")
        except Exception:                                   # noqa: BLE001
            info["signature"] = "unsigned"
        try:
            md = eapack_mod.read_changelog(path)
            self.changelog = md or ""
            info["changelog"] = bool(md)
        except Exception:                                   # noqa: BLE001
            self.changelog = ""
        with self._lock:
            self.update_info = info
        label = "更新包" if kind == PackKind.UPDATE else "导出包"
        self.bus.log(f"已导入{label}：{path.name}", "INFO")
        if info["signature"] not in ("ok", "unsigned"):
            self.bus.log(f"完整性校验未通过：{info['signature']}", "WARNING")
        return self.snapshot()

    def clear(self) -> dict:
        if self.busy():
            raise FlowError("更新流程进行中")
        self._discard_temp()
        with self._lock:
            self.update_zip = None
            self.pack_kind = None
            self.update_info = {}
            self.changelog = ""
            self.diff = None
            self.compare_root = None
            self.strategies = {}
            self.checked = {}
            self.tops = []
            self.root_files = []
            self.tasks = []
            self.results = []
            self.pending = []
            self.report = {}
            self.error = None
        return self.snapshot()

    # ------------------------------------------------------------------
    # 比对
    # ------------------------------------------------------------------
    def start_scan(self, strategies: dict | None = None,
                   checked: dict | None = None) -> dict:
        if self.busy():
            raise FlowError("正在处理中")
        if self.pack_root is None:
            raise FlowError("尚未选择整合包")
        if self.update_zip is None:
            raise FlowError("尚未选择更新包")
        if strategies:
            self.strategies.update(strategies)
        if checked:
            self.checked.update(checked)
        self.phase = PHASE_SCAN
        self.error = None
        self._abort.clear()
        self._thread = threading.Thread(target=self._scan_worker, daemon=True)
        self._thread.start()
        return {"started": True}

    def _scan_worker(self) -> None:
        try:
            zpath = self.update_zip
            root = self.pack_root
            assert zpath is not None and root is not None
            self.bus.log(f"开始比对：{zpath.name}", "INFO")
            self._temp = Path(tempfile.mkdtemp(prefix="pe-web-"))
            prefix, _standard, zip_tops, _names = _zip_layout(zpath)
            content_root = (self._temp / prefix) if prefix else self._temp
            self.bus.progress("scan", "正在读更新包…", 0.02)
            _extract_root_members(zpath, content_root, prefix)

            recorded = eapack_mod.read_hashes(zpath) or {}
            manifest = recorded.get("files") or {}
            folders = recorded.get("folders") or {}
            index_hashes = recorded.get("index") or {}

            def _prog(done: int, total: int, phase: str = "") -> None:
                total = max(1, total)
                self.bus.progress("scan", f"比对 {done}/{total}", done / total)

            self.bus.progress("scan", "正在解压更新包…", 0.05)
            count, bad = _extract_all_members(zpath, self._temp, manifest, prefix)

            try:
                content_root, _fb = locate_content_root(self._temp)
            except Exception:                               # noqa: BLE001
                pass
            self.compare_root = content_root
            if bad:
                self.bus.log(f"有 {len(bad)} 个文件与清单不一致（以解压内容为准）", "WARNING")

            whitelist = db.get_whitelist()
            threads = int(db.get_compare_options().get("hash_threads", 16) or 16)
            self.bus.progress("scan", "正在比对…", 0.55)
            diff = None
            if manifest:
                try:
                    diff = diff_from_manifest(
                        root, manifest, index_hashes=index_hashes,
                        folder_hashes=folders, whitelist=whitelist,
                        compare_root=content_root,
                        pack_tops=set(zip_tops),
                        threads=max(1, min(64, threads)), progress=_prog)
                except Exception as exc:                     # noqa: BLE001
                    self.bus.log(f"清单比对失败，改用逐文件比对：{exc}", "WARNING")
                    diff = None
            if diff is None:
                diff = diff_packs_parallel(
                    root, content_root, whitelist=whitelist,
                    folder_hashes=folders,
                    threads=max(1, min(64, threads)), progress=_prog)
            self.diff = diff
            self._build_tables(content_root)
            self.bus.progress("scan", "比对完成", 1.0)
            self.phase = PHASE_IDLE
            self.bus.log(
                f"比对完成：+{len(diff.added)} ~{len(diff.modified)} "
                f"-{len(diff.deleted)}", "INFO")
            self.controller.push_state()
        except Exception as exc:                            # noqa: BLE001
            self.phase = PHASE_IDLE
            self.error = str(exc)
            self.bus.log(f"比对失败：{exc}", "ERROR")
            self.controller.push_state()

    def _build_tables(self, content_root: Path) -> None:
        root = self.pack_root
        assert root is not None
        diff = self.diff
        assert diff is not None

        files: set[str] = set()
        for entry in content_root.iterdir() if content_root.is_dir() else []:
            if entry.is_file() and not is_reserved_root_name(entry.name):
                files.add(entry.name)
        folders: list[str] = []
        if content_root.is_dir():
            folders = sorted(e.name for e in content_root.iterdir()
                             if e.is_dir())
        self.root_files = sorted(files)
        self.tops = folders

        rec: dict = {}
        if self.update_zip is not None:
            try:
                rec = eapack_mod.read_recommended_strategies(self.update_zip) or {}
            except Exception:                               # noqa: BLE001
                rec = {}

        for name in folders:
            if self.strategies.get(name):
                continue
            self.strategies[name] = rec.get(name) or default_strategy_for_dir(name).value
        for name in self.root_files:
            if self.strategies.get(name):
                continue
            self.strategies[name] = (rec.get(name)
                                     or default_strategy_for_file(name).value)
        for name in folders + self.root_files:
            self.checked.setdefault(name, True)

    # ------------------------------------------------------------------
    # 任务收集
    # ------------------------------------------------------------------
    def _is_checked(self, top: str) -> bool:
        return _rule_is_checked(self.checked, top, default=True)

    def _strategy(self, top: str) -> str:
        return resolve_strategy(self.strategies, top)

    def _smart_excluded(self, rel: str) -> bool:
        if self.pack_kind != PackKind.UPDATE:
            return False
        return config_exclude.is_protected(rel)

    def collect_tasks(self) -> list[DownloadTask]:
        """按 diff + 策略算出"要下载哪些文件"。已在包内的不下载。"""
        diff = self.diff
        src = self.compare_root
        if diff is None or src is None:
            self.tasks = []
            return []

        merged: dict[str, dict] = {}
        if self.update_zip is not None:
            try:
                pack, _err = parse_mrpack(self.update_zip)
                for item in getattr(pack, "files", []) or []:
                    p = str(item.get("path", ""))
                    if p:
                        merged[p] = item
            except Exception:                               # noqa: BLE001
                merged = {}

        cache = self.controller.cache_root()
        tasks: list[DownloadTask] = []
        for ch in list(diff.added) + list(diff.modified):
            rel = ch.rel_path
            if not rel.parts:
                continue
            if will_skip(ch, self.checked, self.strategies,
                         checked_default=True):
                continue
            local = src / rel
            if local.is_file():                             # 包里自带 → 不用下载
                continue
            item = merged.get(rel.as_posix())
            urls = list((item or {}).get("downloads") or [])
            if not urls:
                continue
            tasks.append(DownloadTask(
                rel_path=rel.as_posix(),
                dest=cache / rel,
                urls=urls,
                size=int((item or {}).get("fileSize") or 0),
                hashes=dict((item or {}).get("hashes") or {})))
        self.tasks = tasks
        return tasks

    # ------------------------------------------------------------------
    # 下载 + 应用
    # ------------------------------------------------------------------
    def start_apply(self) -> dict:
        if self.busy():
            raise FlowError("正在处理中")
        if self.diff is None or self.compare_root is None:
            raise FlowError("请先比对")
        self.error = None
        self.report = {}
        self.phase = PHASE_DOWNLOAD if self.collect_tasks() else PHASE_APPLY
        self._thread = threading.Thread(target=self._apply_worker, daemon=True)
        self._thread.start()
        return {"started": True, "tasks": len(self.tasks)}

    def _apply_worker(self) -> None:
        try:
            tasks = self.tasks
            cache = self.controller.cache_root()
            if tasks:
                self.bus.progress("download", f"正在下载 {len(tasks)} 个文件…", 0.0)
                engine = self.controller.engine()
                results = engine.download_files(
                    tasks, target_dir=cache,
                    threads=self.controller.threads(),
                    progress=lambda done, total, name="": self.bus.progress(
                        "download", f"下载 {done}/{total}", done / max(1, total)),
                    log=lambda level, msg: self.bus.log(msg, level.upper()),
                    byte_progress=lambda rel, got, total: self.bus.emit(
                        "bytes", {"key": rel, "done": got, "total": total}),
                    should_abort=self._abort.is_set)
                self.results = results
                failed = [r for r in results if not getattr(r, "ok", True)]
                if failed:
                    self.pending = [
                        {"path": r.task.rel_path, "error": getattr(r, "error", "")}
                        for r in failed]
                    self.phase = PHASE_IDLE
                    self.bus.log(f"{len(failed)} 个文件下载失败", "ERROR")
                    self.controller.push_state()
                    return

            self.phase = PHASE_APPLY
            self.bus.progress("apply", "正在应用更新…", 0.0)
            src_layers = [SourceLayer(cache), SourceLayer(self.compare_root, consume=False)]
            source = PlanSource(src_layers)
            plan = build_plan(self.diff, self.checked, self.strategies,
                              old_root=self.pack_root, source=source)
            report = execute_plan(
                plan, self.pack_root, source=source,
                log=lambda level, msg: self.bus.log(msg, level.upper()),
                progress=lambda done, total: self.bus.progress(
                    "apply", f"应用 {done}/{total}", done / max(1, total)))
            failed = verify_after_update(plan, self.pack_root)
            self.report = {"applied": report.get("applied", 0),
                           "deleted": report.get("deleted", 0),
                           "failed": len(failed)}
            self.phase = PHASE_DONE
            self.bus.progress("apply", "更新完成", 1.0)
            self.bus.log(
                f"更新完成：新增 {self.report['applied']}，"
                f"删除 {self.report['deleted']}", "INFO")
            if failed:
                self.bus.log(f"有 {len(failed)} 项未通过复核", "WARNING")
            self.controller.refresh_pack()
        except Exception as exc:                            # noqa: BLE001
            self.phase = PHASE_IDLE
            self.error = str(exc)
            self.bus.log(f"更新失败：{exc}", "ERROR")
        finally:
            self.controller.push_state()

    # ------------------------------------------------------------------
    # 策略（前端改一格就回推一份新快照）
    # ------------------------------------------------------------------
    def set_strategy(self, name: str, value: str = "",
                     checked: bool | None = None) -> dict:
        if not name:
            raise FlowError("缺少条目名")
        with self._lock:
            if value:
                allowed = {s.value for s in _file_strategies()} if name in self.root_files \
                    else {s.value for s in _dir_strategies()}
                if value not in allowed:
                    raise FlowError(f"不支持的策略：{value}")
                self.strategies[name] = value
            if checked is not None:
                self.checked[name] = bool(checked)
        self.controller.push_state()
        return self.snapshot()

    def set_all(self, value: str = "", checked: bool | None = None,
                names: list[str] | None = None) -> dict:
        targets = list(names) if names else (self.tops + self.root_files)
        for name in targets:
            try:
                self.set_strategy(name, value or "", checked)
            except FlowError:
                continue
        return self.snapshot()

    def retry(self, rel_path: str) -> dict:
        """重下单个失败文件（补入窗口用）。"""
        if self.busy():
            raise FlowError("正在处理中")
        task = next((t for t in self.tasks if t.rel_path == rel_path), None)
        if task is None:
            raise FlowError("没有这个文件的下载任务")
        cache = self.controller.cache_root()
        engine = self.controller.engine()
        results = engine.download_files(
            [task], target_dir=cache, threads=1,
            log=lambda level, msg: self.bus.log(msg, level.upper()),
            byte_progress=lambda rel, got, total: self.bus.emit(
                "bytes", {"key": rel, "done": got, "total": total}),
            should_abort=lambda: False)
        ok = bool(results) and getattr(results[0], "ok", False)
        if ok:
            self.pending = [p for p in self.pending if p.get("path") != rel_path]
            self.bus.log(f"已补入 {rel_path}", "SUCCESS")
        else:
            self.bus.log(f"{rel_path} 仍然失败", "ERROR")
        self.controller.push_state()
        return {"ok": ok, "pending": list(self.pending)}

    def abort(self) -> dict:
        self._abort.set()
        self.bus.log("已请求中止", "WARNING")
        return {"ok": True}

    # ------------------------------------------------------------------
    def _discard_temp(self) -> None:
        if self._temp is not None:
            shutil.rmtree(self._temp, ignore_errors=True)
            self._temp = None


# ----------------------------------------------------------------------
def counts_of(diff: DiffResult | None) -> dict:
    if diff is None:
        return {"added": 0, "modified": 0, "deleted": 0}
    return {"added": len(diff.added), "modified": len(diff.modified),
            "deleted": len(diff.deleted)}


def change_counts(diff: DiffResult | None) -> dict:
    """每个顶层项有多少处差异（折叠「无需更新」用）。"""
    out: dict[str, int] = {}
    if diff is None:
        return out
    for ch in list(diff.added) + list(diff.modified) + list(diff.deleted):
        parts = getattr(ch, "rel_path", None)
        if not parts or not parts.parts:
            continue
        top = parts.parts[0]
        out[top] = out.get(top, 0) + 1
    return out


def group_changes(diff: DiffResult | None, strategies: dict) -> list[dict]:
    """按顶层目录分组的变更列表（右栏树）。"""
    if diff is None:
        return []
    buckets: dict[str, dict] = {}

    def bucket(top: str) -> dict:
        if top not in buckets:
            buckets[top] = {"top": top, "items": [],
                            "counts": {"added": 0, "modified": 0, "deleted": 0},
                            "skipped": 0, "smartExcluded": 0}
        return buckets[top]

    for kind, items in (("added", diff.added), ("modified", diff.modified),
                        ("deleted", diff.deleted)):
        for ch in items:
            rel = getattr(ch, "rel_path", None)
            if not rel or not rel.parts:
                continue
            top = rel.parts[0]
            b = bucket(top)
            smart = strategies.get(top) == Strategy.SMART_EXCLUDE.value
            b["items"].append({"path": rel.as_posix(), "kind": kind,
                               "smartExcluded": smart})
            b["counts"][kind] += 1
            if smart:
                b["smartExcluded"] += 1
    for b in buckets.values():
        b["items"].sort(key=lambda i: i["path"])
    return [buckets[k] for k in sorted(buckets)]


def _dir_strategies() -> list[Strategy]:
    from ..config import STRATEGY_LIST
    return list(STRATEGY_LIST)


def _file_strategies() -> list[Strategy]:
    from ..config import FILE_STRATEGY_LIST
    return list(FILE_STRATEGY_LIST)
