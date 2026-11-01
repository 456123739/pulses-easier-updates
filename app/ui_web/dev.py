"""
dev.py — 开发者流程（导入整合包 ZIP → 选项 → 导出 .eapack）
------------------------------------------------
与 flow.py 同构：状态只在这里，UI 读快照、事件只走 EventBus。
导出本身是纯计算 + 大量 IO，全程后台线程，进度按"文件数"回报。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from ..core import database as db
from ..core import eapack as eapack_mod
from ..core import export_rules as rules_mod
from ..core import instance_export as inst_export
from ..core import instance_scan
from ..config import NEVER_PACK_FILES
from ..core.mrpack import (MRPack, is_reserved_root_name, merge_files,
                           parse_mrpack)

EXPORT_KEYS = ("include_hashes", "tamper_proof", "precompute_overrides",
               "min_size")


class DevError(RuntimeError):
    code = "DEV_ERROR"


class DevFlow:
    def __init__(self, controller, bus):
        self.controller = controller
        self.bus = bus
        self._lock = threading.RLock()

        self.phase = "idle"           # idle | exporting | done | error
        self.mode = "zip"             # zip（导入启动器整合包）| instance（本地实例）
        self.scan: dict | None = None
        self.rule_keys: set[str] = rules_mod.default_keys()
        self.cf_key = ""
        try:
            self.cf_key = str(db.load_app_config().get("cf_api_key") or "")
        except Exception:                                  # noqa: BLE001
            self.cf_key = ""
        self.source_zip: Path | None = None
        self.pack: MRPack | None = None
        self.merged: list[dict] = []
        self.tops: list[str] = []
        self.root_files: list[str] = []
        self.strategies: dict[str, str] = {}
        self.excluded: set[str] = set()
        self.changelog = ""
        self.version = ""
        self.options: dict = {}
        self.out_path: str = ""
        self.error: str | None = None
        self.progress: dict = {}
        self._temp: Path | None = None
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None

        try:
            self.options = {k: bool(db.get_export_options().get(k))
                            for k in EXPORT_KEYS}
        except Exception:                                  # noqa: BLE001
            self.options = {k: True for k in EXPORT_KEYS}
        self.options["min_size"] = False

    # ------------------------------------------------------------------
    def busy(self) -> bool:
        return self.phase == "exporting"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "mode": self.mode,
                "instance": self._scan_summary(),
                "ruleOptions": [{"key": g.key, "title": g.title,
                                 "checked": g.key in self.rule_keys,
                                 "children": [{"key": c.key, "title": c.title,
                                               "checked": c.key in self.rule_keys}
                                              for c in g.children]}
                                for g in rules_mod.DEFAULT_OPTIONS],
                "hasCfKey": bool(self.cf_key),
                "source": ({"path": str(self.source_zip),
                            "name": self.source_zip.name}
                           if self.source_zip else None),
                "name": getattr(self.pack, "name", "") or "",
                "version": self.version,
                "files": len(self.merged),
                "strategies": [{"name": n, "current": self.strategies.get(n, ""),
                                "excluded": n in self.excluded,
                                "isFile": n in self.root_files}
                               for n in self.tops + self.root_files],
                "changelog": self.changelog,
                "options": dict(self.options),
                "outPath": self.out_path,
                "error": self.error,
                "progress": dict(self.progress),
            }

    # ------------------------------------------------------------------
    def import_zip(self, raw: str) -> dict:
        if self.busy():
            raise DevError("正在导出，暂时不能更换整合包")
        path = Path(raw).expanduser()
        if not path.is_file():
            raise DevError(f"文件不存在：{path}")
        if path.suffix.lower() not in (".zip", ".mrpack"):
            raise DevError("请选择启动器导出的整合包（.zip / .mrpack）")

        pack, err = parse_mrpack(path)
        if pack is None:
            raise DevError(f"不是有效的整合包：{err}")
        merged = merge_files(pack)
        paths = [str(m.get("path", "")) for m in merged if m.get("path")]
        tops = sorted({p.split("/", 1)[0] for p in paths if "/" in p})
        files = sorted({p for p in paths
                        if "/" not in p and not is_reserved_root_name(p)})

        with self._lock:
            self.mode = "zip"
            self.scan = None
            self.source_zip = path
            self.pack = pack
            self.merged = merged
            self.tops = tops
            self.root_files = files
            self.version = str(getattr(pack, "version_id", "") or "")
            self.strategies = {n: "完全匹配" for n in tops + files}
            self.excluded = set()
            self.phase = "idle"
            self.error = None
            self.progress = {}
            self.out_path = ""
        self.bus.log(f"已导入整合包：{path.name}（{len(merged)} 个文件）", "SUCCESS")
        self.controller.push_state()
        return self.snapshot()

    # ------------------------------------------------------------------
    def _scan_summary(self) -> dict | None:
        if not self.scan:
            return None
        return {
            "root": self.scan.get("root", ""),
            "name": Path(self.scan.get("root", "")).name,
            "minecraft": self.scan.get("minecraft", ""),
            "loader": self.scan.get("loader", ""),
            "loaderVersion": self.scan.get("loaderVersion", ""),
            "totals": self.scan.get("totals", {}),
            "unsafeLinks": list(self.scan.get("unsafeLinks") or []),
            "badNames": list(self.scan.get("badNames") or []),
            "rows": [r for r in self.scan.get("rows", []) if r["files"]],
        }

    def _current_rules(self) -> list[str]:
        # 目录用 `!dir\`（展开成 dir\*），顶层文件必须写裸名 `!file`，
        # 否则 `!file\*` 永远匹配不到文件本身。
        root = Path(self.scan["root"]) if self.scan else None

        def _is_file(name: str) -> bool:
            if name in self.root_files:
                return True
            if root is not None:
                try:
                    return (root / name).is_file()
                except OSError:
                    pass
            return False

        extra = tuple((f"!{name}" if _is_file(name) else f"!{name}\\")
                      for name in sorted(self.excluded))
        return rules_mod.expand_rules(set(self.rule_keys), extra)

    def _rescan(self) -> None:
        if not self.scan:
            return
        self.scan = instance_scan.scan_instance(self.scan["root"],
                                                self._current_rules())
        self._sync_scope()

    def _sync_scope(self) -> None:
        """规则变化后刷新顶层清单：新增的补默认策略，消失的移除。"""
        if not self.scan:
            return
        rows = [r for r in self.scan.get("rows", []) if r["files"]]
        tops = [r["name"] for r in rows if not r["isFile"]]
        # 永远不进包的文件（options.txt 等）不该出现在"更新范围"里
        files = [r["name"] for r in rows
                 if r["isFile"] and r["name"] not in NEVER_PACK_FILES]
        with self._lock:
            for name in tops + files:
                self.strategies.setdefault(name, "完全匹配")
            # 被排除的条目在扫描结果里本来就看不到，不能被"清理"掉
            kept_dirs = list(tops)
            kept_files = list(files)
            for name in sorted(self.excluded):
                if name in kept_dirs or name in kept_files:
                    continue
                if name in self.root_files:      # 之前识别为根散文件
                    kept_files.append(name)
                else:
                    kept_dirs.append(name)
                self.strategies.setdefault(name, "完全匹配")
            alive = set(tops) | set(files) | set(self.excluded)
            for name in list(self.strategies):
                if name not in alive:
                    self.strategies.pop(name, None)
            self.tops = kept_dirs
            self.root_files = kept_files

    def import_instance(self, raw: str) -> dict:
        """从本地整合包目录直接制作更新包（不需要启动器导出的 zip）。"""
        if self.busy():
            raise DevError("正在导出，暂时不能更换整合包")
        raw = str(raw or "").strip()
        if not raw:
            raise DevError("请选择整合包目录")
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise DevError(f"目录不存在：{path}")
        root = instance_scan.pack_locator.locate_modpack(path)
        if not root:
            raise DevError("这个目录里没有找到整合包（需要 mods/ 与 config/）")
        scan = instance_scan.scan_instance(root, self._current_rules())
        if not scan["totals"]["files"]:
            raise DevError("导出范围内没有文件（检查导出选项）")

        tops = [r["name"] for r in scan["rows"]
                if not r["isFile"] and r["files"]]
        files = [r["name"] for r in scan["rows"]
                 if r["isFile"] and r["files"]
                 and r["name"] not in NEVER_PACK_FILES]
        with self._lock:
            self.mode = "instance"
            self.scan = scan
            self.source_zip = None
            self.pack = None
            self.merged = []
            self.tops = tops
            self.root_files = files
            self.strategies = {n: "完全匹配" for n in tops + files}
            self.excluded = set()
            self.phase = "idle"
            self.error = None
            self.progress = {}
            self.out_path = ""
            if not self.version:
                mc = scan.get("minecraft") or ""
                self.version = f"{mc}-1.0.0" if mc else "1.0.0"
        if scan.get("unsafeLinks"):
            self.bus.log("已跳过指向实例外的链接：" +
                         "、".join(scan["unsafeLinks"][:5]), "WARN")
        if scan.get("badNames"):
            self.bus.log("有文件名不是 UTF-8，导出会失败：" +
                         "、".join(scan["badNames"][:5]), "WARN")
        self.bus.log(f"已载入实例：{Path(root).name}"
                     f"（{scan['totals']['files']} 个文件，"
                     f"候选 {scan['totals']['candidates']}）", "SUCCESS")
        self.controller.push_state()
        return self.snapshot()

    def set_rule(self, key: str, value: bool) -> dict:
        """勾选/取消一个「导出内容」选项，并立即重扫。"""
        if self.busy():
            raise DevError("正在导出")
        known = {g.key for g in rules_mod.DEFAULT_OPTIONS}
        for g in rules_mod.DEFAULT_OPTIONS:
            known.update(c.key for c in g.children)
        if key not in known:
            raise DevError(f"未知选项：{key}")
        if value:
            self.rule_keys.add(key)
        else:
            self.rule_keys.discard(key)
        self._rescan()
        self.controller.push_state()
        return self.snapshot()

    def set_cf_key(self, key: str) -> dict:
        self.cf_key = str(key or "").strip()
        try:
            cfg = db.load_app_config()
            cfg["cf_api_key"] = self.cf_key
            db.save_app_config(cfg)
        except Exception:                                  # noqa: BLE001
            pass
        return self.snapshot()

    def clear(self) -> dict:
        if self.busy():
            raise DevError("正在导出")
        self._cleanup_temp()
        with self._lock:
            self.mode = "zip"
            self.scan = None
            self.source_zip = None
            self.pack = None
            self.merged = []
            self.tops = []
            self.root_files = []
            self.strategies = {}
            self.excluded = set()
            self.phase = "idle"
            self.error = None
            self.progress = {}
            self.out_path = ""
        self.controller.push_state()
        return self.snapshot()

    # ------------------------------------------------------------------
    def set_option(self, key: str, value: bool) -> dict:
        if self.busy():
            raise DevError("正在导出，暂时不能修改导出选项")
        if key not in EXPORT_KEYS:
            raise DevError(f"未知选项：{key}")
        with self._lock:
            self.options[key] = bool(value)
        try:
            db.set_export_options({**db.get_export_options(), key: bool(value)})
        except Exception:                                  # noqa: BLE001, S110
            pass
        self.controller.push_state()
        return self.snapshot()

    def set_changelog(self, text: str) -> dict:
        if self.busy():
            raise DevError("正在导出，暂时不能修改更新日志")
        with self._lock:
            self.changelog = str(text or "")
        return self.snapshot()

    def set_strategy(self, name: str, value: str = "", excluded: bool | None = None) -> dict:
        if self.busy():
            raise DevError("正在导出，暂时不能修改更新范围")
        if name not in self.strategies and name not in self.excluded:
            raise DevError(f"没有这个条目：{name}")
        before = set(self.excluded)
        with self._lock:
            if value:
                self.strategies[name] = value
            if excluded is not None:
                if excluded:
                    self.excluded.add(name)
                else:
                    self.excluded.discard(name)
        if self.mode == "instance" and set(self.excluded) != before:
            self._rescan()
        self.controller.push_state()
        return self.snapshot()

    # ------------------------------------------------------------------
    def start_export(self, out_path: str) -> dict:
        out = Path(out_path).expanduser()
        if out.suffix.lower() != ".eapack":
            out = out.with_suffix(".eapack")
        out.parent.mkdir(parents=True, exist_ok=True)
        total = (self.scan or {}).get("totals", {}).get("files", 0) \
            if self.mode == "instance" else len(self.merged)
        with self._lock:
            if self.phase == "exporting":
                raise DevError("正在导出")
            if self.mode == "instance":
                if not self.scan:
                    raise DevError("请先选择整合包目录")
            elif self.pack is None or self.source_zip is None:
                raise DevError("请先导入整合包")
            self.phase = "exporting"
            self._cancel = threading.Event()
            self.error = None
            self.progress = {"done": 0, "total": total, "label": "准备中…"}
            self.out_path = str(out)
        target = self._worker_instance if self.mode == "instance" else self._worker
        self._thread = threading.Thread(target=target, args=(out,), daemon=True)
        self._thread.start()
        self.controller.push_state()        # 立刻让界面进入"导出中"
        return {"started": True, "out": str(out)}

    def abort(self) -> dict:
        """取消正在进行的导出（下一个文件边界生效）。"""
        with self._lock:
            running = self.phase == "exporting"
            ev = self._cancel
        if running and ev is not None:
            ev.set()
            self.bus.log("正在取消导出…", "WARN")
        return self.snapshot()

    def _worker_instance(self, out: Path) -> None:
        try:
            scan = self.scan or {}
            root = scan.get("root", "")
            if not root:
                raise DevError("没有可导出的实例")
            strategies = {k: v for k, v in self.strategies.items()
                          if k not in self.excluded}
            res = inst_export.export_from_instance(
                root, out,
                rules=self._current_rules(),
                strategies=strategies,
                whitelist=db.get_whitelist(),
                changelog_md=self.changelog,
                pack_name=Path(root).name,
                pack_version=self.version or "1.0.0",
                opts=dict(self.options),
                excluded_tops=sorted(self.excluded),
                cf_key=self.cf_key,
                cancel=self._cancel,
                progress=self._progress,
                log=lambda level, msg: self.bus.log(msg, level.upper()))
            if res is None:
                if self._cancel is not None and self._cancel.is_set():
                    with self._lock:
                        self.phase = "idle"
                        self.progress = {}
                    self.bus.log("已取消导出", "WARN")
                    return
                raise DevError("导出失败（详见日志）")
            size = out.stat().st_size if out.exists() else 0
            total = scan.get("totals", {}).get("files", 0)
            with self._lock:
                self.phase = "done"
                self.progress = {"done": total, "total": total, "label": "完成"}
                self.out_path = str(out)
            self.bus.log(f"更新包已生成：{out.name}（{size:,} B）", "SUCCESS")
        except Exception as exc:                           # noqa: BLE001
            msg = str(exc)
            notes = getattr(exc, "__notes__", None)
            if notes:
                msg = f"{msg}（{'；'.join(str(n) for n in notes)}）"
            with self._lock:
                self.phase = "error"
                self.error = msg
            self.bus.log(f"导出失败：{msg}", "ERROR")
        finally:
            self.controller.push_state()

    def _worker(self, out: Path) -> None:
        try:
            assert self.source_zip is not None and self.pack is not None
            self._cleanup_temp()
            self._temp = Path(tempfile.mkdtemp(prefix="pe-dev-"))
            with zipfile.ZipFile(self.source_zip) as zf:
                zf.extractall(self._temp)

            res = eapack_mod.export_eapack(
                source_dir=self._temp,
                source_zip=self.source_zip,
                out_path=out,
                pack=self.pack,
                merged_files=self.merged,
                strategies={k: v for k, v in self.strategies.items()
                            if k not in self.excluded},
                changelog_md=self.changelog,
                version=self.version,
                excluded_folders=sorted(self.excluded),
                log=lambda level, msg: self.bus.log(msg, level.upper()),
                progress=self._progress)
            if res is None:
                if self._cancel is not None and self._cancel.is_set():
                    with self._lock:
                        self.phase = "idle"
                        self.progress = {}
                    self.bus.log("已取消导出", "WARN")
                    return
                raise DevError("导出失败（详见日志）")
            size = out.stat().st_size if out.exists() else 0
            with self._lock:
                self.phase = "done"
                self.progress = {"done": len(self.merged),
                                 "total": len(self.merged), "label": "完成"}
                self.out_path = str(out)
            self.bus.log(f"更新包已生成：{out.name}（{size:,} B）", "SUCCESS")
        except Exception as exc:                           # noqa: BLE001
            msg = str(exc)
            notes = getattr(exc, "__notes__", None)
            if notes:
                msg = f"{msg}（{'；'.join(str(n) for n in notes)}）"
            with self._lock:
                self.phase = "error"
                self.error = msg
            self.bus.log(f"导出失败：{msg}", "ERROR")
        finally:
            self._cleanup_temp()
            self.controller.push_state()

    def _progress(self, done: int, total: int, label: str) -> None:
        with self._lock:
            self.progress = {"done": int(done), "total": int(total),
                             "label": str(label)}
        self.bus.progress("export", label or "导出中…",
                          (done / total) if total else 0.0)

    def _cleanup_temp(self) -> None:
        if self._temp is not None:
            shutil.rmtree(self._temp, ignore_errors=True)
            self._temp = None
