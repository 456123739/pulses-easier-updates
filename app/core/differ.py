"""
differ.py — 新旧版本差异比对
------------------------------------------------
数据源模型：
  index 和 overrides 是**互补**的：
    - 有下载链接的文件 → 在 index 里
    - 下不到的文件   → 在 overrides 里
  新版本某文件夹里的文件清单 = index 里该文件夹直接文件
                              ∪ overrides 里该文件夹直接文件

输出粒度：
  白名单文件夹：单层比对
    - 直接子文件：文件级 Change（is_folder_level=False）
    - 直接子文件夹：文件夹级 Change（is_folder_level=True）
  非白名单文件夹：整体文件夹级 Change
  只在新包有 → ADDED
  只在旧包有 → DELETED（仅当"更新包里有这个顶层文件夹"时输出；
                       顶层文件夹不在更新包里的，不输出）
  双侧都有且不同 → MODIFIED

根目录散文件：
  只在新包有 → ADDED
  双侧都有 → MODIFIED
  只在旧包有 → 保留（玩家本地配置，不输出 DELETED）

算法一致：
  - index 声明 sha1 → 本地也算 sha1
  - index 声明 sha256 → 本地也算 sha256
  - index 没声明 → 两侧都 sha1
"""

import hashlib
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ChangeKind
from ..utils.files import iter_files

DEFAULT_WHITELIST = ["mods"]


@dataclass
class Change:
    rel_path: Path
    kind: ChangeKind
    is_folder_level: bool = False  # True=文件夹级比对，False=文件级比对


@dataclass
class DiffResult:
    added: list[Change] = field(default_factory=list)
    modified: list[Change] = field(default_factory=list)
    deleted: list[Change] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


# ----------------------------------------------------------------------
# 哈希工具
# ----------------------------------------------------------------------
def _hash_file(path: Path, algo: str = "sha1") -> str:
    try:
        h = hashlib.new(algo)
    except Exception:  # noqa: BLE001
        return ""
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def folder_hash(folder: Path) -> str:
    """文件夹级哈希：相对路径 + 大小 + mtime 拼接后 SHA-256，不读内容"""
    entries: list[str] = []
    try:
        for dirpath, _dirnames, filenames in os.walk(folder):
            for name in filenames:
                abs_path = Path(dirpath) / name
                try:
                    rel = abs_path.relative_to(folder).as_posix()
                    st = abs_path.stat()
                    entries.append(f"{rel}|{st.st_size}|{int(st.st_mtime)}")
                except OSError:
                    continue
    except OSError:
        return ""
    h = hashlib.sha256()
    for line in sorted(entries):
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _pick_hash(hashes: dict | None) -> tuple[str | None, str | None]:
    """从 index 哈希里挑一个：优先 sha1（更快）"""
    if not isinstance(hashes, dict):
        return None, None
    if hashes.get("sha1"):
        return "sha1", hashes["sha1"]
    if hashes.get("sha256"):
        return "sha256", hashes["sha256"]
    if hashes.get("sha512"):
        return "sha512", hashes["sha512"]
    return None, None


def _list_entries(folder: Path | None
                  ) -> tuple[dict[str, Path], dict[str, Path]]:
    """列出文件夹直接子项：({文件名: 路径}, {子目录名: 路径})"""
    files: dict[str, Path] = {}
    dirs: dict[str, Path] = {}
    if folder is None or not folder.is_dir():
        return files, dirs
    try:
        for entry in folder.iterdir():
            if entry.is_file():
                files[entry.name] = entry
            elif entry.is_dir():
                dirs[entry.name] = entry
    except OSError:
        pass
    return files, dirs


# ----------------------------------------------------------------------
# index 哈希表预处理：按顶层文件夹切分 + 收集深层目录
# ----------------------------------------------------------------------
def _split_index_by_top(
    index_hashes: dict[str, dict],
) -> tuple[dict[str, dict[str, dict]], dict[str, set[str]]]:
    """
    预遍历 index_hashes 一次，产出：
      - by_top: {top_name: {rel_path: hashes}}
      - deep_dirs: {top_name: {相对 top 的目录路径集合}}
    """
    by_top: dict[str, dict[str, dict]] = {}
    deep_dirs: dict[str, set[str]] = {}

    for rel, hashes in index_hashes.items():
        if "/" not in rel:
            continue
        top, _, sub = rel.partition("/")
        if not top or not sub:
            continue
        by_top.setdefault(top, {})[rel] = hashes

        parts = sub.split("/")
        if len(parts) >= 2:
            acc = ""
            dirs = deep_dirs.setdefault(top, set())
            for seg in parts[:-1]:
                acc = seg if not acc else acc + "/" + seg
                dirs.add(acc)

    return by_top, deep_dirs


