"""
export_specials.py — 「特殊导出内容」：更新范围表表达不了的那些
------------------------------------------------
更新范围表是按**顶层条目**给策略的（mods/、config/、options.txt……），
管不到目录内部；而 PCL 的导出选项里有一批正好是目录内部的细碎内容
（已禁用的 mod、JEI 书签、地图数据、截图……）。

这些就放在这里，规则**复用** `export_rules.DEFAULT_OPTIONS` 的同名 key
（展开、优先级、固定排除段全部由那边负责），本模块只干两件事：

1. 说清楚有哪些、默认不导出（`SPECIALS`）；
2. **扫到了才算数**（`detect`）——没这个文件就不该出现在界面上。

判定要便宜：只 stat 与列目录，不读内容、不算哈希；单个选项最多数
`MAX_FILES` 个文件就收手。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

#: 单个特殊选项最多统计多少个文件（再多也没意义，只是显示个数量）
MAX_FILES = 4000
#: 每个选项最多留几个样本路径给界面做提示
SAMPLE_SIZE = 3


@dataclass(frozen=True)
class Special:
    """一个「特殊导出内容」勾选项。

    `roots` 相对实例根；命中规则由 `export_rules` 里同名 key 负责，
    这里只用来判断"这个实例里到底有没有"。
    """

    key: str
    title: str
    desc: str
    roots: tuple[str, ...]
    patterns: tuple[str, ...] = ()   # 只看 roots 的直接子项名（fnmatch）


#: 全部特殊选项。**默认一个都不导出**——它们都是"本机产生的东西"，
#: 跟本项目对 options.txt 的一贯口径一致：更新包只装整合包该带的。
SPECIALS: tuple[Special, ...] = (
    Special("optifine", "OptiFine 画质设置",
            "optionsof.txt / optionsshaders.txt，跟显卡与光影走",
            ("optionsof.txt", "optionsshaders.txt")),
    Special("mods-disabled", "已禁用的 Mod",
            "mods/*.disabled、mods/*.old，玩家自己禁用的",
            ("mods",), ("*.disabled", "*.old")),
    Special("jei", "JEI 个人信息",
            "config/jei/world/：书签、作弊记录",
            ("config/jei/world",)),
    Special("patchouli", "帕秋莉手册个人数据",
            "patchouli_data.json",
            ("patchouli_data.json",)),
    Special("emi", "EMI 个人信息",
            "emi.json",
            ("emi.json",)),
    Special("personal", "本机操作记录",
            "hotbar.nbt / command_history.txt",
            ("hotbar.nbt", "command_history.txt")),
    Special("maps", "已绘制的地图",
            "journeymap/data/：玩家自己跑出来的",
            ("journeymap/data",)),
    Special("screenshots", "截图",
            "screenshots/",
            ("screenshots",)),
    Special("replay", "录像回放",
            "replay_recordings/、replay_videos/",
            ("replay_recordings", "replay_videos")),
)


def keys() -> frozenset[str]:
    """所有特殊选项的 key（开发者端默认要把这些从勾选里去掉）。"""
    return frozenset(s.key for s in SPECIALS)


def _scan_dir(target: Path, base: Path, budget: list[int],
              sample: list[str]) -> tuple[int, int]:
    """递归数文件、累加字节；返回 (files, bytes)。"""
    files = 0
    total = 0
    stack = [target]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if budget[0] <= 0:
                        return files, total
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    files += 1
                    total += stat.st_size
                    budget[0] -= 1
                    if len(sample) < SAMPLE_SIZE:
                        try:
                            sample.append(str(Path(entry.path).relative_to(base)))
                        except ValueError:
                            pass
        except OSError:
            continue
    return files, total


def _scan_flat(target: Path, base: Path, patterns: tuple[str, ...],
               sample: list[str]) -> tuple[int, int]:
    """只看一层，按文件名通配（mods/*.disabled 这种）。"""
    files = 0
    total = 0
    try:
        with os.scandir(target) as it:
            for entry in it:
                name = entry.name
                if not any(fnmatch.fnmatch(name, pat) for pat in patterns):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                files += 1
                total += stat.st_size
                if len(sample) < SAMPLE_SIZE:
                    try:
                        sample.append(str(Path(entry.path).relative_to(base)))
                    except ValueError:
                        pass
    except OSError:
        pass
    return files, total


def _probe(root: Path, special: Special) -> dict | None:
    """这个实例里有没有这个特殊选项的内容？有就返回统计，没有返回 None。"""
    files = 0
    total = 0
    sample: list[str] = []
    budget = [MAX_FILES]
    for raw in special.roots:
        target = root / raw
        try:
            if special.patterns:
                if not target.is_dir():
                    continue
                got = _scan_flat(target, root, special.patterns, sample)
            elif target.is_file():
                got = (1, target.stat().st_size)
                if len(sample) < SAMPLE_SIZE:
                    sample.append(raw)
            elif target.is_dir():
                got = _scan_dir(target, root, budget, sample)
            else:
                continue
        except OSError:
            continue
        files += got[0]
        total += got[1]
        if files >= MAX_FILES:
            break
    if not files:
        return None
    return {"key": special.key, "title": special.title, "desc": special.desc,
            "files": files, "bytes": total, "sample": sample}


def detect(root: str | Path) -> list[dict]:
    """扫一遍实例，返回**确实存在**的特殊选项（保持 SPECIALS 的顺序）。

    只做 stat / 列目录，不读任何文件内容；不存在的直接不返回，
    所以界面不需要"灰掉"这一类状态。
    """
    base = Path(str(root or "")).expanduser()
    if not base.is_dir():
        return []
    found: list[dict] = []
    for special in SPECIALS:
        hit = _probe(base, special)
        if hit:
            found.append(hit)
    return found
