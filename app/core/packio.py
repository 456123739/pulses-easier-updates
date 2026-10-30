"""
packio.py — 更新包的"不解压先看布局"与流式解压
------------------------------------------------
从 app/ui/player_view.py 提出来：Web 界面（app/ui_web）也要用同一套
包内布局判据，而 player_view 依赖 customtkinter，无头环境不想加载 GUI。

这里只做纯 IO：认布局、解压、边解压边按清单复核哈希。
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from .mrpack import is_safe_rel_path

_META_NAMES = {
    "modrinth.index.json", "ea_settings.json", "ea_manifest.json",
    "ea_hashes.json", "changelog.md", "CHANGELOG.md", "README.md",
}


def _zip_layout(zip_path: Path
                ) -> tuple[str, bool, list[str], list[str]]:
    """
    返回 (内容根前缀, 是否标准 .eapack 布局, 内容根下的顶层目录名)。

    标准布局 = 所有内容都在 `overrides/` 下，包级元数据散在 zip 根。
    这样不必先全部解压就能知道"内容根"在哪，可以立刻开始比对。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n.replace("\\", "/") for n in zf.namelist()
                     if n and not n.endswith("/")]
    except Exception:  # noqa: BLE001
        return "", False, [], []
    tops = {n.split("/", 1)[0] for n in names}
    standard = bool(tops) and "overrides" in tops and all(
        t == "overrides" or t in _META_NAMES for t in tops)
    prefix = "overrides/" if standard else ""
    dirs: set[str] = set()
    for n in names:
        rest = n[len(prefix):] if prefix and n.startswith(prefix) else (
            "" if prefix else n)
        if not rest or "/" not in rest:
            continue
        dirs.add(rest.split("/", 1)[0])
    return prefix, standard, sorted(dirs), names


def _extract_all_members(zip_path: Path, extract_dir: Path,
                         manifest: dict | None = None,
                         prefix: str = ""
                         ) -> tuple[int, list[str]]:
    """
    解压整个更新包，**在同一次读取里**按清单复核哈希。

    以前的做法是"先 extractall、再按清单抽查 128 个文件"：抽查会漏
    （单条陈旧项可能不在样本里），全量复核又要再读一遍整包。
    现在边解压边算哈希：零额外 IO，而且是**全量**复核。
    返回 (解压文件数, 与清单不一致的条目)。
    """
    from ..core.differ import _new_hasher

    bad: list[str] = []
    count = 0
    manifest = manifest if isinstance(manifest, dict) else {}
    prefix = prefix or ""
    with zipfile.ZipFile(zip_path) as zf:
        if manifest and not prefix:
            # 自愈：调用方忘了传前缀时，从成员名里认出来。
            # 否则清单永远匹配不上，复核会静默空转（历史 bug）。
            try:
                names = {str(i.filename).replace("\\", "/")
                         for i in zf.infolist() if not i.is_dir()}
                if names and any(n.startswith("overrides/") for n in names) \
                        and not any(str(k) in names for k in manifest):
                    prefix = "overrides/"
            except Exception:  # noqa: BLE001, S110
                pass
        for info in zf.infolist():
            name = str(info.filename).replace("\\", "/")
            if info.is_dir() or not name:
                continue
            rel = name.lstrip("/")
            # 规范化重复斜杠/`.`（与 extractall 的行为保持一致，避免静默丢文件）
            if "//" in rel or "/./" in rel or rel.startswith("./"):
                rel = "/".join(p for p in rel.split("/") if p not in ("", "."))
            if not is_safe_rel_path(rel):
                continue                  # 穿越 / 盘符 / 保留名：不落盘
            target = extract_dir / rel
            # 清单的键是"相对内容根"（overrides/ 已去掉），zip 成员名带前缀
            key = (rel[len(prefix):] if prefix and rel.startswith(prefix)
                   else rel)
            entry = manifest.get(key)
            algo = str(entry.get("algo", "")) if isinstance(entry, dict) else ""
            expect = str(entry.get("value", "")) if isinstance(entry, dict) else ""
            hasher = None
            if algo and expect:
                try:
                    hasher = _new_hasher(algo)
                except Exception:  # noqa: BLE001
                    hasher = None
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
            count += 1
            if hasher is not None and hasher.hexdigest() != expect:
                bad.append(key)
    return count, bad


def _extract_root_members(zip_path: Path, dest: Path, prefix: str
                          ) -> tuple[int, list[str]]:
    """只解压内容根下的散文件（通常只有几个小的），返回解压个数。"""
    count = 0
    failed: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if prefix:
                if not name.startswith(prefix):
                    continue
                rest = name[len(prefix):]
            else:
                rest = name
            if not rest or "/" in rest:
                continue
            target = dest / rest
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
            except Exception:  # noqa: BLE001
                failed.append(rest)
    return count, failed