# ----------------------------------------------------------------------
# 白名单文件夹比对（单层）
# ----------------------------------------------------------------------
def _diff_whitelist_dir(
    old_dir: Path | None,
    new_dir: Path | None,
    top_name: str,
    index_by_top: dict[str, dict[str, dict]],
    index_deep_dirs: set[str],
    threads: int,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Change], list[Change], list[Change]]:
    """
    对白名单文件夹做单层比对，返回 (added, modified, deleted)。
    """
    added: list[Change] = []
    modified: list[Change] = []
    deleted: list[Change] = []

    old_files, old_dirs = _list_entries(old_dir)
    new_files, new_dirs = _list_entries(new_dir)

    prefix = top_name + "/"
    index_direct: dict[str, dict] = {}
    for rel, hashes in index_by_top.get(top_name, {}).items():
        if not rel.startswith(prefix):
            continue
        sub = rel[len(prefix):]
        if "/" in sub:
            continue
        index_direct[sub] = hashes

    index_dirs: set[str] = set()
    for d in index_deep_dirs:
        first = d.split("/", 1)[0]
        if first:
            index_dirs.add(first)

    new_file_names = set(new_files.keys()) | set(index_direct.keys())
    old_file_names = set(old_files.keys())

    to_compare: list[tuple[str, Path, str, str]] = []

    for name in sorted(new_file_names):
        full_rel = top_name + "/" + name
        local = old_files.get(name)

        target_h: str | None = None
        algo = "sha1"

        idx_entry = index_direct.get(name)
        if idx_entry is not None:
            algo_h, hash_v = _pick_hash(idx_entry)
            if hash_v:
                target_h = hash_v
                algo = algo_h or "sha1"

        if target_h is None:
            src = new_files.get(name)
            if src is not None:
                target_h = _hash_file(src, "sha1")
                algo = "sha1"

        if not target_h:
            if local is None:
                added.append(Change(Path(full_rel), ChangeKind.ADDED))
            else:
                modified.append(Change(Path(full_rel),
                                       ChangeKind.MODIFIED))
            continue

        if local is None:
            added.append(Change(Path(full_rel), ChangeKind.ADDED))
        else:
            to_compare.append((full_rel, local, target_h, algo))

    for name in sorted(old_file_names - new_file_names):
        full_rel = top_name + "/" + name
        deleted.append(Change(Path(full_rel), ChangeKind.DELETED))

    if to_compare:
        total = len(to_compare)
        counter = {"n": 0}
        lock = threading.Lock()

        def _one(item):
            full_rel, path, target_h, algo = item
            local_h = _hash_file(path, algo)
            return full_rel, local_h, target_h

        with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
            futures: list[Future] = [pool.submit(_one, it)
                                     for it in to_compare]
            for fut in futures:
                try:
                    full_rel, local_h, target_h = fut.result()
                except Exception:  # noqa: BLE001
                    continue

                with lock:
                    counter["n"] += 1
                    d = counter["n"]
                if progress:
                    try:
                        progress(d, total)
                    except Exception:  # noqa: BLE001, S110
                        pass

                if local_h and local_h != target_h:
                    modified.append(
                        Change(Path(full_rel), ChangeKind.MODIFIED))

    effective_new_dirs = set(new_dirs.keys()) | index_dirs
    all_subdirs = set(old_dirs) | effective_new_dirs

    for sub in sorted(all_subdirs):
        old_sub = old_dirs.get(sub)
        new_sub = new_dirs.get(sub)
        full_rel = top_name + "/" + sub

        if sub in index_dirs and new_sub is None:
            if old_sub is None:
                added.append(Change(Path(full_rel), ChangeKind.ADDED,
                                    is_folder_level=True))
            continue

        if new_sub and not old_sub:
            added.append(Change(Path(full_rel), ChangeKind.ADDED,
                                is_folder_level=True))
        elif old_sub and not new_sub:
            deleted.append(Change(Path(full_rel), ChangeKind.DELETED,
                                  is_folder_level=True))
        else:
            old_fh = folder_hash(old_sub) if old_sub else ""
            new_fh = folder_hash(new_sub) if new_sub else ""
            if old_fh != new_fh:
                modified.append(Change(Path(full_rel), ChangeKind.MODIFIED,
                                       is_folder_level=True))

    return added, modified, deleted


