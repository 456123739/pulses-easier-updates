"""
dbmigrate.py — 数据库（项目库）位置切换与迁移
------------------------------------------------
玩家的数据库默认在「首次运行向导」选的目录里，之后就没法改了 ——
本模块补上"换位置"和"把数据搬过去"。

一个数据库目录里有什么：
    config.json     配置（白名单/黑名单/下载参数/界面参数…）
    projects/       项目（整合包）数据
    cache/          下载缓存、检查点、临时目录 —— **可重建**，默认不迁移

三条硬规则：
  1. **绝不删除原目录**。迁移只是"复制到新位置 + 改指向"，旧数据原样留着，
     玩家自己确认没问题后再手动删。
  2. **先复制、后校验、最后才切换**。中途失败（磁盘满/权限/被杀进程）
     不会切过去，标记文件 `<目标>/.pulses_migrate` 会留下痕迹。
  3. 目标已存在同名文件时**覆盖**（迁移语义），但目标里多出来的文件保留。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import database as db
from .transfer import friendly_os_error, sys_path

# 迁移进度标记：存在 = 上次迁移没跑完
MARKER = ".pulses_migrate"
# 默认不搬的目录（可重建，往往还很大）
DEFAULT_SKIP = ("cache",)


def _human(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}TB"


def describe(path) -> dict:
    """看一眼某个目录像不像数据库（大小、项目数、是否有效）。"""
    root = Path(path)
    info = {"path": str(root), "exists": root.is_dir(), "valid": False,
            "files": 0, "bytes": 0, "projects": 0, "cache_bytes": 0}
    if not info["exists"]:
        return info
    info["valid"] = db.is_valid_database(root)
    try:
        projects = root / "projects"
        if projects.is_dir():
            info["projects"] = sum(1 for p in projects.iterdir())
        cache = root / "cache"
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            info["files"] += 1
            info["bytes"] += size
            if cache.is_dir() and cache in p.parents:
                info["cache_bytes"] += size
    except OSError:
        pass
    return info


def iter_source_files(src: Path, skip: tuple[str, ...] = DEFAULT_SKIP):
    """列出要迁移的文件（相对路径）。跳过 skip 里的顶层目录和标记文件。"""
    root = Path(src)
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in skip:
            continue
        if rel.name == MARKER or rel.name.endswith(".tmp"):
            continue
        yield rel


def iter_source_dirs(src: Path, skip: tuple[str, ...] = DEFAULT_SKIP):
    """列出要一起创建的目录（含空目录 —— 新建的库 projects/ 就是空的）。"""
    root = Path(src)
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_dir():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in skip:
            continue
        yield rel


def plan(src, dst, skip: tuple[str, ...] = DEFAULT_SKIP) -> dict:
    """迁移前预览：搬多少、哪些会覆盖、目标是空/有效库。"""
    src_root, dst_root = Path(src), Path(dst)
    files = list(iter_source_files(src_root, skip))
    total = 0
    conflicts: list[str] = []
    for rel in files:
        try:
            total += (src_root / rel).stat().st_size
        except OSError:
            continue
        if (dst_root / rel).exists():
            conflicts.append(rel.as_posix())
    target = describe(dst_root)
    dirs = list(iter_source_dirs(src_root, skip))
    return {"dirs": len(dirs), "files": len(files), "bytes": total,
            "conflicts": conflicts,
            "target_valid": target["valid"], "target_exists": target["exists"],
            "target_is_same": _same_path(src_root, dst_root),
            "skipped_dirs": list(skip)}


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except Exception:  # noqa: BLE001
        return False


def verify(src, dst, skip: tuple[str, ...] = DEFAULT_SKIP) -> list[dict]:
    """逐文件核对大小；返回不一致清单（空 = 一致）。"""
    src_root, dst_root = Path(src), Path(dst)
    bad: list[dict] = []
    for rel in iter_source_files(src_root, skip):
        s = src_root / rel
        d = dst_root / rel
        try:
            s_size = s.stat().st_size
        except OSError as e:
            bad.append({"rel": rel.as_posix(), "error": f"读源失败：{e}"})
            continue
        try:
            if not d.is_file():
                bad.append({"rel": rel.as_posix(), "error": "缺失"})
            elif d.stat().st_size != s_size:
                bad.append({"rel": rel.as_posix(), "error": "大小不一致"})
        except OSError as e:
            bad.append({"rel": rel.as_posix(), "error": str(e)})
    return bad


def migrate(src, dst, *, skip: tuple[str, ...] = DEFAULT_SKIP,
            progress=None, should_abort=None) -> dict:
    """
    把数据库从 src 复制到 dst（覆盖同名、保留目标里多出来的文件）。

    返回 {"ok", "copied", "bytes", "failed", "verified", "msg"}。
    **不会**自动切换指向，也**不会**动源目录 —— 由调用方决定。
    """
    import errno
    src_root, dst_root = Path(src), Path(dst)
    result = {"ok": False, "copied": 0, "bytes": 0, "failed": [],
              "verified": [], "msg": ""}

    if not src_root.is_dir():
        result["msg"] = "原数据库目录不存在"
        return result
    if _same_path(src_root, dst_root):
        result["msg"] = "新位置和当前位置是同一个目录"
        return result
    if not db.is_valid_database(src_root):
        result["msg"] = "原目录不是有效的数据库（缺 config.json 或 projects/）"
        return result

    files = list(iter_source_files(src_root, skip))
    total_bytes = 0
    for rel in files:
        try:
            total_bytes += (src_root / rel).stat().st_size
        except OSError:
            pass

    try:
        dst_root.mkdir(parents=True, exist_ok=True)
        (dst_root / MARKER).write_text(
            f"migration started {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"from {src_root}\n", encoding="utf-8")
    except OSError as e:
        result["msg"] = friendly_os_error(e, "无法创建目标目录")
        return result

    # 先建目录结构（空目录也要建）
    for rel in iter_source_dirs(src_root, skip):
        try:
            (dst_root / rel).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            result["failed"].append({"rel": rel.as_posix(),
                                     "error": friendly_os_error(e)})
    if result["failed"]:
        result["msg"] = "无法创建目标目录结构"
        return result

    for done, rel in enumerate(files):
        if should_abort is not None and should_abort():
            result["msg"] = f"已取消（已迁移 {done} 个文件）"
            return result
        s = src_root / rel
        d = dst_root / rel
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sys_path(s), sys_path(d))
            size = d.stat().st_size
            result["copied"] += 1
            result["bytes"] += size
        except OSError as e:
            if getattr(e, "errno", None) == errno.ENOSPC:
                result["failed"].append({"rel": rel.as_posix(),
                                         "error": "磁盘空间不足"})
                result["msg"] = "目标磁盘空间不足，已中止（原数据未受影响）"
                return result
            result["failed"].append({"rel": rel.as_posix(),
                                     "error": friendly_os_error(e)})
        if progress is not None:
            try:
                progress(done + 1, len(files), result["bytes"], total_bytes)
            except Exception:  # noqa: BLE001, S110
                pass

    if result["failed"]:
        result["msg"] = f"{len(result['failed'])} 个文件复制失败"
        return result

    result["verified"] = verify(src_root, dst_root, skip)
    if result["verified"]:
        result["msg"] = f"{len(result['verified'])} 个文件校验不一致"
        return result
    if not db.is_valid_database(dst_root):
        result["msg"] = "复制完成，但目标目录不构成有效数据库"
        return result

    try:
        (dst_root / MARKER).unlink()
    except OSError:
        pass
    result["ok"] = True
    result["msg"] = (f"已迁移 {result['copied']} 个文件 / "
                     f"{_human(result['bytes'])}")
    return result


def create_at(path) -> tuple[bool, str]:
    """在指定位置新建一个空数据库（不迁移任何东西）。"""
    root = Path(path)
    try:
        ok = db.create_database(root)
    except Exception as e:  # noqa: BLE001
        return False, f"创建失败：{e}"
    if not ok:
        return False, "创建失败（没有写权限或路径非法）"
    return True, f"已在 {root} 新建空数据库"


def switch_to(path) -> tuple[bool, str]:
    """
    把程序指向某个数据库目录，并清掉会缓存旧路径的运行时状态。

    注意：**只改指向**，不动任何文件；原数据库原地保留。
    """
    root = Path(path)
    if not root.is_dir():
        return False, "目录不存在"
    if not db.is_valid_database(root):
        return False, "这个目录不是有效的数据库（缺 config.json 或 projects/）"
    if (root / MARKER).is_file():
        return False, "这个目录里还有没迁移完的标记，请重新迁移后再切换"
    try:
        db.set_db_path(root)
    except Exception as e:  # noqa: BLE001
        return False, f"写入配置失败：{e}"
    # 路径相关的缓存要失效，否则界面还在用旧的
    try:
        from . import downloader as dl_mod
        dl_mod.invalidate_options()
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        from . import cache as cache_mod
        cache_mod.clear_disk_cache()
    except Exception:  # noqa: BLE001, S110
        pass
    return True, f"数据库已切换到 {root}"


def has_incomplete_marker(path) -> bool:
    return (Path(path) / MARKER).is_file()


def human_size(n: int) -> str:
    return _human(n)
