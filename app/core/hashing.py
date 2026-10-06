"""
hashing.py — 文件级 / 文件夹级哈希
------------------------------------------------
文件级哈希策略：
  - index 已有 sha256/sha1/sha512 → 直接用，不重算
  - 否则计算本地 BLAKE2b（128 位）快速哈希
文件夹级哈希：
  - 对文件夹内所有文件的 相对路径|大小|mtime 拼接后 SHA-256（不读内容）

支持 exclude_tops：构建清单时跳过这些顶层目录（用于"最小体积"模式）。
"""

import hashlib
import os
from pathlib import Path

BLAKE2B_SIZE = 16   # 128 位


# ----------------------------------------------------------------------
# 单个文件哈希
# ----------------------------------------------------------------------
def blake2b_of_file(path: Path, chunk_size: int = 1 << 20) -> str | None:
    """本地快速哈希：128 位 BLAKE2b"""
    try:
        h = hashlib.blake2b(digest_size=BLAKE2B_SIZE)
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def file_hash(path: Path) -> tuple[str, str] | None:
    """返回 (algo, value)，失败 None。当前固定用 BLAKE2b。"""
    value = blake2b_of_file(path)
    if value is None:
        return None
    return ("blake2b", value)


# ----------------------------------------------------------------------
# 文件夹级哈希
# ----------------------------------------------------------------------
def folder_hash(folder: Path) -> str | None:
    """
    文件夹内容哈希：相对路径 + 大小 + 每个文件的内容哈希。

    与 differ.folder_hash 使用**同一算法**，玩家端可以直接拿这里记录的
    值与本地目录比较（省掉一半读取）。
    刻意不含 mtime —— 更新包解压后 mtime 会全变，含 mtime 必然误判。
    """
    folder = Path(folder)
    if not folder.is_dir():
        return None
    from .differ import folder_hash as _content_folder_hash
    value = _content_folder_hash(folder, content=True)
    return value or None


# ----------------------------------------------------------------------
# 哈希清单
# ----------------------------------------------------------------------
def build_hash_manifest(
    root: Path,
    whitelist: list[str],
    index_hashes: dict[str, dict] | None = None,
    exclude_tops: list[str] | None = None,
) -> dict:
    """
    构建哈希清单。

    参数
    ----
    root           整合包根目录（通常是 overrides/）
    whitelist      需要文件级哈希的路径列表（可含子目录）
    index_hashes   modrinth.index.json 里的哈希，优先使用
    exclude_tops   跳过的顶层目录名（最小体积模式下排除的文件夹）

    返回
    ----
    {
      "files": {rel_path: {"algo": "...", "value": "..."}, ...},
      "folders": {top_folder: "sha256...", ...},
      "whitelist": [...],
    }
    """
    root = Path(root)
    index_hashes = index_hashes or {}
    exclude_set = set(exclude_tops or [])

    # 归一化白名单
    wl: list[str] = []
    for item in whitelist:
        s = str(item).strip().replace("\\", "/").strip("/")
        if s:
            wl.append(s)

    files: dict[str, dict] = {}
    covered_tops: set[str] = set()

    # 1) 白名单路径 → 文件级哈希
    for rel in wl:
        top = rel.split("/", 1)[0]
        if top in exclude_set:
            # 被排除的顶层目录：整支跳过
            continue
        target = root / rel
        if not target.exists():
            continue
        covered_tops.add(top)

        if target.is_file():
            _add_file_entry(target, rel, files, index_hashes)
        elif target.is_dir():
            for dirpath, _dirnames, filenames in os.walk(target):
                for fname in filenames:
                    abs_path = Path(dirpath) / fname
                    try:
                        rel_path = abs_path.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    _add_file_entry(abs_path, rel_path,
                                    files, index_hashes)

    # 2) 非白名单顶层文件夹 → 文件夹级哈希
    folders: dict[str, str] = {}
    try:
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in covered_tops:
                continue
            if entry.name in exclude_set:
                continue
            h = folder_hash(entry)
            if h:
                folders[entry.name] = h
    except OSError:
        pass

    return {
        "files": files,
        "folders": folders,
        "whitelist": wl,
    }


def _add_file_entry(abs_path: Path, rel_path: str,
                    files: dict, index_hashes: dict):
    """为单个文件写入清单条目。index 哈希优先，否则 BLAKE2b"""
    idx = index_hashes.get(rel_path)
    if isinstance(idx, dict):
        for algo in ("sha256", "sha512", "sha1"):
            v = idx.get(algo)
            if isinstance(v, str) and v:
                files[rel_path] = {"algo": algo, "value": v}
                return

    result = file_hash(abs_path)
    if result is not None:
        algo, value = result
        files[rel_path] = {"algo": algo, "value": value}