# ----------------------------------------------------------------------
# 目录枚举
# ----------------------------------------------------------------------
def _top_dirs(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not root.is_dir():
        return result
    try:
        for entry in root.iterdir():
            if entry.is_dir():
                result[entry.name] = entry
    except OSError:
        pass
    return result


def _root_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    result: list[Path] = []
    try:
        for entry in root.iterdir():
            if entry.is_file():
                result.append(Path(entry.name))
    except OSError:
        pass
    return result


# ----------------------------------------------------------------------
# 主比对
# ----------------------------------------------------------------------
def diff_packs_parallel(
    old_root: Path,
    new_root: Path,
    whitelist: list[str] | None = None,
    index_hashes: dict[str, dict] | None = None,
    threads: int = 16,
    progress: Callable[[int, int, str], None] | None = None,
) -> DiffResult:
    old_root, new_root = Path(old_root), Path(new_root)
    wl = list(whitelist) if whitelist else list(DEFAULT_WHITELIST)
    wl_top = {p.split("/", 1)[0] for p in wl if p}
    idx = index_hashes or {}

    index_by_top, index_deep_dirs = _split_index_by_top(idx)

    def _p(done: int, total: int, phase: str):
        if progress:
            try:
                progress(done, total, phase)
            except Exception:  # noqa: BLE001, S110
                pass

    old_dirs = _top_dirs(old_root)
    new_dirs = _top_dirs(new_root)
    all_names = sorted(set(old_dirs) | set(new_dirs) | set(index_by_top))
    total_dirs = len(all_names)

    result = DiffResult()

    for i, name in enumerate(all_names):
        has_old = name in old_dirs
        has_new = name in new_dirs
        old_dir = old_dirs.get(name)
        new_dir = new_dirs.get(name)
        has_index = name in index_by_top

        if not has_old and (has_new or has_index):
            if name in wl_top:
                idx_files = index_by_top.get(name, {})
                if idx_files:
                    for rel in sorted(idx_files.keys()):
                        result.added.append(
                            Change(Path(rel), ChangeKind.ADDED,
                                   is_folder_level=False))
                if new_dir:
                    try:
                        existing = set()
                        if idx_files:
                            for rel in idx_files:
                                sub = rel[len(name) + 1:]
                                existing.add(sub)
                        for rel in iter_files(new_dir):
                            rel_str = rel.as_posix()
                            if rel_str in existing:
                                continue
                            result.added.append(
                                Change(Path(name) / rel, ChangeKind.ADDED,
                                       is_folder_level=False))
                    except Exception:  # noqa: BLE001
                        if not idx_files:
                            result.added.append(
                                Change(Path(name), ChangeKind.ADDED,
                                       is_folder_level=True))
            else:
                result.added.append(Change(Path(name), ChangeKind.ADDED,
                                           is_folder_level=True))
            _p(i + 1, max(1, total_dirs), "merge")
            continue

        if has_old and not has_new and not has_index:
            _p(i + 1, max(1, total_dirs), "merge")
            continue

        if name in wl_top:
            added, modified, deleted = _diff_whitelist_dir(
                old_dir, new_dir, name,
                index_by_top, index_deep_dirs.get(name, set()),
                threads)
            result.added.extend(added)
            result.modified.extend(modified)
            result.deleted.extend(deleted)
        else:
            old_fh = folder_hash(old_dir) if old_dir else ""
            new_fh = folder_hash(new_dir) if new_dir else ""
            if old_fh != new_fh:
                result.modified.append(
                    Change(Path(name), ChangeKind.MODIFIED,
                           is_folder_level=True))

        _p(i + 1, max(1, total_dirs), "merge")

    old_files = set(_root_files(old_root))
    new_files = set(_root_files(new_root))
    for rel in sorted(new_files - old_files):
        result.added.append(Change(rel, ChangeKind.ADDED,
                                   is_folder_level=False))
    for rel in sorted(old_files & new_files):
        result.modified.append(Change(rel, ChangeKind.MODIFIED,
                                      is_folder_level=False))

    return result


def diff_packs(old_root: Path, new_root: Path,
               whitelist: list[str] | None = None,
               index_hashes: dict[str, dict] | None = None,
               progress: Callable[[int, int, str], None] | None = None
               ) -> DiffResult:
    return diff_packs_parallel(old_root, new_root,
                               whitelist=whitelist,
                               index_hashes=index_hashes,
                               threads=1, progress=progress)