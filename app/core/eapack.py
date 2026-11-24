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

import json
import os
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..config import NEVER_PACK_FILES
from . import container as ct
from .packsign import (compute_signature, compute_signature_v2,
                       content_entries as _content_entries,
                       zip_content_entries_v2 as _zip_content_entries)
from .database import get_export_options, get_whitelist
from .hashing import build_hash_manifest
from .mrpack import (
    INDEX_FILENAME,
    OVERRIDES_DIR,
    MRPack,
    locate_content_root,
)

EAPACK_FORMAT_VERSION = 1

SETTINGS_FILE = "ea_settings.json"
HASHES_FILE = "ea_hashes.json"
MANIFEST_FILE = "ea_manifest.json"
CHANGELOG_FILE = "changelog.md"


# ----------------------------------------------------------------------
# 完整性签名
# ----------------------------------------------------------------------




def append_manifest_with_signature(zip_path: Path, settings: dict,
                                   manifest: dict,
                                   tamper_proof: bool) -> dict:
    """写完内容后追加 ea_manifest.json；开启防篡改时用内容哈希签名（v2）。"""
    if tamper_proof:
        manifest["signature_version"] = 2   # 先定版本，签名载荷里要包含它
        entries = _content_entries(zip_path)
        manifest["signature"] = compute_signature_v2(settings, manifest, entries)
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_FILE,
                    json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def verify_signature(eapack_path: Path) -> tuple[bool, str]:
    """
    校验更新包完整性（v2 容器与 zip 都支持）。
    返回 (ok, reason)：
      - 没有签名（未开启该选项）→ (True, "unsigned")
      - 校验通过 → (True, "")
      - 不通过 → (False, 原因)

    v2（当前）：逐条读内容算 sha1 再签名，能挡住"同 CRC 改写"；
    v1（0.10.x 及更早）：只签 CRC/size，仍按老算法校验，保证旧包可读。
    """
    eapack_path = Path(eapack_path)
    crc_entries: list[tuple[str, int, int]] | None = None
    is_zip = False
    try:
        with ct.open_container(eapack_path) as reader:
            is_zip = isinstance(reader, ct.ZipReader)
            manifest = json.loads(reader.read(MANIFEST_FILE).decode("utf-8"))
            if not isinstance(manifest, dict):
                return False, "ea_manifest.json 结构异常"
            expected = manifest.get("signature", "")
            if not expected:
                return True, "unsigned"
            try:
                settings = json.loads(reader.read(SETTINGS_FILE).decode("utf-8"))
            except Exception:  # noqa: BLE001
                settings = {}
            version = int(manifest.get("signature_version") or 1)
            if version < 2 and is_zip:
                crc_entries = [(i.filename, i.file_size, i.CRC)
                               for i in reader._zf.infolist()
                               if i.filename != MANIFEST_FILE]
    except Exception as e:  # noqa: BLE001
        return False, f"无法读取更新包：{e}"

    try:
        if version >= 2:
            entries2 = (_zip_content_entries(eapack_path) if is_zip
                        else _content_entries(eapack_path))
            got = compute_signature_v2(settings or {}, manifest, entries2)
        else:
            if crc_entries is None:
                return False, "旧版签名口径只能校验 zip 包"
            got = compute_signature(settings or {}, manifest, crc_entries)
    except Exception as e:  # noqa: BLE001
        return False, f"无法校验更新包：{e}"
    if got != expected:
        return False, "完整性校验不通过（包内容与签名不符）"
    return True, ""


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
    opts: dict | None = None,
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

    _db_opts = get_export_options()
    _db_opts.update({k: v for k, v in (opts or {}).items() if v is not None})
    opts = _db_opts
    from .packwriter import resolve_export_options
    wopts = resolve_export_options(opts)
    whitelist = get_whitelist()

    # `excluded_folders` is the explicit STEP 1 scope. It must be honored
    # for every source mode; `min_size` only controls the optional automatic
    # exclusions from older callers, never a user's explicit selection.
    #
    # The ZIP developer flow passes scope exclusions directly. Clearing them
    # when min_size is false leaked excluded local content into exports.

    # 根目录散文件里的"玩家本地设置"永远不进包（与 min_size 无关）：
    # options.txt 里是键位/画质/音量，随包分发只会覆盖玩家自己的设置。

    try:
        # 源包可能把 index 与 overrides 放在一层子目录里（launcher 导出很常见）。
        # 旧实现在这里只认根级 overrides/，于是嵌套源包会被导出成
        # "只有 index + 空 overrides/" 的残包 —— overrides 全部静默丢失。
        content_root, is_fallback = locate_content_root(source_dir)
        ovr = (source_dir / OVERRIDES_DIR) if is_fallback else content_root
        override_files: list[Path] = []
        if ovr.is_dir():
            for p in ovr.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(ovr)
                if rel.parts and rel.parts[0] in excluded:
                    continue
                if rel.as_posix() in NEVER_PACK_FILES:
                    continue
                override_files.append(p)

        total_steps = len(override_files) + 4
        step = 0

        # 1) 哈希清单（include_hashes 写文件；precompute_overrides 也触发计算，
        #    两者都会写进 ea_hashes.json —— 玩家端比对时直接消费，省一次读取）
        hashes_data = None
        want_hashes = bool(opts.get("include_hashes")
                           or opts.get("precompute_overrides"))
        if want_hashes:
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
            # 本地时间、只给人看（不参与比较/判定），不需要时区
            "created_at": datetime.now().isoformat(  # noqa: DTZ005
                timespec="seconds"),
            "pack_name": pack.name,
            "pack_version": version or pack.version_id,
            "game": pack.game,
            "dependencies": pack.dependencies,
            "total_files": len(merged_files),
            "whitelist": list(whitelist),
            "excluded_folders": sorted(excluded),
            "has_changelog": bool(changelog_md and changelog_md.strip()),
        }
        step += 1

        # 4) 写 ZIP（先写临时文件，成功后再原子替换到目标路径）
        _prog(step, total_steps, "写入更新包")
        _log("info", f"写入 {out_path.name}...")
        if excluded:
            _log("info", f"最小体积模式：排除 {', '.join(sorted(excluded))}")

        tmp_path = out_path.with_name(out_path.name + ".part-eapack")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass

        from .packwriter import PackWriter

        writer = PackWriter(tmp_path, use_v2=wopts["use_v2"],
                            level=wopts["level"],
                            mca_transform=wopts["mca"],
                            jar_transform=wopts["jar"],
                            log=_log)
        try:
            # 4.1) index（最小体积模式下按 excluded 过滤 index 条目）
            if source_zip and source_zip.is_file():
                index_bytes = _read_index_from_zip(source_zip, excluded, _log)
                if index_bytes is None:
                    _log("error", "复制 modrinth.index.json 失败，"
                                  "导出中止（缺 index 的包无法被识别）")
                    raise RuntimeError("index copy failed")
                writer.add_bytes(INDEX_FILENAME, index_bytes, inline=True)
            else:
                _log("error", "缺少源 ZIP，无法复制 index，导出中止")
                raise RuntimeError("missing source zip")

            # 4.2) overrides/
            wrote_override = False
            for p in override_files:
                rel = p.relative_to(ovr)
                arcname = OVERRIDES_DIR + "/" + rel.as_posix()
                writer.add_file(arcname, p)
                wrote_override = True
                step += 1
                if step % 20 == 0 or step == total_steps - 1:
                    _prog(step, total_steps, f"打包 {rel.as_posix()}")

            if not wrote_override:
                # 必须**无条件**写出 overrides/ 目录条目。
                # 玩家端用「解压目录里有没有 overrides/」决定内容根，
                # 缺了它就会退化成"整合包根目录"，把本包自己的
                # modrinth.index.json / ea_*.json / changelog.md 当成
                # 更新内容写进玩家整合包根目录。
                writer.add_dir_marker(OVERRIDES_DIR + "/")
                _prog(step, total_steps, "写入 overrides/ 目录条目")

            # 4.3) changelog.md
            if changelog_md and changelog_md.strip():
                writer.add_bytes(CHANGELOG_FILE, changelog_md.encode("utf-8"),
                                 inline=True)

            # 4.4) ea_settings.json
            writer.add_bytes(SETTINGS_FILE,
                             json.dumps(settings, ensure_ascii=False,
                                        indent=2).encode("utf-8"),
                             inline=True)

            # 4.5) ea_hashes.json
            if hashes_data is not None:
                writer.add_bytes(HASHES_FILE,
                                 json.dumps(hashes_data, ensure_ascii=False,
                                            indent=2).encode("utf-8"),
                                 inline=True)

            # 4.6) 签名 + ea_manifest.json（签名用写时记录的 sha1，不重读包）
            writer.finish(settings, manifest,
                          tamper_proof=bool(opts.get("tamper_proof")))
            step += 1
        except Exception:
            writer.abort()
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

        try:
            os.replace(tmp_path, out_path)
        except OSError as e:
            _log("error", f"写入目标文件失败：{e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return None

        _prog(total_steps, total_steps, "完成")
        _log("info", f"导出完成：{out_path}")
        return out_path
    except Exception as e:  # noqa: BLE001
        _log("error", f"导出失败：{e}")
        return None


def _filter_index(data: bytes, excluded: set[str],
                  log: Callable[[str, str], None]) -> bytes | None:
    """最小体积模式：从 index 里剔除 excluded 顶层目录下的条目。
    返回 None 表示"不需要改动"。"""
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log("warn", f"index 解析失败，按原样复制：{e}")
        return None
    files = obj.get("files")
    if not isinstance(files, list):
        return None
    kept = []
    dropped = 0
    for f in files:
        p = str(f.get("path", "")) if isinstance(f, dict) else ""
        top = p.split("/", 1)[0] if "/" in p else ""
        if top and top in excluded:
            dropped += 1
            continue
        kept.append(f)
    if not dropped:
        return None
    obj["files"] = kept
    log("info", f"最小体积：index 中剔除 {dropped} 个条目")
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def _read_index_from_zip(source_zip: Path,
                         excluded: set[str] | None,
                         log: Callable[[str, str], None]) -> bytes | None:
    """从源 ZIP 里取出 index（可按最小体积模式过滤）；失败返回 None。"""
    try:
        with zipfile.ZipFile(source_zip, "r") as src:
            index_name = None
            for n in src.namelist():
                if n == INDEX_FILENAME or n.endswith("/" + INDEX_FILENAME):
                    index_name = n
                    break
            if not index_name:
                log("error", "源 ZIP 中未找到 modrinth.index.json")
                return None
            data = src.read(index_name)
    except Exception as e:  # noqa: BLE001
        log("error", f"复制 index 失败：{e}")
        return None
    if excluded:
        filtered = _filter_index(data, excluded, log)
        if filtered is not None:
            data = filtered
    log("info", f"已复制 {INDEX_FILENAME}")
    return data


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------
def _read_json_member(eapack_path: Path, name: str):
    try:
        with ct.open_container(eapack_path) as reader:
            if name not in reader.entries:
                return None
            return json.loads(reader.read(name).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_settings(eapack_path: Path) -> dict | None:
    return _read_json_member(eapack_path, SETTINGS_FILE)


def read_manifest(eapack_path: Path) -> dict | None:
    return _read_json_member(eapack_path, MANIFEST_FILE)


def read_hashes(eapack_path: Path) -> dict | None:
    return _read_json_member(eapack_path, HASHES_FILE)


def read_changelog(eapack_path: Path) -> str | None:
    """
    读取 .eapack 中的 changelog.md。
    兼容旧格式：若不存在独立文件，尝试从 settings 内嵌字段读取。
    """
    try:
        with ct.open_container(eapack_path) as reader:
            if CHANGELOG_FILE in reader.entries:
                return reader.read(CHANGELOG_FILE).decode("utf-8")
            data = _read_json_member(eapack_path, SETTINGS_FILE) or {}
            legacy = data.get("changelog_markdown")
            if legacy:
                return legacy
    except Exception:  # noqa: BLE001, S110
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