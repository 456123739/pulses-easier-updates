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

from ..config import ChangeKind, is_local_only_dir, is_local_only_name
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
BLAKE2B_DIGEST_SIZE = 16      # 与 hashing.py 保持一致（128 位）


def _new_hasher(algo: str):
    """按算法名创建哈希对象；blake2b 固定 128 位以兼容导出包清单。"""
    if algo == "blake2b":
        return hashlib.blake2b(digest_size=BLAKE2B_DIGEST_SIZE)
    return hashlib.new(algo)


def _hash_file(path: Path, algo: str = "sha1") -> str:
    try:
        h = _new_hasher(algo)
    except Exception:  # noqa: BLE001
        return ""
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _folder_manifest(folder: Path | None) -> tuple[list[tuple[str, int]], int]:
    """
    遍历目录，返回 ([(相对路径, 大小)], 总字节)。

    不存在/不可读 → ([], 0)。
    """
    entries: list[tuple[str, int]] = []
    total = 0
    if folder is None:
        return entries, total
    folder = Path(folder)
    if not folder.is_dir():
        return entries, total
    try:
        for dirpath, _dirnames, filenames in os.walk(folder):
            for name in filenames:
                abs_path = Path(dirpath) / name
                try:
                    rel = abs_path.relative_to(folder).as_posix()
                    size = abs_path.stat().st_size
                except OSError:
                    continue
                entries.append((rel, size))
                total += size
    except OSError:
        return [], 0
    entries.sort()
    return entries, total


# 超过这个体积就只做"路径+大小"比较（避免为了确认一致性读入 GB 级内容）
_FOLDER_DEEP_MAX_BYTES = 256 * 1024 * 1024


