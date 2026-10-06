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
import os
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

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
def _signature_payload(settings: dict, manifest: dict,
                       entries: list[tuple[str, int, int]]) -> str:
    """
    签名载荷：settings + manifest（不含 signature 字段）+ 包内条目指纹
    （文件名 / 解压后大小 / CRC32，由 zipfile 直接给出，不需要额外读文件）。

    这不是密钥 MAC，无法防"有权重打包的人"伪造；它的用途是**发现包在
    传输/编辑后被改坏或被第三方改过**，比"写个没人校验的签名"有意义。
    """
    return json.dumps({
        "settings": settings,
        "manifest": {k: v for k, v in manifest.items() if k != "signature"},
        "entries": [[n, int(s), int(c)] for n, s, c in entries],
    }, ensure_ascii=False, sort_keys=True)


def compute_signature(settings: dict, manifest: dict,
                      entries: list[tuple[str, int, int]]) -> str:
    payload = _signature_payload(settings, manifest, entries)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_signature(eapack_path: Path) -> tuple[bool, str]:
    """
    校验更新包完整性。
    返回 (ok, reason)：
      - 没有签名（未开启该选项）→ (True, "unsigned")
      - 校验通过 → (True, "")
      - 不通过 → (False, 原因)
    """
    eapack_path = Path(eapack_path)
    try:
        with zipfile.ZipFile(eapack_path, "r") as zf:
            manifest = json.loads(zf.read(MANIFEST_FILE).decode("utf-8"))
            if not isinstance(manifest, dict):
                return False, "ea_manifest.json 结构异常"
            expected = manifest.get("signature", "")
            if not expected:
                return True, "unsigned"
            try:
                settings = json.loads(zf.read(SETTINGS_FILE).decode("utf-8"))
            except Exception:  # noqa: BLE001
                settings = {}
            entries = [(i.filename, i.file_size, i.CRC)
                       for i in zf.infolist() if i.filename != MANIFEST_FILE]
            got = compute_signature(settings or {}, manifest, entries)
            if got != expected:
                return False, "完整性校验不通过（包内容与签名不符）"
            return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"无法读取更新包：{e}"


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

        try:
            with zipfile.ZipFile(tmp_path, "w",
                                 zipfile.ZIP_DEFLATED) as zf:
                # 4.1) index（最小体积模式下按 excluded 过滤 index 条目）
                if source_zip and source_zip.is_file():
                    if not _copy_index_from_zip(source_zip, zf, _log,
                                                excluded):
                        _log("error", "复制 modrinth.index.json 失败，"
                                      "导出中止（缺 index 的包无法被识别）")
                        raise RuntimeError("index copy failed")
                else:
                    _log("error", "缺少源 ZIP，无法复制 index，导出中止")
                    raise RuntimeError("missing source zip")

                # 4.2) overrides/
                wrote_override = False
                for p in override_files:
                    rel = p.relative_to(ovr)
                    arcname = OVERRIDES_DIR + "/" + rel.as_posix()
                    zf.write(p, arcname)
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
                    zf.writestr(zipfile.ZipInfo(OVERRIDES_DIR + "/"), b"")
                    _prog(step, total_steps, "写入 overrides/ 目录条目")

                # 4.3) changelog.md
                if changelog_md and changelog_md.strip():
                    zf.writestr(CHANGELOG_FILE, changelog_md)

                # 4.4) ea_settings.json
                zf.writestr(SETTINGS_FILE,
                            json.dumps(settings, ensure_ascii=False,
                                       indent=2))

                # 4.5) ea_hashes.json
                if hashes_data is not None:
                    zf.writestr(HASHES_FILE,
                                json.dumps(hashes_data, ensure_ascii=False,
                                           indent=2))

                # 4.6) 签名（基于已写入条目的 CRC/大小 + settings/manifest）
                if opts.get("tamper_proof"):
                    entries = [(i.filename, i.file_size, i.CRC)
                               for i in zf.filelist
                               if i.filename != MANIFEST_FILE]
                    manifest["signature"] = compute_signature(
                        settings, manifest, entries)

                # 4.7) ea_manifest.json
                zf.writestr(MANIFEST_FILE,
                            json.dumps(manifest, ensure_ascii=False,
                                       indent=2))
            step += 1
        except Exception:
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


def _copy_index_from_zip(source_zip: Path, out_zf: zipfile.ZipFile,
                         log: Callable[[str, str], None],
                         excluded: set[str] | None = None) -> bool:
    """复制 index；失败返回 False（调用方必须视为致命错误）。"""
    try:
        with zipfile.ZipFile(source_zip, "r") as src:
            index_name = None
            for n in src.namelist():
                if n == INDEX_FILENAME or n.endswith("/" + INDEX_FILENAME):
                    index_name = n
                    break
            if not index_name:
                log("error", "源 ZIP 中未找到 modrinth.index.json")
                return False
            data = src.read(index_name)
            if excluded:
                filtered = _filter_index(data, excluded, log)
                if filtered is not None:
                    data = filtered
            out_zf.writestr(INDEX_FILENAME, data)
            log("info", f"已复制 {INDEX_FILENAME}")
            return True
    except Exception as e:  # noqa: BLE001
        log("error", f"复制 index 失败：{e}")
        return False


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
            except Exception:  # noqa: BLE001, S110
                pass
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