"""
cache.py — 缓存管理
------------------------------------------------
清理数据库里的缓存目录，只保留可安全删除的内容：
  - cache/update_packs/  : 下载缓存（重下即可）
  - cache/checkpoints/   : 更新检查点
  - cache/temp/          : 临时文件

特性：
  - 逐文件删除，遇占用文件跳过（不中断整体清理）
  - 启动时可清理孤儿 .part 文件
"""

import os
import shutil
from pathlib import Path

from . import database as db

_CLEANABLE = ("update_packs", "checkpoints", "temp")


def get_cache_root() -> Path | None:
    db_root = db.get_db_path()
    if db_root is None:
        return None
    return db_root / "cache"


def cache_size_bytes() -> int:
    cache = get_cache_root()
    if cache is None or not cache.is_dir():
        return 0
    total = 0
    try:
        for p in cache.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


# ----------------------------------------------------------------------
# 单文件安全删除
# ----------------------------------------------------------------------
def _try_remove_file(path: Path) -> bool:
    """尝试删除单个文件；被占用返回 False"""
    try:
        os.chmod(path, 0o666)  # 去掉只读
    except OSError:
        pass
    try:
        path.unlink()
        return True
    except (PermissionError, OSError):
        return False


def _try_remove_dir(dir_path: Path) -> tuple[int, int, list[str]]:
    """
    递归安全删除目录。
    返回 (成功数, 跳过数, 占用文件列表)。
    """
    ok = 0
    skipped = 0
    locked: list[str] = []

    if not dir_path.exists():
        return 0, 0, []

    # 自底向上：先删文件，再删空目录
    for root, _dirs, files in os.walk(dir_path, topdown=False):
        root_path = Path(root)
        for name in files:
            p = root_path / name
            if _try_remove_file(p):
                ok += 1
            else:
                skipped += 1
                locked.append(str(p))
        for name in _dirs:
            d = root_path / name
            try:
                d.rmdir()
            except OSError:
                pass

    # 尝试删除根目录本身
    try:
        dir_path.rmdir()
    except OSError:
        pass

    return ok, skipped, locked


# ----------------------------------------------------------------------
# 清理
# ----------------------------------------------------------------------
def clean_cache() -> tuple[bool, str]:
    """
    清理缓存目录里白名单内的子目录。
    遇到被占用的文件跳过，不中断清理。
    返回 (成功, 描述)。
    """
    cache = get_cache_root()
    if cache is None:
        return False, "未设置数据库，无法定位缓存"
    if not cache.is_dir():
        return True, "没有缓存"

    total_ok = 0
    total_skip = 0
    all_locked: list[str] = []
    removed_dirs: list[str] = []

    for name in _CLEANABLE:
        target = cache / name
        if not target.exists():
            continue
        ok, skip, locked = _try_remove_dir(target)
        total_ok += ok
        total_skip += skip
        all_locked.extend(locked)
        if ok > 0 or skip == 0:
            removed_dirs.append(name)

    if total_ok == 0 and total_skip == 0:
        return True, "没有需要清理的缓存"

    msg_parts = [f"已清理 {total_ok} 个文件"]
    if removed_dirs:
        msg_parts.append(f"（{', '.join(removed_dirs)}）")
    if total_skip:
        msg_parts.append(f"，跳过 {total_skip} 个被占用的文件")
        # 只列前几个
        sample = all_locked[:3]
        msg_parts.append("：\n" + "\n".join(sample))
        if len(all_locked) > 3:
            msg_parts.append(f"\n…… 还有 {len(all_locked) - 3} 个")

    return True, "".join(msg_parts)


# ----------------------------------------------------------------------
# 启动时清理孤儿 .part
# ----------------------------------------------------------------------
def release_pack_cache(cache_root) -> tuple[bool, int]:
    """
    释放"某个更新包"的下载缓存（更新彻底完成之后调用）。

    缓存里就是本次下载下来的全部文件——留着只对"重试/续传"有用；
    更新已经完成时它只是纯占空间。返回 (是否成功, 释放的字节数)。
    """
    from pathlib import Path as _Path
    root = _Path(cache_root)
    if not root.is_dir():
        return True, 0
    size = 0
    try:
        for p in root.rglob("*"):
            if p.is_file():
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    ok, _skip, _locked = _try_remove_dir(root)
    return bool(ok), size


def clean_orphan_parts() -> int:
    """
    扫描 update_packs 下所有残留分片并尝试删除。
    返回删除成功数。程序启动时调用，处理上次下载中断的残留。

    同时覆盖 `*.part.meta`（旧实现只 glob `*.part`，meta 文件永远清不掉）。
    """
    cache = get_cache_root()
    if cache is None:
        return 0
    update_packs = cache / "update_packs"
    if not update_packs.is_dir():
        return 0

    count = 0
    try:
        for pattern in ("*.part", "*.part.meta"):
            for p in update_packs.rglob(pattern):
                if _try_remove_file(p):
                    count += 1
    except OSError:
        pass
    return count


def find_legacy_trash() -> list[str]:
    """
    找出旧版本（<= v0.3.0）留下的 .pulses_trash 备份目录。

    v0.5.0 起不再使用回收站，但**绝不自动删除**这些目录 —— 里面是用户
    当时被替换/删除的文件。这里只报告路径，由用户自己决定。
    """
    found: list[str] = []
    try:
        from . import recent
        candidates = list(recent.load_recent())
    except Exception:  # noqa: BLE001
        candidates = []
    try:
        db_root = db.get_db_path()
    except Exception:  # noqa: BLE001
        db_root = None
    if db_root is not None:
        candidates.append(Path(db_root))
    for root in candidates:
        try:
            trash = Path(root).parent / ".pulses_trash"
            if trash.is_dir() and str(trash) not in found:
                found.append(str(trash))
        except Exception:  # noqa: BLE001, S112
            continue
    return found


def work_root() -> Path | None:
    """更新流程的工作目录根：<db>/cache/temp（受 clean_cache 覆盖）。"""
    db_root = db.get_db_path()
    if db_root is None:
        return None
    return db_root / "cache" / "temp"


def clean_orphan_workdirs(prefix: str = "pulses_play_") -> int:
    """
    启动时清理上次遗留的解压/合并工作目录。

    进程启动时不可能有正在运行的更新，所以 cache/temp 下的
    pulses_play_* 全是孤儿，直接删掉（避免每次更新残留一整份副本）。
    """
    root = work_root() or (get_cache_root() or Path()) / "temp"
    if not root.is_dir():
        return 0
    removed = 0
    try:
        for child in root.iterdir():
            if not child.is_dir() or not child.name.startswith(prefix):
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    except OSError:
        pass
    return removed