"""
eapack.py — 导出标准更新包（.eapack）
------------------------------------------------
.eapack 内部：
  ├── modrinth.index.json    原样复制（保留下载链接和哈希）
  ├── overrides/             实际文件
  ├── changelog.md           更新日志（独立文件）
  ├── ea_settings.json       推荐策略、白名单、导出选项
  ├── ea_hashes.json         哈希清单（可选）
  └── ea_manifest.json       元数据
"""

import hashlib
import json
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .database import get_export_options, get_whitelist
from .hashing import build_hash_manifest
from .mrpack import INDEX_FILENAME, OVERRIDES_DIR, MRPack

EAPACK_FORMAT_VERSION = 1

SETTINGS_FILE = "ea_settings.json"
HASHES_FILE = "ea_hashes.json"
MANIFEST_FILE = "ea_manifest.json"
CHANGELOG_FILE = "changelog.md"


def export_eapack(
    source_dir: Path,
    source_zip: Path | None,
    out_path: Path,
    pack: MRPack,
    merged_files: list[dict],
    strategies: dict[str, str],
    changelog_md: str,
    version: str = "",
    excluded_folders: list[str] | None = None,
    log: Callable[[str, str], None] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> Path | None:
    def _log(level: str, msg: str):
        if log:
            log(level, msg)

    def _prog(done: int, total: int, label: str):
        if progress:
            progress(done, total, label)

    source_dir = Path(source_dir)
    source_zip = Path(source_zip) if source_zip else None
    out_path = Path(out_path)
    excluded = set(excluded_folders or [])

    opts = get_export_options()
    whitelist = get_whitelist()

    if not opts.get("min_size"):
        excluded = set()

    try:
        ovr = source_dir / OVERRIDES_DIR
        override_files: list[Path] = []
        if ovr.is_dir():
            for p in ovr.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(ovr)
                if rel.parts and rel.parts[0] in excluded:
                    continue
                override_files.append(p)

        total_steps = len(override_files) + 5
        step = 0

        # 1) 哈希清单
        hashes_data = None
        if opts.get("include_hashes") or opts.get("precompute_overrides"):
            _prog(step, total_steps, "计算哈希清单")
            _log("info", "计算哈希清单...")

            index_hashes: dict[str, dict] = {}
            for item in merged_files:
                p = item.get("path", "")
                if not p:
                    continue
                top = p.split("/", 1)[0]
                if top in excluded:
                    continue
                idx: dict[str, str] = {}
                if item.get("sha1"):
                    idx["sha1"] = item["sha1"]
                if item.get("sha256"):
                    idx["sha256"] = item["sha256"]
                if item.get("sha512"):
                    idx["sha512"] = item["sha512"]
                if idx:
                    index_hashes[p] = idx

            if ovr.is_dir():
                hashes_data = build_hash_manifest(ovr, whitelist,
                                                  index_hashes,
                                                  exclude_tops=list(excluded))
            else:
                hashes_data = {"files": {}, "folders": {},
                               "whitelist": list(whitelist)}
        step += 1

        # 2) settings：更新日志只保留引用
        _prog(step, total_steps, "写入 ea_settings.json")
        settings = {
            "format_version": EAPACK_FORMAT_VERSION,
            "recommended_strategies": dict(strategies),
            "changelog_file": CHANGELOG_FILE,
            "whitelist": list(whitelist),
            "export_options": dict(opts),
            "excluded_folders": sorted(excluded),
        }
        step += 1

        # 3) manifest
        _prog(step, total_steps, "写入 ea_manifest.json")
        manifest = {
            "format_version": EAPACK_FORMAT_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "pack_name": pack.name,
            "pack_version": version or pack.version_id,
            "game": pack.game,
            "dependencies": pack.dependencies,
            "total_files": len(merged_files),
            "whitelist": list(whitelist),
            "excluded_folders": sorted(excluded),
            "has_changelog": bool(changelog_md and changelog_md.strip()),
        }
        if opts.get("tamper_proof"):
            payload = json.dumps(
                {"settings": settings, "manifest": manifest},
                ensure_ascii=False, sort_keys=True)
            manifest["signature"] = hashlib.sha256(
                payload.encode("utf-8")).hexdigest()
        step += 1

        # 4) 写 ZIP
        _prog(step, total_steps, "写入更新包")
        _log("info", f"写入 {out_path.name}...")
        if excluded:
            _log("info", f"最小体积模式：排除 {', '.join(sorted(excluded))}")

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 4.1) index 原样复制
            if source_zip and source_zip.is_file():
                _copy_index_from_zip(source_zip, zf, _log)
            else:
                _log("warn", "缺少源 ZIP，index 文件将为空")

            # 4.2) overrides/
            for p in override_files:
                rel = p.relative_to(ovr)
                arcname = OVERRIDES_DIR + "/" + rel.as_posix()
                zf.write(p, arcname)
                step += 1
                if step % 20 == 0 or step == total_steps - 1:
                    _prog(step, total_steps, f"打包 {rel.as_posix()}")

            # 4.3) changelog.md
            if changelog_md and changelog_md.strip():
                zf.writestr(CHANGELOG_FILE, changelog_md)

            # 4.4) ea_settings.json
            zf.writestr(SETTINGS_FILE,
                        json.dumps(settings, ensure_ascii=False, indent=2))

            # 4.5) ea_hashes.json
            if hashes_data is not None and opts.get("include_hashes"):
                zf.writestr(HASHES_FILE,
                            json.dumps(hashes_data, ensure_ascii=False,
                                       indent=2))

            # 4.6) ea_manifest.json
            zf.writestr(MANIFEST_FILE,
                        json.dumps(manifest, ensure_ascii=False, indent=2))

        _prog(total_steps, total_steps, "完成")
        _log("info", f"导出完成：{out_path}")
        return out_path
    except Exception as e:  # noqa: BLE001
        _log("error", f"导出失败：{e}")
        return None


