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
from ..core.mrpack import (MRPack, is_reserved_root_name, merge_files,
                           parse_mrpack)

EXPORT_KEYS = ("include_hashes", "tamper_proof", "precompute_overrides",
               "min_size")


class DevError(RuntimeError):
    pass


class DevFlow:
    def __init__(self, controller, bus):
        self.controller = controller
        self.bus = bus
        self._lock = threading.RLock()

        self.phase = "idle"           # idle | exporting | done | error
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

    def clear(self) -> dict:
        if self.busy():
            raise DevError("正在导出")
        self._cleanup_temp()
        with self._lock:
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
        with self._lock:
            self.changelog = str(text or "")
        return self.snapshot()

    def set_strategy(self, name: str, value: str = "", excluded: bool | None = None) -> dict:
        if name not in self.strategies and name not in self.excluded:
            raise DevError(f"没有这个条目：{name}")
        with self._lock:
            if value:
                self.strategies[name] = value
            if excluded is not None:
                if excluded:
                    self.excluded.add(name)
                else:
                    self.excluded.discard(name)
        self.controller.push_state()
        return self.snapshot()

    # ------------------------------------------------------------------
    def start_export(self, out_path: str) -> dict:
        if self.busy():
            raise DevError("正在导出")
        if self.pack is None or self.source_zip is None:
            raise DevError("请先导入整合包")
        out = Path(out_path).expanduser()
        if out.suffix.lower() != ".eapack":
            out = out.with_suffix(".eapack")
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.phase = "exporting"
            self.error = None
            self.progress = {"done": 0, "total": len(self.merged), "label": "准备中…"}
            self.out_path = str(out)
        self._thread = threading.Thread(target=self._worker, args=(out,), daemon=True)
        self._thread.start()
        return {"started": True, "out": str(out)}

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
                raise DevError("导出失败（详见日志）")
            size = out.stat().st_size if out.exists() else 0
            with self._lock:
                self.phase = "done"
                self.progress = {"done": len(self.merged),
                                 "total": len(self.merged), "label": "完成"}
                self.out_path = str(out)
            self.bus.log(f"更新包已生成：{out.name}（{size:,} B）", "SUCCESS")
        except Exception as exc:                           # noqa: BLE001
            with self._lock:
                self.phase = "error"
                self.error = str(exc)
            self.bus.log(f"导出失败：{exc}", "ERROR")
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
