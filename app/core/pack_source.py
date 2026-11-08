"""
pack_source.py — 一次 stat / 一次中央目录，判断「这个路径是什么」
------------------------------------------------
开发者端的 STEP 1 现在只有一个入口：整合包定位。用户可能给的东西很杂
（实例目录、版本目录、拖进来的导出包 zip、甚至误拖的更新包），
所以要有一处统一的判断，并且**必须便宜**：

* 只看目录项与 zip 的中央目录（`namelist()`），**绝不读成员内容**、绝不解压、绝不哈希；
* 目录最多向上找 `pack_locator.MAX_DEPTH` 层，每层一次 `scandir`；
* 条目数超过 `MAX_ENTRIES` 直接判"不像整合包"，避免被超大包拖住。

判定结果只有三种：实例目录（instance）/ 导出包 zip（zip）/ 其它（抛 SourceError）。
"其它"里最常见的是**更新包**（`.eapack`）——那是玩家端吃的东西，这里要明确拒绝并说清楚，
否则用户会以为拖错了软件。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import pack_locator

KIND_INSTANCE = "instance"
KIND_ZIP = "zip"

#: 只有这两种后缀会被当成"启动器导出的整合包"
ZIP_SUFFIXES = (".zip", ".mrpack")
#: 玩家端的更新包（开发者端不接受）
UPDATE_SUFFIXES = (".eapack",)

#: zip 里找这个才算 Modrinth 格式（与 mrpack.parse_mrpack 的判据一致）
INDEX_NAME = "modrinth.index.json"
#: 条目上限：再多就不像整合包了（真实整合包通常几百到几千）
MAX_ENTRIES = 200_000


class SourceError(RuntimeError):
    """路径不能当整合包源用；`code` 会原样回给界面。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SourceRef:
    """判断结果。`root` 只有 instance 有；`entries` 只有 zip 有。"""

    kind: str
    path: Path
    label: str
    root: str = ""
    note: str = ""
    entries: int = 0


def _zip_names(path: Path) -> list[str]:
    """只取中央目录里的名字，不碰任何成员内容。"""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        raise SourceError("BAD_ZIP", "这个文件不是有效的 ZIP（可能没下完或被改坏）")
    except OSError as exc:
        raise SourceError("READ_FAILED", f"读不了这个文件：{exc}")
    if len(names) > MAX_ENTRIES:
        raise SourceError("TOO_MANY_ENTRIES",
                          f"包内条目超过 {MAX_ENTRIES} 个，不像整合包")
    return names


def _has_index(names: list[str]) -> bool:
    """modrinth.index.json 可以在根，也可以在一层目录里（mrpack 允许）。"""
    for name in names:
        if name == INDEX_NAME or name.endswith("/" + INDEX_NAME):
            return True
    return False


def classify(raw: str | Path) -> SourceRef:
    """判断路径类型。快：目录两次 stat 级操作，zip 一次中央目录读。"""
    text = str(raw or "").strip()
    if not text:
        raise SourceError("INVALID_INPUT", "没有拿到路径")
    try:
        path = Path(text).expanduser()
    except (OSError, ValueError):
        raise SourceError("INVALID_INPUT", f"路径不合法：{text}")

    try:
        is_dir = path.is_dir()
        is_file = path.is_file()
    except OSError as exc:
        raise SourceError("READ_FAILED", f"看不了这个路径：{exc}")
    if not is_dir and not is_file:
        raise SourceError("NOT_FOUND", f"路径不存在：{path}")

    if is_dir:
        root = pack_locator.locate_modpack(path)
        if not root:
            raise SourceError(
                "NOT_A_PACK",
                "这个目录里没找到整合包（需要有 mods/ 和 config/）。"
                "如果拿到的是启动器导出的包，请直接拖入 .zip / .mrpack。")
        return SourceRef(kind=KIND_INSTANCE, path=Path(root),
                         label=Path(root).name, root=str(root))

    suffix = path.suffix.lower()
    if suffix in UPDATE_SUFFIXES:
        raise SourceError(
            "IS_UPDATE_PACK",
            "这是更新包（玩家端用的），开发者端要的是整合包本身："
            "请选整合包实例目录，或 Modrinth 格式的 .mrpack。")
    if suffix in ZIP_SUFFIXES:
        names = _zip_names(path)
        if not _has_index(names):
            raise SourceError(
                "NOT_A_PACK",
                "这个 zip 里没有 modrinth.index.json —— 目前只认 Modrinth 格式的导出包。"
                "其它启动器导出的包，请先解压成实例目录，再用整合包定位选目录。")
        return SourceRef(kind=KIND_ZIP, path=path, label=path.stem,
                         note="modrinth", entries=len(names))

    # 其它文件：和侧边栏一样，认「整合包里的任意文件」（mods/x.jar、config/y.toml…），
    # 向上找根目录。这一步只是 scandir，不会读文件内容。
    root = pack_locator.locate_modpack(path)
    if root:
        return SourceRef(kind=KIND_INSTANCE, path=Path(root),
                         label=Path(root).name, root=str(root))
    raise SourceError(
        "NOT_SUPPORTED",
        f"不认识这个文件（{suffix or '无后缀'}）：请选整合包目录、整合包里的任意文件，"
        "或 Modrinth 格式的 .zip / .mrpack。")

