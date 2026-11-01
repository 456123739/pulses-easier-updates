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

import json
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from ..config import (Strategy, default_strategy_for_dir,
                      default_strategy_for_file)
from ..core import config_exclude
from ..core import recover as recover_mod
from ..core import database as db
from ..core import eapack as eapack_mod
from ..core.apply_rules import is_checked as _rule_is_checked
from ..core.apply_rules import resolve_strategy, will_skip
from ..core.differ import DiffResult, diff_from_manifest, diff_packs_parallel
from ..core.downloader import DownloadTask
from ..core.mrpack import (is_reserved_root_name, locate_content_root,
                           merge_files, parse_mrpack)
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


def _sha1_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _merge_index_changes(diff, root: Path, content_root: Path,
                         index_hashes: dict) -> None:
    """把只在 index 里声明的文件补进 diff（added/modified，永不删）。"""
    from ..config import ChangeKind
    from ..core.differ import Change

    known = {c.rel_path.as_posix() for c in diff.added}
    known |= {c.rel_path.as_posix() for c in diff.modified}
    for rel, hashes in (index_hashes or {}).items():
        rel_s = str(rel).replace("\\", "/").strip("/")
        if not rel_s or rel_s in known:
            continue
        try:
            if (content_root / rel_s).exists():
                continue                 # 包里有同路径文件，归普通比对管
        except OSError:
            continue
        target = ""
        algo = ""
        for key in ("sha1", "sha256", "sha512"):
            value = (hashes or {}).get(key)
            if value:
                target = str(value).lower()
                algo = key
                break
        local = root / rel_s
        try:
            if not local.is_file():
                diff.added.append(Change(Path(rel_s), ChangeKind.ADDED))
                known.add(rel_s)
                continue
            if algo == "sha1" and target and _sha1_file(local) != target:
                diff.modified.append(Change(Path(rel_s), ChangeKind.MODIFIED))
                known.add(rel_s)
        except OSError:
            continue


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
        self._resume_pending = False
        self._done_paths: set[str] = set()

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
                "groups": group_changes(self.diff, self.strategies,
                                        self.checked),
                "counts": counts_of(self.diff),
                "changeCounts": change_counts(self.diff),
                "tasks": len(self.tasks),
                "done": sum(1 for r in self.results if getattr(r, "ok", False)),
                "pending": list(self.pending),
                "report": dict(self.report) if self.report else None,
                "error": self.error,
                "changelog": bool(self.changelog),
                "resume": self.resume_info(),
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
            # 上次中断留下的 .xxx.pulses_tmp 会被当成"玩家文件"算进差异里，先收拾
            try:
                if recover_mod.has_leftovers(root):
                    stats = recover_mod.recover_leftovers(
                        root, log=lambda level, msg: self.bus.log(msg, level.upper()))
                    self.bus.log(recover_mod.summary(stats), "WARNING")
            except Exception as exc:                       # noqa: BLE001
                self.bus.log(f"清理上次残留失败：{exc}", "WARNING")
            self._temp = Path(tempfile.mkdtemp(prefix="pe-web-"))
            prefix, _standard, zip_tops, _names = _zip_layout(zpath)
            content_root = (self._temp / prefix) if prefix else self._temp
            self.bus.progress("scan", "正在读更新包…", 0.02)
            _extract_root_members(zpath, content_root, prefix)

            recorded = eapack_mod.read_hashes(zpath) or {}
            manifest = recorded.get("files") or {}
            folders = recorded.get("folders") or {}
            index_hashes = dict(recorded.get("index") or {})
            # "要下载的文件"（index 声明、包内没有）不在 ea_hashes 里，
            # 只能从更新包的 modrinth.index.json 拿。少了这一步，它们既不会
            # 出现在变更列表里、也永远不会被下载（Tk 版一直是这么补的）。
            index_tops: set[str] = set()
            index_root_files: set[str] = set()
            try:
                up_pack, _up_err = parse_mrpack(zpath)
                for item in getattr(up_pack, "files", []) or []:
                    rel = str(getattr(item, "path", "") or "")
                    if not rel:
                        continue
                    if "/" in rel:
                        index_tops.add(rel.split("/", 1)[0])
                    else:
                        index_root_files.add(rel)
                    if rel in index_hashes:
                        continue
                    hashes = {k: getattr(item, k, "")
                              for k in ("sha1", "sha256", "sha512")}
                    hashes = {k: v for k, v in hashes.items() if v}
                    if hashes:
                        index_hashes[rel] = hashes
            except Exception:                               # noqa: BLE001
                index_tops = set()
                index_root_files = set()
            self._index_tops = set(index_tops)
            self._index_root_files = set(index_root_files)

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
            if index_tops:
                # index 文件所在的目录必须逐文件比，否则会退化成"整目录级"，
                # 那些要下载的文件就沉进目录级变更里了
                whitelist = sorted(set(whitelist) | index_tops)
            threads = int(db.get_compare_options().get("hash_threads", 16) or 16)
            self.bus.progress("scan", "正在比对…", 0.55)
            diff = None
            # 只有 index 声明、包里没有任何同名文件的更新包（模组全部走直链）
            # 也必须走清单比对，否则那些文件既不可见也不会被下载。
            if manifest or index_hashes:
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
                # 兜底比对只看磁盘上解压出来的内容，index 声明的文件它不知道；
                # 这里把"包内没有、只在 index 里"的文件补进去（不判删除）。
                _merge_index_changes(diff, root, content_root, index_hashes)
            self.diff = diff
            self._build_tables(content_root)
            self.bus.progress("scan", "比对完成", 1.0)
            self.phase = PHASE_IDLE
            self.bus.log(
                f"比对完成：+{len(diff.added)} ~{len(diff.modified)} "
                f"-{len(diff.deleted)}", "INFO")
            self.controller.push_state()
            if self._resume_pending:
                self._resume_pending = False
                self.start_apply()
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
        # index 声明、包内没有的目录/根文件也要出现在表里，
        # 否则"只在 index 里"的 mods 既看不见也不会被下载。
        files |= set(getattr(self, "_index_root_files", ()))
        folders = sorted(set(folders) | set(getattr(self, "_index_tops", ())))
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
                if pack is not None:
                    # parse_mrpack 给的是 MRFile 对象，下载链接/大小只在
                    # merge_files 的 dict 里；以前这里对对象调 .get()，
                    # 异常被吞掉 → merged 永远为空 → 一个文件都不会下载。
                    for item in merge_files(pack):
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
            if rel.as_posix() in self._done_paths and (cache / rel).is_file():
                continue                                    # 上次已经下好了
            local = src / rel
            if local.is_file():                             # 包里自带 → 不用下载
                continue
            item = merged.get(rel.as_posix()) or {}
            urls = list(item.get("downloads") or [])
            if not urls:
                continue
            # DownloadTask 的字段是 sha1/sha256/sha512 + file_size：
            # 以前这里按 dest/size/hashes 构造，一行就 TypeError，
            # 于是"要下载的文件"直接让开始更新报错。
            tasks.append(DownloadTask(
                rel_path=rel.as_posix(),
                urls=urls,
                sha1=str(item.get("sha1") or ""),
                sha256=str(item.get("sha256") or ""),
                sha512=str(item.get("sha512") or ""),
                file_size=int(item.get("file_size") or 0)))
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
        if not self._done_paths:
            self._done_paths = set()
        self.phase = PHASE_DOWNLOAD if self.collect_tasks() else PHASE_APPLY
        self._write_resume()
        # 先把"进入下载/应用"推给界面：主按钮靠这个变成「取消下载」。
        # 少了这次推送，玩家在下载中根本点不到取消（界面还停在"确认更新"）。
        self.controller.push_state()
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
                    on_file_done=self._note_done,
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
            self.controller.push_state()
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
            self._done_paths.clear()
            self._clear_resume()
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
    # 续传
    # ------------------------------------------------------------------
    def _resume_file(self) -> Path:
        return self.controller.cache_root() / "resume.json"

    def resume_info(self) -> dict | None:
        try:
            path = self._resume_file()
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not Path(data.get("update", "")).is_file():
                return None
            if not Path(data.get("pack", "")).is_dir():
                return None
            return {"update": Path(data["update"]).name,
                    "completed": len(data.get("completed") or []),
                    "total": int(data.get("total", 0) or 0)}
        except Exception:                                  # noqa: BLE001
            return None

    def _write_resume(self) -> None:
        try:
            payload = {
                "update": str(self.update_zip or ""),
                "pack": str(self.pack_root or ""),
                "strategies": dict(self.strategies),
                "checked": dict(self.checked),
                "completed": sorted(self._done_paths),
                "total": len(self.tasks),
            }
            self._resume_file().write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:                           # noqa: BLE001
            self.bus.log(f"续传记录写入失败：{exc}", "WARNING")

    def _clear_resume(self) -> None:
        try:
            self._resume_file().unlink(missing_ok=True)
        except Exception:                                  # noqa: BLE001, S110
            pass

    def resume(self) -> dict:
        """继续上次没做完的更新：恢复策略/勾选，重新比对后自动应用。"""
        if self.busy():
            raise FlowError("正在处理中")
        info = self.resume_info()
        if info is None:
            raise FlowError("没有可继续的更新")
        try:
            data = json.loads(self._resume_file().read_text(encoding="utf-8"))
        except Exception as exc:                           # noqa: BLE001
            raise FlowError(f"续传记录损坏：{exc}") from exc
        self.pack_root = Path(data.get("pack") or "")
        self.import_update(data.get("update") or "")
        self.strategies.update(data.get("strategies") or {})
        self.checked.update(data.get("checked") or {})
        self._done_paths = set(data.get("completed") or [])
        self._resume_pending = True
        self.bus.log("继续上次未完成的更新", "INFO")
        self.start_scan()
        return {"started": True}

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

    def _note_done(self, task) -> None:
        rel = getattr(task, "rel_path", "")
        if rel:
            self._done_paths.add(rel)
            self._write_resume()

    def give_up(self) -> dict:
        """放弃补入：清掉待补列表，让流程可以继续（缺失文件会记进日志）。"""
        if not self.pending:
            return {"ok": True, "pending": []}
        missed = [p.get("path", "") for p in self.pending]
        self.pending = []
        self.bus.log(f"已放弃 {len(missed)} 个未能下载的文件："
                     + "、".join(missed[:3]), "WARNING")
        self.controller.push_state()
        return {"ok": True, "pending": []}

    # ------------------------------------------------------------------
    # 策略配置的导入 / 导出（给"这套策略存下来"用）
    # ------------------------------------------------------------------
    def export_config(self, raw: str) -> dict:
        path = Path(raw).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1,
                   "strategies": dict(self.strategies),
                   "checked": dict(self.checked)}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        self.bus.log(f"策略配置已导出：{path.name}", "SUCCESS")
        return {"path": str(path)}

    def import_config(self, raw: str) -> dict:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise FlowError(f"文件不存在：{path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                           # noqa: BLE001
            raise FlowError(f"不是有效的策略配置：{exc}") from exc
        strategies = data.get("strategies") or {}
        checked = data.get("checked") or {}
        if not isinstance(strategies, dict):
            raise FlowError("配置文件里的 strategies 不合法")
        known = set(self.tops) | set(self.root_files)
        applied = 0
        with self._lock:
            for name, value in strategies.items():
                if name in known and isinstance(value, str):
                    self.strategies[name] = value
                    applied += 1
            for name, value in checked.items():
                if name in known:
                    self.checked[name] = bool(value)
        self.bus.log(f"已导入策略配置（{applied} 项）", "SUCCESS")
        self.controller.push_state()
        return self.snapshot()

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


def group_changes(diff: DiffResult | None, strategies: dict,
                  checked: dict | None = None) -> list[dict]:
    """按顶层目录分组的变更列表（右栏树）。

    `skipped` 必须真的按当前策略算出来：否则玩家把某个目录改成「跳过重名」
    之后，右栏看上去毫无变化（界面说会更新，实际不更新）。
    判定统一走 apply_rules.will_skip —— 与实际执行是同一份规则。
    """
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
            skip = (will_skip(ch, checked, strategies, checked_default=True)
                    if checked is not None else False)
            b["items"].append({"path": rel.as_posix(), "kind": kind,
                               "smartExcluded": smart, "skipped": skip})
            b["counts"][kind] += 1
            if smart:
                b["smartExcluded"] += 1
            if skip:
                b["skipped"] += 1
    for b in buckets.values():
        b["items"].sort(key=lambda i: i["path"])
    return [buckets[k] for k in sorted(buckets)]


def _dir_strategies() -> list[Strategy]:
    from ..config import STRATEGY_LIST
    return list(STRATEGY_LIST)


def _file_strategies() -> list[Strategy]:
    from ..config import FILE_STRATEGY_LIST
    return list(FILE_STRATEGY_LIST)
