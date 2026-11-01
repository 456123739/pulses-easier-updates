"""
instance_export.py — 从本地整合包直接生成 .eapack（一键制作更新包）
------------------------------------------------
对应 PCL2「导出整合包」的数据流（docs/PCL-整合包导出-技术规范.md §1-§5）：

    判定导出范围 → 遍历分类 → 查 CF/MR 拿直链 → 写 index → 其余进 overrides

与 PCL 的区别：不把整个实例拷进临时目录再打包，而是**原地读文件写 ZIP**，
命中网络资源的只写 index（玩家端自己下），因此导出体积≈overrides 大小。

产物仍是标准 .eapack（index + overrides + ea_*.json + changelog.md），
所以玩家端更新流程零改动。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..config import NEVER_PACK_FILES
from . import fingerprint as fpmod
from . import instance_scan, lookup
from .eapack import (CHANGELOG_FILE, EAPACK_FORMAT_VERSION, HASHES_FILE,
                     SETTINGS_FILE, append_manifest_with_signature)
from .hashing import build_hash_manifest
from .index_build import dependencies as build_deps
from .index_build import build_index, dumps
from .mrpack import INDEX_FILENAME, OVERRIDES_DIR

# 查不到网络资源时，超过这个体积的文件也进 overrides（PCL 的 min_size 语义由调用方控制）
DEFAULT_MIN_SIZE_KB = 0


class _Cancelled(RuntimeError):
    """内部信号：导出中途被取消。"""


class _BadName(RuntimeError):
    """内部信号：文件名无法写进 zip（非 UTF-8）。"""


STALE_SECONDS = 3600          # 超过 1 小时的 .part/.stage 视为上次崩溃留下的


def _sweep_stale(out_path: Path) -> None:
    """清掉上次被强杀留下的临时目录/文件（新文件名带随机串，不会自己复用）。"""
    parent = out_path.parent
    if not parent.is_dir():
        return
    now = time.time()
    for pattern in (f"{out_path.name}.stage-*", f"{out_path.name}.part-*"):
        for path in parent.glob(pattern):
            try:
                if now - path.stat().st_mtime < STALE_SECONDS:
                    continue
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
            except OSError:
                pass
USER_AGENT = "pulses-easier/{version} (modpack updater)"


def _log_default(level: str, message: str) -> None:
    pass


def _prog_default(step: int, total: int, message: str) -> None:
    pass


def _link_or_copy(src: Path, dst: Path) -> None:
    """硬链接优先（同盘零拷贝），失败就复制。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
        return
    except OSError:
        pass
    shutil.copy2(src, dst)


def _uniq() -> str:
    """进程 + 线程 + 随机串：并发导出时各自的临时文件不会互相踩。"""
    return f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"


