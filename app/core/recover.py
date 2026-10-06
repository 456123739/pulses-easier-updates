"""
recover.py — 更新被强杀之后的"就地自检"
------------------------------------------------
进程被强杀 / 断电时，磁盘上可能留下两类残渣：

1. `.xxx.pulses_tmp`（目录整体替换时旧内容的副本）
   替换流程是"旧目录改名 → 写新目录 → 删旧副本"。**副本还在，就说明
   这次替换没有跑完**（正常成功一定会把它删掉）。
   玩家看到的可能是一个残缺的 `config`，加一个点号开头的陌生目录。

2. `.yyy.pulses_new`（文件搬运的临时文件）
   目标端只会被 `os.replace` 原子覆盖，所以这些临时文件一律可删。

处理原则（宁可能多跑一次更新，也不让玩家丢东西）：
  - 有 `.pulses_tmp` → 把目标目录整个删掉，把副本改名回原位
    （= 回滚这次没做完的替换；重跑一次更新即可收敛）
  - 有 `.pulses_new` → 删除

代价只有"替换过的目录要重做一次"，绝不会出现"整合包目录残缺"。
"""

import os
import shutil
import time
from pathlib import Path

from . import transfer

TMP_MARK = ".pulses_tmp"
NEW_MARK = ".pulses_new"

# 扫描上限：正常不会有这么多残渣，超过就当作不是残渣（避免误伤）
_MAX_ITEMS = 500


def _split_tmp_name(name: str) -> str | None:
    """`.config.pulses_tmp` / `.config.pulses_tmp2` → `config`"""
    if not name.startswith(".") or TMP_MARK not in name:
        return None
    body = name[1:]
    idx = body.find(TMP_MARK)
    if idx <= 0:
        return None
    rest = body[idx + len(TMP_MARK):]
    if rest and not rest.isdigit():
        return None
    return body[:idx]


def find_leftovers(pack_root: Path) -> dict:
    """
    找出整合包里遗留的临时残渣（只扫一遍目录树）。

    返回 {"tmp_dirs": [(副本, 目标)], "new_files": [路径]}
    """
    root = Path(pack_root)
    tmp_dirs: list[tuple[Path, Path]] = []
    new_files: list[Path] = []
    if not root.is_dir():
        return {"tmp_dirs": tmp_dirs, "new_files": new_files}
    try:
        for p in root.rglob("*"):
            name = p.name
            if TMP_MARK in name:
                target_name = _split_tmp_name(name)
                if target_name:
                    tmp_dirs.append((p, p.with_name(target_name)))
            elif name.endswith(NEW_MARK):
                new_files.append(p)
            if len(tmp_dirs) + len(new_files) >= _MAX_ITEMS:
                break
    except OSError:
        pass
    return {"tmp_dirs": tmp_dirs, "new_files": new_files}


def has_leftovers(pack_root: Path) -> bool:
    got = find_leftovers(pack_root)
    return bool(got["tmp_dirs"] or got["new_files"])


def recover_leftovers(pack_root: Path, log=None,
                      max_age_s: float = 0.0) -> dict:
    """
    就地把残渣收拾干净。返回统计结果（也用于日志/提示）。

    `max_age_s > 0` 时只处理"足够旧"的残渣（避免误删同一进程里正在
    进行的搬运；启动自检用 0 即可）。
    """
    now = time.time()
    stats: dict = {"restored": [], "removed_files": [], "skipped": []}

    def _say(level: str, msg: str):
        if log is not None:
            try:
                log(level, msg)
            except Exception:  # noqa: BLE001, S110
                pass

    found = find_leftovers(pack_root)

    def _too_new(p: Path) -> bool:
        if max_age_s <= 0:
            return False
        try:
            return (now - p.stat().st_mtime) < max_age_s
        except OSError:
            return True

    for sidecar, target in found["tmp_dirs"]:
        if _too_new(sidecar):
            stats["skipped"].append(sidecar.name)
            continue
        try:
            if target.exists():
                shutil.rmtree(transfer.sys_path(target),
                              ignore_errors=True)
            os.replace(transfer.sys_path(sidecar),
                       transfer.sys_path(target))
            stats["restored"].append(target.name)
            _say("warn", f"上次替换没做完，已回滚为替换前的内容："
                         f"{target.name}")
        except OSError as e:
            stats["skipped"].append(sidecar.name)
            _say("error", f"恢复 {sidecar.name} 失败："
                          f"{transfer.friendly_os_error(e)}")

    for p in found["new_files"]:
        if _too_new(p):
            stats["skipped"].append(p.name)
            continue
        try:
            os.unlink(transfer.sys_path(p))
            stats["removed_files"].append(p.name)
        except OSError as e:
            stats["skipped"].append(p.name)
            _say("warn", f"清理临时文件 {p.name} 失败："
                         f"{transfer.friendly_os_error(e)}")

    if stats["removed_files"]:
        _say("info", f"清理了 {len(stats['removed_files'])} 个搬运临时文件")
    return stats


def summary(stats: dict) -> str:
    """把统计变成一句人话（没有残渣时返回空串）。"""
    parts: list[str] = []
    if stats.get("restored"):
        parts.append(f"回滚 {len(stats['restored'])} 个没做完的目录替换")
    if stats.get("removed_files"):
        parts.append(f"清理 {len(stats['removed_files'])} 个搬运临时文件")
    if stats.get("skipped"):
        parts.append(f"{len(stats['skipped'])} 项未能处理")
    return "，".join(parts)
