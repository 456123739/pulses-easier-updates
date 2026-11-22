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
from ..core import export_specials
from ..core import instance_export as inst_export
from ..core import instance_scan
from ..core import cf_key
from ..core import pack_source
from ..config import NEVER_PACK_FILES, STRATEGY_LIST
from ..core.mrpack import (MRPack, is_reserved_root_name, merge_files,
                           parse_mrpack)

EXPORT_KEYS = ("include_hashes", "tamper_proof", "precompute_overrides",
               "min_size", "compact_container", "world_transform")


class DevError(RuntimeError):
    code = "DEV_ERROR"

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class DevFlow:
    def __init__(self, controller, bus):
        self.controller = controller
        self.bus = bus
        self._lock = threading.RLock()

        self.phase = "idle"           # idle | loading | exporting | done | error
        self.mode = "zip"             # zip（导入启动器整合包）| instance（本地实例）
        self.pending: dict | None = None   # loading 时的目标（kind / path / name / note）
        self._open_seq = 0                 # 连续拖入时只让最后一次生效
        self.scan: dict | None = None
        # 特殊选项（本机数据）默认一律不导出：从默认勾选里剔掉
        self.rule_keys: set[str] = rules_mod.default_keys() - export_specials.keys()
        self.specials: list[dict] = []
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
        # 动态子项：key = "saves/World" / "resourcepacks/foo.zip"
        #   {folder, name, kind: "dir"|"file", files, bytes, checked, strategy}
        # 没勾选的子项不进规则；strategy 为空表示跟随父目录。
        self.subs: dict[str, dict] = {}
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
        return self.phase in ("loading", "exporting")

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "mode": self.mode,
                "pending": dict(self.pending) if self.pending else None,
                "instance": self._scan_summary(),
                "ruleOptions": [{"key": g.key, "title": g.title,
                                 "checked": g.key in self.rule_keys,
                                 "children": [{"key": c.key, "title": c.title,
                                               "checked": c.key in self.rule_keys}
                                              for c in g.children]}
                                for g in rules_mod.DEFAULT_OPTIONS],
                "specials": [dict(item, checked=item["key"] in self.rule_keys)
                             for item in self.specials],
                "hasCfKey": bool(self.cf_key),
                "cfKeySource": cf_key.source(self.cf_key),
                "source": ({"path": str(self.source_zip),
                            "name": self.source_zip.name}
                           if self.source_zip else None),
                "name": getattr(self.pack, "name", "") or "",
                "version": self.version,
                "files": len(self.merged),
                "strategies": [self._row_payload(n, n in self.root_files)
                               for n in self.tops + self.root_files],
                "changelog": self.changelog,
                "options": dict(self.options),
                "outPath": self.out_path,
                "error": self.error,
                "progress": dict(self.progress),
            }

    # ------------------------------------------------------------------
    # 入口：统一判断 + 后台识别（界面只走这一个）
    # ------------------------------------------------------------------
    def open(self, raw: str) -> dict:
        """整合包定位：实例目录 / 启动器导出包都行。

        判断本身很便宜（见 `core.pack_source`），但"扫描 / 解析"是重活，
        所以这里只做到"认出是什么"，然后立刻返回（phase=loading），
        真正的读取交给后台线程，界面不会卡在点击上。
        """
        if self.phase == "exporting":
            raise DevError("上一步还没完成，稍等一下")
        try:
            ref = pack_source.classify(raw)
        except pack_source.SourceError as exc:
            raise DevError(exc.message, code=exc.code)

        with self._lock:
            self._open_seq += 1
            seq = self._open_seq
            self.phase = "loading"
            self.error = None
            self.progress = {}
            self.pending = {"kind": ref.kind, "path": str(ref.path),
                            "name": ref.label, "note": ref.note}
        snap = self.snapshot()
        self.controller.push_state()
        threading.Thread(target=self._open_worker, args=(seq, ref),
                         name="dev-open", daemon=True).start()
        return snap

    def _open_worker(self, seq: int, ref: pack_source.SourceRef) -> None:
        try:
            if ref.kind == pack_source.KIND_INSTANCE:
                self._load_instance(ref.root, seq=seq)
            else:
                self._load_zip(ref.path, seq=seq)
        except Exception as exc:                        # noqa: BLE001
            with self._lock:
                if seq != self._open_seq:               # 后来居上，丢弃这次结果
                    return
                self.phase = "error"
                self.error = str(exc) or "识别失败"
                self.pending = None
            self.bus.log(f"整合包识别失败：{self.error}", "ERROR")
        else:
            with self._lock:
                if seq != self._open_seq:
                    return
                self.pending = None
        finally:
            self.controller.push_state()

    # ------------------------------------------------------------------
    def import_zip(self, raw: str) -> dict:
        """同步导入启动器导出的 zip（老入口，兼容脚本与用例；界面走 open）。"""
        if self.busy():
            raise DevError("上一步还没完成，稍等一下")
        path = Path(str(raw or "")).expanduser()
        if not path.is_file():
            raise DevError(f"文件不存在：{path}")
        if path.suffix.lower() not in (".zip", ".mrpack"):
            raise DevError("请选择启动器导出的整合包（.zip / .mrpack）")
        self._load_zip(path)
        return self.snapshot()

    def _load_zip(self, path: Path, seq: int | None = None) -> None:
        pack, err = parse_mrpack(path)
        if pack is None:
            raise DevError(f"不是有效的整合包：{err}")
        merged = merge_files(pack)
        paths = [str(m.get("path", "")) for m in merged if m.get("path")]
        tops = sorted({p.split("/", 1)[0] for p in paths if "/" in p})
        files = sorted({p for p in paths
                        if "/" not in p and not is_reserved_root_name(p)})

        with self._lock:
            if seq is not None and seq != self._open_seq:
                return                                   # 后来居上：这次结果作废
            self.mode = "zip"
            self.scan = None
            self.specials = []
            self.source_zip = path
            self.pack = pack
            self.merged = merged
            self.tops = tops
            self.root_files = files
            self.version = str(getattr(pack, "version_id", "") or "")
            self.strategies = {n: "完全匹配" for n in tops + files}
            self.excluded = set()
            self.subs = {}
            self.phase = "idle"
            self.error = None
            self.progress = {}
            self.out_path = ""
        self.bus.log(f"已导入整合包：{path.name}（{len(merged)} 个文件）", "SUCCESS")
        self.controller.push_state()

    # ------------------------------------------------------------------
    def _scan_summary(self) -> dict | None:
        if not self.scan:
            return None
        totals = dict(self.scan.get("totals") or {})
        rows: list[dict] = []
        for row in self.scan.get("rows", []):
            if not row.get("files"):
                continue
            out = dict(row)
            folder = out.get("name", "")
            if not out.get("isFile") and folder in rules_mod.SUB_SCOPE_TITLES:
                # 没勾选的子项：从 children 里拿掉，并从计数里减掉。
                # scan 本身是不带子项规则的（否则取消勾选后就看不见这一项了），
                # 所以这里只是"视图扣除"，导出时靠 _current_rules 再真正排除。
                dropped = {k for k, v in self.subs.items()
                           if v.get("folder") == folder
                           and not v.get("checked", True)}
                if dropped:
                    kept = []
                    sub_files = sub_bytes = sub_candidates = 0
                    for child in out.get("children") or []:
                        if child.get("path") in dropped:
                            sub_files += int(child.get("files") or 0)
                            sub_bytes += int(child.get("bytes") or 0)
                            sub_candidates += int(child.get("candidates") or 0)
                            continue
                        kept.append(child)
                    out["children"] = kept
                    out["files"] = max(0, int(out.get("files") or 0) - sub_files)
                    out["bytes"] = max(0, int(out.get("bytes") or 0) - sub_bytes)
                    out["candidates"] = max(0, int(out.get("candidates") or 0)
                                             - sub_candidates)
                    totals["files"] = totals.get("files", 0) - sub_files
                    totals["bytes"] = totals.get("bytes", 0) - sub_bytes
                    totals["candidates"] = (totals.get("candidates", 0)
                                            - sub_candidates)
                out["subs"] = self._sub_rows(folder)
            rows.append(out)
        return {
            "root": self.scan.get("root", ""),
            "name": Path(self.scan.get("root", "")).name,
            "minecraft": self.scan.get("minecraft", ""),
            "loader": self.scan.get("loader", ""),
            "loaderVersion": self.scan.get("loaderVersion", ""),
            "totals": totals,
            "unsafeLinks": list(self.scan.get("unsafeLinks") or []),
            "badNames": list(self.scan.get("badNames") or []),
            "rows": rows,
        }

    # ------------------------------------------------------------------
    # 动态子项：saves / resourcepacks / texturepacks / shaderpacks
    # ------------------------------------------------------------------
    def _sub_rows(self, folder: str) -> list[dict]:
        rows = []
        for path, item in self.subs.items():
            if item.get("folder") != folder:
                continue
            rows.append({
                "path": path,
                "name": item.get("name", ""),
                "kind": item.get("kind", "dir"),
                "files": int(item.get("files") or 0),
                "bytes": int(item.get("bytes") or 0),
                "checked": bool(item.get("checked", True)),
                "strategy": item.get("strategy") or "",
                "inherited": self.strategies.get(folder, ""),
                "strategies": [x.value for x in STRATEGY_LIST],
            })
        # 目录在前、按名字排（存档不按时间：扫描里没带 mtime，别伪造顺序）
        rows.sort(key=lambda r: (r["kind"] != "dir", r["name"].lower()))
        return rows

    def _row_payload(self, name: str, is_file: bool) -> dict:
        row = {"name": name, "current": self.strategies.get(name, ""),
               "excluded": name in self.excluded, "isFile": is_file}
        if not is_file and name in rules_mod.SUB_SCOPE_TITLES:
            row["title"] = rules_mod.SUB_SCOPE_TITLES[name]
            row["subs"] = self._sub_rows(name)
        return row

    def _sync_subs(self, scan: dict | None) -> None:
        """扫描结果 → 子项表：新出现的默认导出，已有的保留勾选/策略与计数。"""
        if not scan:
            return
        for row in scan.get("rows", []):
            folder = row.get("name", "")
            if row.get("isFile") or folder not in rules_mod.SUB_SCOPE_TITLES:
                continue
            for child in row.get("children") or []:
                path = str(child.get("path") or "")
                if not path.startswith(folder + "/"):
                    continue
                name = path.split("/", 1)[1]
                if not name or "/" in name:
                    continue
                if any(bad in name for bad in rules_mod.SUB_OPTION_BLACKLIST):
                    continue
                item = self.subs.get(path)
                if item is None:
                    item = self.subs[path] = {
                        "folder": folder,
                        "name": name,
                        "kind": ("file" if rules_mod.is_sub_archive(name)
                                 else "dir"),
                        "checked": True,
                        "strategy": "",
                    }
                item["files"] = int(child.get("files") or 0)
                item["bytes"] = int(child.get("bytes") or 0)
        for path in [k for k, v in self.subs.items()
                     if v.get("folder") not in rules_mod.SUB_SCOPE_TITLES]:
            self.subs.pop(path, None)

    def _current_rules(self, include_subs: bool = True) -> list[str]:
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

        extra = [(f"!{name}" if _is_file(name) else f"!{name}\\")
                 for name in sorted(self.excluded)]
        if include_subs:
            for path, item in sorted(self.subs.items()):
                if not item.get("checked", True):
                    extra.append(rules_mod.sub_rule(
                        path, item.get("kind") == "file"))
        return rules_mod.expand_rules(set(self.rule_keys), tuple(extra))

    def _rescan(self) -> None:
        if not self.scan:
            return
        self.scan = instance_scan.scan_instance(
            self.scan["root"], self._current_rules(include_subs=False))
        self._sync_scope()

    def _sync_scope(self) -> None:
        """规则变化后刷新顶层清单：新增的补默认策略，消失的移除。"""
        if not self.scan:
            return
        self._sync_subs(self.scan)
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
        """从本地整合包目录直接制作更新包（老入口，界面走 open）。"""
        if self.busy():
            raise DevError("上一步还没完成，稍等一下")
        raw = str(raw or "").strip()
        if not raw:
            raise DevError("请选择整合包目录")
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise DevError(f"目录不存在：{path}")
        self._load_instance(raw)
        return self.snapshot()

    def _load_instance(self, raw: str, seq: int | None = None) -> None:
        path = Path(str(raw or "")).expanduser()
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
        # 存档默认不导出（PCL 同款），但要在"更新范围"里留一行让人找得到：
        # 行显示为灰（已排除），点目录名才把它纳入包；纳入后再展开子项。
        has_saves = (Path(root) / "saves").is_dir()
        if has_saves and "saves" not in tops:
            tops.append("saves")
        specials = export_specials.detect(root)
        with self._lock:
            if seq is not None and seq != self._open_seq:
                return                                   # 后来居上：这次结果作废
            self.mode = "instance"
            self.scan = scan
            self.specials = specials
            self.source_zip = None
            self.pack = None
            self.merged = []
            self.tops = tops
            self.root_files = files
            self.strategies = {n: "完全匹配" for n in tops + files}
            self.excluded = set()
            self.subs = {}
            self._sync_subs(scan)
            if has_saves:
                self.rule_keys.add("saves")          # 有规则才扫得到，导出靠下面的 excluded 挡住
                self.excluded.add("saves")
                self.strategies.setdefault("saves", "完全匹配")
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
            raise DevError("上一步还没完成，稍等一下")
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
        """保存用户自己的 CurseForge API Key；传空串 = 清掉，回到内置默认。"""
        self.cf_key = str(key or "").strip()
        try:
            cfg = db.load_app_config()
            cfg["cf_api_key"] = self.cf_key
            db.save_app_config(cfg)
        except Exception:                                  # noqa: BLE001
            pass
        self.controller.push_state()
        return self.snapshot()

    def clear(self) -> dict:
        if self.busy():
            raise DevError("上一步还没完成，稍等一下")
        self._cleanup_temp()
        with self._lock:
            self._open_seq += 1                          # 清空后，晚到的识别结果不再写入
            self.pending = None
            self.mode = "zip"
            self.scan = None
            self.specials = []
            self.source_zip = None
            self.pack = None
            self.merged = []
            self.tops = []
            self.root_files = []
            self.strategies = {}
            self.excluded = set()
            self.subs = {}
            self.phase = "idle"
            self.error = None
            self.progress = {}
            self.out_path = ""
        self.controller.push_state()
        return self.snapshot()

    # ------------------------------------------------------------------
    def set_option(self, key: str, value: bool) -> dict:
        if self.busy():
            raise DevError("上一步还没完成，稍等一下")
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
            raise DevError("上一步还没完成，稍等一下")
        with self._lock:
            self.changelog = str(text or "")
        return self.snapshot()

    def set_strategy(self, name: str, value: str = "", excluded: bool | None = None) -> dict:
        if self.busy():
            raise DevError("上一步还没完成，稍等一下")
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
                    if name in rules_mod.SUB_SCOPE_TITLES:
                        self.rule_keys.add(name)
        if self.mode == "instance" and set(self.excluded) != before:
            self._rescan()
        self.controller.push_state()
        return self.snapshot()

    def set_sub_all(self, folder: str, checked: bool) -> dict:
        """一个目录下的子项全选 / 全不选（浮层底部的快捷操作）。"""
        if self.busy():
            raise DevError("上一步还没完成，稍等一下")
        folder = str(folder or "")
        if folder not in rules_mod.SUB_SCOPE_TITLES:
            raise DevError(f"没有这个子项目录：{folder}")
        for item in self.subs.values():
            if item.get("folder") == folder:
                item["checked"] = bool(checked)
        self.controller.push_state()
        return self.snapshot()

    def _recommended_strategies(self) -> dict[str, str]:
        """写进 ea_settings.json 的推荐策略：顶层 + 逐项（只写显式设置过的）。"""
        out = {k: v for k, v in self.strategies.items()
               if k not in self.excluded}
        for path, item in self.subs.items():
            if item.get("checked", True) and item.get("strategy"):
                out[path] = item["strategy"]
        return out

    def set_sub(self, path: str, checked: bool | None = None,
                strategy: str | None = None) -> dict:
        """子项（存档 / 资源包 / 光影包里的某一项）：是否导出 + 单独策略。

        `strategy` 传空串表示"跟随父目录"（不写进 ea_settings）。
        不改扫描结果，只改规则与视图计数 —— 所以是即时生效，不用等重扫。
        """
        if self.busy():
            raise DevError("上一步还没完成，稍等一下")
        item = self.subs.get(str(path or ""))
        if item is None:
            raise DevError(f"没有这个子项：{path}")
        if checked is not None:
            item["checked"] = bool(checked)
        if strategy is not None:
            value = str(strategy)
            if value and value not in {x.value for x in STRATEGY_LIST}:
                raise DevError(f"不支持的策略：{value}")
            item["strategy"] = value
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
                raise DevError("正在导出中，稍等一下")
            if self.phase == "loading":
                raise DevError("还在识别整合包，稍等一下")
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
            strategies = self._recommended_strategies()
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
                cf_key=cf_key.resolve(self.cf_key),
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