def export_from_instance(
    root: str | Path,
    out_path: str | Path,
    *,
    rules: list[str],
    strategies: dict | None = None,
    whitelist: list[str] | None = None,
    changelog_md: str = "",
    pack_name: str = "",
    pack_version: str = "1.0.0",
    summary: str = "",
    opts: dict | None = None,
    excluded_tops: list[str] | None = None,
    cf_key: str = "",
    user_agent: str = "",
    progress: Callable[[int, int, str], None] | None = None,
    log: Callable[[str, str], None] | None = None,
    cancel: threading.Event | None = None,
    cache: fpmod.FingerprintCache | None = None,
    fetch=None,
    lookup_fn: Callable | None = None,
    version: str = "",
) -> Path | None:
    """跑完整导出流程；成功返回产物路径。"""
    log = log or _log_default
    _prog = progress or _prog_default
    opts = dict(opts or {})
    root = Path(root)
    out_path = Path(out_path)
    whitelist = list(whitelist or [])
    cache = cache or fpmod.FingerprintCache()
    version = version or getattr(__import__("app.core", fromlist=["__version__"]),
                                 "__version__", "0")
    stamp = _uniq()
    _sweep_stale(out_path)
    tmp_path = out_path.with_name(f"{out_path.name}.part-{stamp}")
    stage = out_path.with_name(f"{out_path.name}.stage-{stamp}")

    def stopped() -> bool:
        return cancel is not None and cancel.is_set()

    def drop(*paths: Path) -> None:
        for path in paths:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink()
            except OSError:
                pass

    _prog(0, 1, "扫描实例")
    scan = instance_scan.scan_instance(root, rules)
    if scan.get("badNames"):
        log("error", "有文件名不是 UTF-8，无法写进 zip：" +
                     "、".join(scan["badNames"][:5]))
        return None
    # 永不进包的文件（options.txt 等）不必读、也不必因为读不出来而中止
    items = [i for i in scan["items"]
             if i["path"] not in NEVER_PACK_FILES
             and i["path"].split("/", 1)[0] not in NEVER_PACK_FILES]
    if not items:
        log("error", "导出范围内没有文件，检查导出选项")
        return None
    log("info", f"扫描完成：{len(items)} 个文件，候选资源 "
                f"{scan['totals']['candidates']} 个")

    # ---- 1) 指纹：只有候选资源需要 CurseForge 指纹（省一遍大文件读盘）----
    cands = [i for i in items if i["candidate"]]
    hashes: dict[str, fpmod.Fingerprints] = {}
    failed: list[str] = []
    total_steps = len(items) + 6
    for n, item in enumerate(items, 1):
        if stopped():
            log("warn", "已取消")
            return None
        try:
            hashes[item["path"]] = cache.compute(
                root / item["path"], curseforge=bool(item["candidate"]))
        except OSError as exc:
            # 读不出来 = 包里会缺这个文件。缺一个 mod 玩家就崩，
            # 所以这里必须失败，不能"跳过并报成功"。
            log("error", f"读取失败：{item['path']}（{exc}）")
            failed.append(item["path"])
            continue
        if n % 25 == 0 or n == len(items):
            _prog(n, total_steps, f"计算哈希 {n}/{len(items)}")
    if failed:
        log("error", f"{len(failed)} 个文件读不出来，导出中止（避免产出缺文件的包）")
        return None

    # ---- 2) 联网查询 ----
    wanted = [{"path": i["path"], "sha1": hashes[i["path"]].sha1,
               "curseforge": hashes[i["path"]].curseforge}
              for i in cands if i["path"] in hashes]
    hits: dict[str, lookup.LookupHit] = {}
    if wanted:
        _prog(len(items) + 1, total_steps, "查询下载地址")
        if lookup_fn is not None:
            hits = lookup_fn(wanted)
        else:
            ua = user_agent or USER_AGENT.format(version=version)
            kwargs = {}
            if fetch is not None:
                kwargs["fetch"] = fetch
            hits = lookup.resolve(wanted, cf_key=cf_key, user_agent=ua,
                                  cancel=cancel, **kwargs)
        log("info", f"命中网络资源 {len(hits)}/{len(wanted)}")
        if not hits:
            log("warn", "没有查到任何下载地址，所有文件都将打包进 overrides"
                        "（包会变大，但玩家端仍可正常更新）")
    else:
        log("info", "没有需要联网查询的文件")

    # ---- 3) index：以"真正写进 index 的条目"为准，其余一律本地打包 ----
    index_files = []
    for path, hit in sorted(hits.items()):
        fp_item = hashes.get(path)
        urls = [u for u in (hit.urls or []) if u]
        if fp_item is None or not urls:
            continue
        index_files.append({"path": path, "sha1": fp_item.sha1,
                            "sha512": fp_item.sha512,
                            "downloads": urls, "size": fp_item.size})
    index = build_index(
        name=pack_name or root.name,
        version_id=pack_version,
        summary=summary,
        files=index_files,
        deps=build_deps(scan.get("loader", ""), scan.get("loaderVersion", ""),
                        scan.get("minecraft", "")),
    )
    indexed = {entry["path"] for entry in index["files"]}
    if hits and not indexed:
        log("warn", "查询结果里没有可用的下载地址，全部改为本地打包")
    _prog(len(items) + 2, total_steps, "写入 index")

    # ---- 4) overrides 暂存（硬链接，不复制数据）----
    staged = 0
    try:
        stage.mkdir(parents=True, exist_ok=True)
        local_items = [i for i in items if i["path"] not in indexed]
        for n, item in enumerate(local_items, 1):
            if stopped():
                log("warn", "已取消")
                return None
            rel = item["path"]
            try:
                _link_or_copy(root / rel, stage / rel)
                staged += 1
            except OSError as exc:
                log("error", f"暂存失败：{rel}（{exc}）")
                failed.append(rel)
            if n % 50 == 0 or n == len(local_items):
                _prog(len(items) + 3, total_steps,
                      f"暂存本地文件 {n}/{len(local_items)}")

        if failed:
            log("error", f"{len(failed)} 个文件没能进包，导出中止")
            return None

        # ---- 5) 哈希清单 ----
        index_hashes = {item["path"]: {"sha1": item["sha1"],
                                       "sha512": item["sha512"]}
                        for item in index_files}
        hashes_data = None
        if opts.get("include_hashes", True) or opts.get("precompute_overrides"):
            hashes_data = build_hash_manifest(stage, whitelist, index_hashes)
        _prog(len(items) + 4, total_steps, "写入哈希清单")

        # ---- 6) 打 ZIP ----
        settings = {
            "format_version": EAPACK_FORMAT_VERSION,
            "recommended_strategies": dict(strategies or {}),
            "changelog_file": CHANGELOG_FILE,
            "whitelist": whitelist,
            "export_options": dict(opts),
            "excluded_folders": sorted(set(excluded_tops or [])),
        }
        manifest = {
            "format_version": EAPACK_FORMAT_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),  # noqa: DTZ005
            "pack_name": index["name"],
            "pack_version": index["versionId"],
            "game": "minecraft",
            "dependencies": index["dependencies"],
            "total_files": len(indexed) + staged,
            "whitelist": whitelist,
            "has_changelog": bool(changelog_md and changelog_md.strip()),
            "source": "instance",
        }

        out_path.parent.mkdir(parents=True, exist_ok=True)
        drop(tmp_path)
        _prog(len(items) + 5, total_steps, "写入更新包")
        log("info", f"写入 {out_path.name}...")
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(INDEX_FILENAME, dumps(index))
            wrote = False
            for p in sorted(stage.rglob("*")):
                if not p.is_file():
                    continue
                if stopped():
                    raise _Cancelled()
                rel = p.relative_to(stage).as_posix()
                try:
                    zf.write(p, f"{OVERRIDES_DIR}/{rel}")
                except UnicodeEncodeError as exc:
                    raise _BadName(f"文件名不是 UTF-8，无法写入 zip：{rel}") from exc
                except OSError as exc:
                    try:
                        exc.add_note(f"写入 {rel} 失败")
                    except AttributeError:      # Python < 3.11
                        pass
                    raise
                wrote = True
            if not wrote:
                zf.writestr(zipfile.ZipInfo(OVERRIDES_DIR + "/"), b"")
            if changelog_md and changelog_md.strip():
                zf.writestr(CHANGELOG_FILE, changelog_md)
            zf.writestr(SETTINGS_FILE,
                        json.dumps(settings, ensure_ascii=False, indent=2))
            if hashes_data is not None:
                zf.writestr(HASHES_FILE,
                            json.dumps(hashes_data, ensure_ascii=False,
                                       indent=2))
        # 签名与 manifest 最后追加（v2：内容哈希，抗同 CRC 改写）
        append_manifest_with_signature(tmp_path, settings, manifest,
                                       bool(opts.get("tamper_proof")))
        if stopped():
            raise _Cancelled()
        try:
            os.replace(tmp_path, out_path)
        except OSError as exc:
            log("error", f"写入目标文件失败：{exc}")
            drop(tmp_path)
            return None
        cache.flush()
        _prog(total_steps, total_steps, "完成")
        log("info", f"导出完成：{out_path}")
        return out_path
    except _Cancelled:
        log("warn", "已取消")
        drop(tmp_path)
        return None
    except _BadName as exc:
        log("error", str(exc))
        drop(tmp_path)
        return None
    except Exception:
        drop(tmp_path)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
