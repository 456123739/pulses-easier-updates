"""
instance_scan.py — 扫描整合包实例，产出「导出清单」
------------------------------------------------
对应 PCL2 `PageInstanceExport` 的复制+分类阶段（见
docs/PCL-整合包导出-技术规范.md §2），但**不复制文件**：只遍历、判定、统计。
打包时再按清单读文件（少一次全量拷贝）。

产物是纯数据，供开发者端 UI 与 index 生成使用。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import pack_locator
from .export_rules import should_keep

# 仅实例根目录跳过的目录（PCL `PageInstanceExport.xaml.vb:499-501`）
ROOT_SKIP = frozenset({"assets", "versions", "libraries"})
# 任意层级都跳过（缓存/工具目录）
ANY_SKIP = frozenset({"structurecachev1", ".fabric", ".git", "avatar-cache",
                      "cosmetic-cache"})

# 允许指向实例外的一级目录（Windows 上把 mods/存档链到别的盘很常见）；
# 其它一级目录（例如 config）被链到外面一律拦，避免把实例外的隐私文件打进包。
ESCAPE_ALLOWED = frozenset({"mods", "resourcepacks", "shaderpacks", "saves",
                            "datapacks", "openloader"})

# 可能来自网络资源库的文件后缀（PCL `:527-529`）
CANDIDATE_EXTS = frozenset({".zip", ".rar", ".jar", ".disabled", ".old"})
# 路径里含这些子串才算"资源文件"
CANDIDATE_HINTS = ("mods", "packs", "openloader", "resource")


@dataclass
class ScanRow:
    """顶层条目（一个目录或一个根散文件）。"""

    name: str
    is_file: bool
    files: int = 0
    bytes: int = 0
    candidates: int = 0
    children: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "isFile": self.is_file,
            "files": self.files,
            "bytes": self.bytes,
            "candidates": self.candidates,
            "children": [
                {"path": k, **v} for k, v in sorted(self.children.items())
            ],
        }


def is_candidate(rel_posix: str) -> bool:
    """这个文件是否值得去 CurseForge / Modrinth 查（PCL 同款判据）。"""
    lower = rel_posix.lower()
    suffix = os.path.splitext(lower)[1]
    if suffix not in CANDIDATE_EXTS:
        return False
    return any(hint in lower for hint in CANDIDATE_HINTS)


def _walk(root: Path, rules: list[str], unsafe: list[str] | None = None,
          bad_names: list[str] | None = None):
    """按 PCL 的顺序遍历，产出 (rel_posix, size, is_candidate, kept)。

    跟随软链接/junction（Windows 上把 mods 链到别的盘很常见），
    但用 realpath 记账防止环。
    """
    seen: set[str] = set()
    root_real = os.path.realpath(root)

    def inside(path: str) -> bool:
        return path == root_real or path.startswith(root_real + os.sep)

    def visit(dir_path: Path, rel_dir: str, escaped_ok: bool = False):
        try:
            real = os.path.realpath(dir_path)
        except OSError:
            return
        if real in seen:
            return
        if not escaped_ok and not inside(real):
            # 只有实例根下的一级目录允许指向外部（mods 链到别的盘的常见用法），
            # 更深层再"跳出去"就是越界，直接跳过并留痕。
            if unsafe is not None and len(unsafe) < 50:
                unsafe.append(f"{rel_dir or '.'}（指向实例外）")
            return
        seen.add(real)
        try:
            with os.scandir(dir_path) as it:
                entries = list(it)
        except OSError:
            return

        subdirs: list[tuple[Path, str]] = []
        for entry in entries:
            name = entry.name
            low = name.lower()
            if low in ANY_SKIP:
                continue
            if not rel_dir and low in ROOT_SKIP:
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                rel.encode("utf-8")
            except UnicodeEncodeError:
                if bad_names is not None and len(bad_names) < 50:
                    bad_names.append(repr(rel))
                continue
            try:
                if entry.is_dir(follow_symlinks=True):
                    # 只有"实例根下、本身是软链接、且指向外部"的一级目录
                    # 才允许整棵子树在外面（mods 链到别的盘）；更深层一律拦住。
                    link_out = (not rel_dir and entry.is_symlink()
                                and name.lower() in ESCAPE_ALLOWED
                                and not inside(os.path.realpath(entry.path)))
                    subdirs.append((Path(entry.path), rel,
                                    escaped_ok or link_out))
                    continue
                if not entry.is_file(follow_symlinks=True):
                    continue
                if not escaped_ok and not inside(os.path.realpath(entry.path)):
                    if unsafe is not None and len(unsafe) < 50:
                        unsafe.append(f"{rel}（指向实例外）")
                    continue
                size = entry.stat(follow_symlinks=True).st_size
            except OSError:
                continue
            if not should_keep(rel, rules):
                yield rel, 0, False, False
                continue
            yield rel, size, is_candidate(rel), True

        for path, rel, child_ok in subdirs:
            yield from visit(path, rel, child_ok)

    yield from visit(root, "")


def _load_version_json(root: Path) -> dict | None:
    """找实例对应的版本 json。

    多版本 `.minecraft` 里 `versions/*` 可能有一堆别的版本，**不能猜**：
    先按 id / 目录名精确匹配，实在只有一个候选时才用它。
    """
    name = root.name
    ranked: list[tuple[int, Path]] = []
    for path, rank in _version_json_candidates(root, name):
        ranked.append((rank, path))
    ranked.sort(key=lambda item: item[0])

    valid: list[tuple[int, Path, dict]] = []
    for rank, path in ranked:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                valid.append((rank, path, data))
        except Exception:  # noqa: BLE001
            continue
    if not valid:
        return None
    for rank, _path, data in valid:
        if rank == 0:                 # 实例自带的 <name>.json 最可信
            return data
    for _rank, _path, data in valid:
        if str(data.get("id") or "") == name:
            return data
    if len(valid) == 1:
        return valid[0][2]
    return None


def _version_json_candidates(root: Path, name: str) -> list[tuple[Path, int]]:
    """(路径, 优先级)：数字越小越可信。"""
    out: list[tuple[Path, int]] = [(root / f"{name}.json", 0)]
    for base, rank in ((root, 1), (root.parent, 2)):
        versions = base / "versions"
        if not versions.is_dir():
            continue
        out.append((versions / name / f"{name}.json", rank))
        try:
            for sub in sorted(versions.iterdir()):
                if sub.is_dir():
                    out.append((sub / f"{sub.name}.json", rank + 10))
        except OSError:
            pass
    # 按路径去重，保留最小优先级
    seen: dict[Path, int] = {}
    for path, rank in out:
        if path not in seen or rank < seen[path]:
            seen[path] = rank
    return [(path, rank) for path, rank in seen.items()]


def detect_loader(root: Path) -> dict:
    """从版本 json 推 MC 版本与加载器（隔离实例 / 非隔离 .minecraft 都试）。"""
    import re

    info = {"minecraft": "", "loader": "vanilla", "loaderVersion": ""}
    data = _load_version_json(root)
    if not isinstance(data, dict):
        return info

    ident = str(data.get("id") or "")
    inherit = str(data.get("inheritsFrom") or "")
    joined = f"{ident} {inherit}".lower()

    if "neoforge" in joined:
        info["loader"] = "neoforge"
    elif "forge" in joined:
        info["loader"] = "forge"
    elif "fabric" in joined:
        info["loader"] = "fabric"

    for text in (inherit, ident):
        m = re.search(r"(1\.\d+(?:\.\d+)?)", text)
        if m:
            info["minecraft"] = m.group(1)
            break

    def _tail_version(raw: str) -> str:
        """forge1.12.2-14.23.5.2859 → 14.23.5.2859；forge-47.2.0 → 47.2.0。"""
        raw = raw.strip("-_ ")
        if "-" in raw:
            raw = raw.split("-")[-1]
        return raw

    loader = info["loader"]
    if loader == "fabric":
        m = re.search(r"fabric-loader-([0-9]+(?:\.[0-9]+)+)", joined)
        if not m:                       # 1.20.1-fabric0.15.11 这种写法
            m = re.search(r"fabric-?([0-9]+(?:\.[0-9]+)+)", joined)
        if m:
            info["loaderVersion"] = m.group(1)
    elif loader in ("forge", "neoforge"):
        m = re.search(rf"{loader}-?([0-9][\w.\-]*)", joined)
        if m:
            info["loaderVersion"] = _tail_version(m.group(1))
    return info


def scan_instance(raw_root: str | Path, rules: list[str] | None = None,
                  *, max_items: int = 200_000) -> dict:
    """扫描实例目录，返回 rows（UI 用）与 items（打包用）。"""
    root = Path(raw_root)
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")
    rules = rules or []

    rows: dict[str, ScanRow] = {}
    items: list[dict] = []
    skipped: list[str] = []
    unsafe: list[str] = []
    bad_names: list[str] = []
    total_files = total_bytes = 0
    candidates = 0

    for rel, size, cand, kept in _walk(root, rules, unsafe, bad_names):
        if not kept:
            if len(skipped) < 200:
                skipped.append(rel)
            continue
        top, _, rest = rel.partition("/")
        is_file = rest == ""
        key = top
        row = rows.get(key)
        if row is None:
            row = rows[key] = ScanRow(name=top, is_file=is_file)
        row.files += 1
        row.bytes += size
        if cand:
            row.candidates += 1
        if not is_file:
            child = row.children.get(top + "/" + rest.split("/", 1)[0])
            if child is None:
                child = row.children[top + "/" + rest.split("/", 1)[0]] = {
                    "files": 0, "bytes": 0, "candidates": 0}
            child["files"] += 1
            child["bytes"] += size
            if cand:
                child["candidates"] += 1
        items.append({"path": rel, "size": size, "candidate": cand})
        total_files += 1
        total_bytes += size
        if cand:
            candidates += 1
        if len(items) >= max_items:
            break

    return {
        "root": str(root),
        "rows": [r.as_dict() for _, r in sorted(rows.items())],
        "items": items,
        "skipped": skipped,
        "unsafeLinks": unsafe,
        "badNames": bad_names,
        "totals": {"files": total_files, "bytes": total_bytes,
                   "candidates": candidates},
        **detect_loader(root),
    }


def locate_and_scan(raw: str | Path, rules: list[str] | None = None) -> dict:
    """先定位整合包根，再扫描。"""
    root = pack_locator.locate_modpack(raw)
    if not root:
        raise FileNotFoundError("这个路径里没有找到整合包（需要 mods/ 与 config/）")
    return scan_instance(root, rules)