def folder_hash(folder: Path | None, content: bool = True) -> str:
    """
    文件夹级哈希：相对路径 + 大小（+ 内容 SHA-1）。

    content=False 时只算路径与大小（不读内容，快）。
    **刻意不包含 mtime**：更新包是现解压出来的，zip 不还原 mtime，
    一旦把 mtime 算进摘要，内容完全相同的目录也会被判定为
    「已修改」，进而触发整目录替换（历史上导致过玩家本地文件被清空）。
    """
    entries, _total = _folder_manifest(folder)
    h = hashlib.sha256()
    for rel, size in entries:
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(size).encode("ascii"))
        if content and folder is not None:
            h.update(b"\x00")
            h.update(_hash_file(Path(folder) / rel, "sha1").encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def folders_differ(a: Path | None, b: Path | None) -> bool:
    """
    两个目录的**内容**是否不同。

    两级判定：
      1) 先用「相对路径 + 大小」排除明显差异（廉价，不读内容）；
      2) 相同再逐个文件比内容哈希（消除"同名同大小但内容不同"
         漏判，同时不受 mtime 影响）。
    目录总体积超过 _FOLDER_DEEP_MAX_BYTES 时只做第 1 级。
    """
    ea, ta = _folder_manifest(a)
    eb, tb = _folder_manifest(b)
    if not ea and not eb:
        return False
    if ea != eb:
        return True
    if ta > _FOLDER_DEEP_MAX_BYTES or tb > _FOLDER_DEEP_MAX_BYTES:
        return False
    return folder_hash(a, content=True) != folder_hash(b, content=True)


def _pick_hash(hashes: dict | None) -> tuple[str | None, str | None]:
    """从 index 哈希里挑一个：优先 sha1（更快），最后才是 blake2b"""
    if not isinstance(hashes, dict):
        return None, None
    for algo in ("sha1", "sha256", "sha512", "blake2b"):
        v = hashes.get(algo)
        if isinstance(v, str) and v:
            return algo, v
    return None, None


def _list_entries(folder: Path | None, recursive: bool = False
                  ) -> tuple[dict[str, Path], dict[str, Path]]:
    """
    列出文件夹里的文件与子目录。

    recursive=False：只列直接子项，键是名字；
    recursive=True ：递归列出所有文件，键是相对路径（a/b.jar）。
    """
    files: dict[str, Path] = {}
    dirs: dict[str, Path] = {}
    if folder is None or not folder.is_dir():
        return files, dirs
    if recursive:
        try:
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = [d for d in dirnames if not is_local_only_dir(d)]
                for name in filenames:
                    if is_local_only_name(name):
                        continue
                    p = Path(dirpath) / name
                    try:
                        rel = p.relative_to(folder).as_posix()
                    except OSError:
                        continue
                    files[rel] = p
        except OSError:
            return files, dirs
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
) -> tuple[dict[str, dict[str, dict]], dict[str, set[str]],
           dict[str, dict]]:
    """
    预遍历 index_hashes 一次，产出：
      - by_top: {top_name: {rel_path: hashes}}
      - deep_dirs: {top_name: {相对 top 的目录路径集合}}
      - root_files: {文件名: hashes}   ← 根目录散文件（旧实现直接丢弃，
        导致根文件只能"按存在性"判断，内容改了也不一定能发现）
    """
    by_top: dict[str, dict[str, dict]] = {}
    deep_dirs: dict[str, set[str]] = {}
    root_files: dict[str, dict] = {}

    for rel, hashes in index_hashes.items():
        if "/" not in rel:
            if rel:
                root_files[rel] = hashes
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

    return by_top, deep_dirs, root_files


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

    # 白名单目录一律**递归到文件级**：和清单比对的粒度保持一致，
    # 否则同一份包"有 ea_hashes / 没有 ea_hashes"会得到两种变更粒度
    #（子目录整体 vs 单文件），「跳过重名」的语义也会跟着变。
    old_files, _old_dirs = _list_entries(old_dir, recursive=True)
    new_files, _new_dirs = _list_entries(new_dir, recursive=True)

    prefix = top_name + "/"
    # 递归比较后，index 里**任意深度**的文件都要算进"新包这一侧"
    # （以前只收直接子文件，深层 index 文件会被当成"包里没有"而误删本地副本）
    index_direct: dict[str, dict] = {}
    for rel, hashes in index_by_top.get(top_name, {}).items():
        if not rel.startswith(prefix):
            continue
        index_direct[rel[len(prefix):]] = hashes

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
                except Exception:  # noqa: BLE001, S112
                    continue

                with lock:
                    counter["n"] += 1
                    d = counter["n"]
                if progress:
                    try:
                        progress(d, total)
                    except Exception:  # noqa: BLE001, S110
                        pass

                if not local_h or local_h != target_h:
                    # local_h 为空 = 本地文件读不出来（被占用/没权限）。
                    # 以前这里会静默判"一致"，那个文件就永远停在旧版本。
                    modified.append(
                        Change(Path(full_rel), ChangeKind.MODIFIED))

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
            if entry.is_dir() and not is_local_only_dir(entry.name):
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
            # 本地专属文件（更新未完成凭证等）永不参与比对
            if entry.is_file() and not is_local_only_name(entry.name):
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
    folder_hashes: dict[str, str] | None = None,
) -> DiffResult:
    """
    folder_hashes：更新包 `ea_hashes.json` 里记录的**顶层文件夹内容哈希**
    （由开发者端用同一算法算出）。命中时只需算本地一侧的哈希即可判定，
    省掉一半读取。
    """
    old_root, new_root = Path(old_root), Path(new_root)
    wl = list(whitelist) if whitelist else list(DEFAULT_WHITELIST)
    wl_top = {p.split("/", 1)[0] for p in wl if p}
    idx = index_hashes or {}
    f_hashes = folder_hashes or {}

    index_by_top, index_deep_dirs, index_root_files = \
        _split_index_by_top(idx)

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
            def _sub(done: int, total: int, _i=i) -> None:
                # 把"这个目录里已比对的文件数"折算成整体进度，避免进度条
                # 在几百个 mod 的哈希阶段一动不动（看起来像卡死）
                if progress:
                    try:
                        progress(_i + (done / max(1, total)),
                                 max(1, total_dirs), "merge")
                    except Exception:  # noqa: BLE001, S110
                        pass

            added, modified, deleted = _diff_whitelist_dir(
                old_dir, new_dir, name,
                index_by_top, index_deep_dirs.get(name, set()),
                threads, progress=_sub if progress else None)
            result.added.extend(added)
            result.modified.extend(modified)
            result.deleted.extend(deleted)
        else:
            recorded = f_hashes.get(name, "")
            if recorded:
                # 极少数情况：记录值算的是"新包侧"，直接和本地侧比
                same = bool(old_dir) and folder_hash(old_dir) == recorded
                if not same:
                    result.modified.append(
                        Change(Path(name), ChangeKind.MODIFIED,
                               is_folder_level=True))
            elif folders_differ(old_dir, new_dir):
                result.modified.append(
                    Change(Path(name), ChangeKind.MODIFIED,
                           is_folder_level=True))

        _p(i + 1, max(1, total_dirs), "merge")

    # 根目录散文件：旧实现把"两侧都有"一律判 MODIFIED（从不比内容），
    # 于是每次都会重写 options.txt 之类的文件。现在按内容比对：
    # index 里声明过哈希就用它，否则两侧各算一次 sha1。
    old_files = set(_root_files(old_root))
    new_files = set(_root_files(new_root))
    for rel in sorted(new_files - old_files):
        result.added.append(Change(rel, ChangeKind.ADDED,
                                   is_folder_level=False))
    for rel in sorted(old_files & new_files):
        name = rel.name
        target_h: str | None = None
        algo = "sha1"
        idx_entry = index_root_files.get(name)
        if idx_entry is not None:
            algo_h, hash_v = _pick_hash(idx_entry)
            if hash_v:
                target_h, algo = hash_v, (algo_h or "sha1")
        if not target_h:
            target_h = _hash_file(new_root / rel, "sha1")
            algo = "sha1"
        if not target_h:
            # 算不出目标哈希（读失败）：保守判"需更新"
            result.modified.append(Change(rel, ChangeKind.MODIFIED,
                                          is_folder_level=False))
            continue
        local_h = _hash_file(old_root / rel, algo)
        if not local_h or local_h != target_h:
            result.modified.append(Change(rel, ChangeKind.MODIFIED,
                                          is_folder_level=False))

    return result