def _copy_index_from_zip(source_zip: Path, out_zf: zipfile.ZipFile,
                         log: Callable[[str, str], None]):
    try:
        with zipfile.ZipFile(source_zip, "r") as src:
            index_name = None
            for n in src.namelist():
                if n == INDEX_FILENAME or n.endswith("/" + INDEX_FILENAME):
                    index_name = n
                    break
            if not index_name:
                log("warn", "源 ZIP 中未找到 modrinth.index.json")
                return
            data = src.read(index_name)
            out_zf.writestr(INDEX_FILENAME, data)
            log("info", f"已原样复制 {INDEX_FILENAME}")
    except Exception as e:  # noqa: BLE001
        log("error", f"复制 index 失败：{e}")


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------
def read_settings(eapack_path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(eapack_path, "r") as zf:
            return json.loads(zf.read(SETTINGS_FILE).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_manifest(eapack_path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(eapack_path, "r") as zf:
            return json.loads(zf.read(MANIFEST_FILE).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_hashes(eapack_path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(eapack_path, "r") as zf:
            return json.loads(zf.read(HASHES_FILE).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_changelog(eapack_path: Path) -> str | None:
    """
    读取 .eapack 中的 changelog.md。
    兼容旧格式：若不存在独立文件，尝试从 settings 内嵌字段读取。
    """
    try:
        with zipfile.ZipFile(eapack_path, "r") as zf:
            try:
                return zf.read(CHANGELOG_FILE).decode("utf-8")
            except KeyError:
                pass
            try:
                data = json.loads(zf.read(SETTINGS_FILE).decode("utf-8"))
                legacy = data.get("changelog_markdown")
                if legacy:
                    return legacy
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return None

def read_recommended_strategies(eapack_path: Path) -> dict[str, str]:
    """
    读取 .eapack 中开发者预设的推荐策略。
    返回 {文件夹: 策略值字符串}，读不到返回空 dict。
    """
    settings = read_settings(eapack_path)
    if not settings:
        return {}
    rec = settings.get("recommended_strategies", {})
    if not isinstance(rec, dict):
        return {}
    return {str(k): str(v) for k, v in rec.items()}


def read_whitelist(eapack_path: Path) -> list[str]:
    """读取 .eapack 中记录的白名单（文件级哈希路径）"""
    settings = read_settings(eapack_path)
    if not settings:
        return []
    wl = settings.get("whitelist", [])
    return [str(x) for x in wl] if isinstance(wl, list) else []