# ----------------------------------------------------------------------
# 清单级比对（不需要新包已解压）
# ----------------------------------------------------------------------
def _walk_rel_files(root: Path) -> dict[str, Path]:
    """递归列出本地文件（相对路径 → 绝对路径），跳过本地专属文件。"""
    out: dict[str, Path] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_local_only_dir(d)]
        for name in filenames:
            if is_local_only_name(name):
                continue
            p = Path(dirpath) / name
            try:
                rel = p.relative_to(root).as_posix()
            except OSError:
                continue
            out[rel] = p
    return out


def diff_from_manifest(
    old_root: Path,
    manifest_files: dict[str, dict],
    index_hashes: dict[str, dict] | None = None,
    folder_hashes: dict[str, str] | None = None,
    whitelist: list[str] | None = None,
    compare_root: Path | None = None,
    pack_tops: set[str] | None = None,
    partial_tops: set[str] | None = None,
    pack_files: set[str] | None = None,
    threads: int = 16,
    progress: Callable[[int, int, str], None] | None = None,
) -> DiffResult | None:
    """
    只用**更新包自带的哈希清单**比对，不必等解压完成。

    清单来自 `.eapack` 里的 `ea_hashes.json`（导出时算好的每个文件哈希）：
      · `manifest_files`：白名单目录下的文件 → 逐文件比哈希（不用读新包文件）
      · `folder_hashes` ：非白名单顶层目录的内容哈希 → 只算本地一侧
      · `index_hashes`  ：modrinth.index.json 声明的下载文件

    `compare_root` 是"已经解压出来的那部分"（只需根目录散文件，很快），
    用来处理根目录文件；目录部分完全走清单。

    返回 None 表示"清单不足以判断"（例如某个既有目录没有记录哈希），
    调用方应退回"等解压完成再逐文件比"的老路径 —— 宁可慢，不能错。
    """
    old_root = Path(old_root)
    wl = list(whitelist) if whitelist else list(DEFAULT_WHITELIST)
    wl_top = {p.split("/", 1)[0] for p in wl if p}
    idx = index_hashes or {}
    f_hashes = {str(k): str(v) for k, v in (folder_hashes or {}).items()
                if isinstance(v, str) and v}
    index_by_top, _index_deep, index_root_files = _split_index_by_top(idx)

    # 清单按顶层目录归档（白名单目录是文件级，其它目录用 folder_hashes）
    per_top: dict[str, dict[str, tuple[str, str]]] = {}
    for rel, entry in (manifest_files or {}).items():
        if not isinstance(entry, dict):
            continue
        algo = str(entry.get("algo", "") or "")
        value = str(entry.get("value", "") or "")
        if not algo or not value:
            continue
        rel_s = str(rel).replace("\\", "/").strip("/")
        if not rel_s or "/" not in rel_s:
            continue                     # 根目录文件单独处理
        top, sub = rel_s.split("/", 1)
        per_top.setdefault(top, {})[sub] = (algo, value)

    # 顶层目录 = 包里的目录（调用方给的）+ 白名单文件清单 + index 清单
    # + 非白名单目录哈希清单
    tops = set(per_top) | set(index_by_top) | set(f_hashes)
    if pack_tops:
        tops |= set(pack_tops)

    # 白名单只覆盖了某个顶层的**子路径**时，那个顶层的清单不完整 ——
    # 不能据此判"删除"。直接交给调用方退回老路径（宁可慢，不能删错）。
    partial = {str(t) for t in (partial_tops or set()) if t}
    partial |= {p.split("/", 1)[0] for p in wl if "/" in p}
    if partial & (tops | set(per_top)):
        return None

    # 覆盖率校验：包里确实存在的白名单文件，清单必须都记了。
    # 少记一个 → 那个文件会被当成"新包没有"（误删）或"没变化"（漏更新）。
    if pack_files:
        covered = set(per_top) | set(index_by_top)
        for rel in pack_files:
            rel_s = str(rel).replace("\\", "/").strip("/")
            if not rel_s or "/" not in rel_s:
                continue
            top = rel_s.split("/", 1)[0]
            if top not in wl_top:
                continue
            if rel_s not in index_by_top.get(top, {}) and \
                    rel_s[len(top) + 1:] not in per_top.get(top, {}):
                return None
    for top in tops:
        if top in wl_top:
            continue
        # 非白名单目录：既没有记录哈希、包里又确实有这个目录 → 判不了
        if top not in f_hashes and (top in (pack_tops or set())
                                    or (old_root / top).is_dir()):
            return None

    result = DiffResult()
    names = sorted(tops)
    total = max(1, len(names))

    def _p(done: int):
        if progress:
            try:
                progress(done, total, "merge")
            except Exception:  # noqa: BLE001, S110
                pass

    for i, top in enumerate(names):
        local_top = old_root / top
        has_local = local_top.is_dir()
        entries = per_top.get(top, {})
        idx_entries = index_by_top.get(top, {})

        if top not in wl_top:
            # 非白名单：整目录级（和文件系统比对一致）
            recorded = f_hashes.get(top, "")
            if not has_local:
                result.added.append(Change(Path(top), ChangeKind.ADDED,
                                           is_folder_level=True))
            elif recorded:
                if folder_hash(local_top) != recorded:
                    result.modified.append(
                        Change(Path(top), ChangeKind.MODIFIED,
                               is_folder_level=True))
            else:
                return None
            _p(i + 1)
            continue

        # 白名单目录：逐文件（递归比，粒度比"子目录整体"更细也更准）
        new_map: dict[str, tuple[str, str]] = dict(entries)
        for full, hs in idx_entries.items():
            sub = full[len(top) + 1:]
            if sub and sub not in new_map:
                algo_h, hash_v = _pick_hash(hs)
                if hash_v:
                    new_map[sub] = (algo_h or "sha1", hash_v)

        if not has_local and not new_map:
            _p(i + 1)
            continue

        local_map = _walk_rel_files(local_top)
        to_compare: list[tuple[str, Path, str, str]] = []
        for sub in sorted(new_map):
            algo, value = new_map[sub]
            local = local_map.get(sub)
            if local is None:
                result.added.append(
                    Change(Path(top) / sub, ChangeKind.ADDED))
            else:
                to_compare.append((f"{top}/{sub}", local, value, algo))

        for sub in sorted(set(local_map) - set(new_map)):
            result.deleted.append(
                Change(Path(top) / sub, ChangeKind.DELETED))

        if to_compare:
            counter = {"n": 0}
            lock = threading.Lock()
            _total_compare = len(to_compare)

            def _one(item):
                full_rel, path, target_h, algo = item
                return full_rel, _hash_file(path, algo), target_h

            with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
                futures = [pool.submit(_one, it) for it in to_compare]
                for fut in futures:
                    try:
                        full_rel, local_h, target_h = fut.result()
                    except Exception:  # noqa: BLE001, S112
                        continue
                    with lock:
                        counter["n"] += 1
                        d = counter["n"]
                    if local_h != target_h:
                        result.modified.append(
                            Change(Path(full_rel), ChangeKind.MODIFIED))
                    if progress and d % 16 == 0:
                        try:
                            progress(i + (d / max(1, _total_compare)),
                                     max(1, total), "merge")
                        except Exception:  # noqa: BLE001, S110
                            pass
        _p(i + 1)

    # 根目录散文件：compare_root 里是"先解压出来的那部分"，体量很小
    if compare_root is not None:
        root_old = old_root
        old_files = {rel.name for rel in _root_files(root_old)}
        for rel in _root_files(Path(compare_root)):
            name = rel.name
            target_h: str | None = None
            algo = "sha1"
            idx_entry = index_root_files.get(name)
            if idx_entry is not None:
                algo_h, hash_v = _pick_hash(idx_entry)
                if hash_v:
                    target_h, algo = hash_v, (algo_h or "sha1")
            if target_h is None:
                ent = (manifest_files or {}).get(name)
                if isinstance(ent, dict) and ent.get("value"):
                    target_h = str(ent["value"])
                    algo = str(ent.get("algo") or "sha1")
            if target_h is None:
                target_h = _hash_file(Path(compare_root) / rel, "sha1")
                algo = "sha1"
            if name not in old_files:
                result.added.append(Change(rel, ChangeKind.ADDED))
                continue
            local_h = _hash_file(root_old / name, algo)
            if not target_h or not local_h or local_h != target_h:
                result.modified.append(Change(rel, ChangeKind.MODIFIED))
        # 根目录散文件**不做删除判定**：与文件系统比对保持一致
        # （包没带的根文件一律保留，避免误删玩家自己的文件）

    return result

def diff_packs(old_root: Path, new_root: Path,
               whitelist: list[str] | None = None,
               index_hashes: dict[str, dict] | None = None,
               progress: Callable[[int, int, str], None] | None = None,
               folder_hashes: dict[str, str] | None = None
               ) -> DiffResult:
    return diff_packs_parallel(old_root, new_root,
                               whitelist=whitelist,
                               index_hashes=index_hashes,
                               threads=1, progress=progress,
                               folder_hashes=folder_hashes)