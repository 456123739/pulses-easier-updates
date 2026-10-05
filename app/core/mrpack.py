"""
mrpack.py — Modrinth 整合包格式解析
------------------------------------------------
解析 .mrpack / .zip 导出包：
  - modrinth.index.json 的 files 数组
  - overrides/ 目录

合并规则：
  - mods/、resourcepacks/、shaderpacks/、tacz/ 等：合并 index + overrides
  - 其余文件夹：只取 overrides
"""

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

INDEX_FILENAME = "modrinth.index.json"
OVERRIDES_DIR = "overrides"

# 需要合并 index 与 overrides 的文件夹（其余只取 overrides）
MERGE_FOLDERS = {"mods", "resourcepacks", "shaderpacks", "tacz"}


@dataclass
class MRFile:
    """modrinth.index.json 中的一个文件条目"""
    path: str                 # 相对根，如 mods/xxx.jar
    sha1: str = ""
    sha256: str = ""
    sha512: str = ""
    downloads: list[str] = field(default_factory=list)
    file_size: int = 0
    env: dict = field(default_factory=dict)


@dataclass
class MRPack:
    game: str = "minecraft"
    format_version: int = 1
    version_id: str = ""
    name: str = ""
    summary: str = ""
    dependencies: dict = field(default_factory=dict)
    files: list[MRFile] = field(default_factory=list)

    # overrides/ 中实际存在的相对路径集合（不含 overrides 前缀）
    overrides_files: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def parse_mrpack(zip_path: Path) -> tuple[MRPack | None, str]:
    """
    解析 MR 导出包。
    返回 (MRPack, "") 或 (None, 错误原因)
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return None, f"文件不存在：{zip_path}"

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()

            # 找 modrinth.index.json
            index_name = None
            for n in names:
                if n == INDEX_FILENAME or n.endswith("/" + INDEX_FILENAME):
                    index_name = n
                    break
            if not index_name:
                return None, "不是 MR 格式：缺少 modrinth.index.json"

            try:
                raw = zf.read(index_name).decode("utf-8")
                data = json.loads(raw)
            except Exception as e:  # noqa: BLE001
                return None, f"modrinth.index.json 解析失败：{e}"

            pack = _parse_index(data)

            # 扫描 overrides/
            prefix = OVERRIDES_DIR + "/"
            for n in names:
                if n.startswith(prefix) and not n.endswith("/"):
                    rel = n[len(prefix):]
                    if rel:
                        pack.overrides_files.append(rel)

            return pack, ""
    except zipfile.BadZipFile:
        return None, "不是有效的 ZIP 文件"
    except Exception as e:  # noqa: BLE001
        return None, f"读取失败：{e}"


def _parse_index(data: dict) -> MRPack:
    pack = MRPack(
        game=data.get("game", "minecraft"),
        format_version=int(data.get("formatVersion", 1)),
        version_id=data.get("versionId", ""),
        name=data.get("name", ""),
        summary=data.get("summary", ""),
        dependencies=data.get("dependencies", {}) or {},
    )

    for f in data.get("files", []) or []:
        if not isinstance(f, dict):
            continue
        hashes = f.get("hashes", {}) or {}
        mf = MRFile(
            path=f.get("path", ""),
            sha1=hashes.get("sha1", ""),
            sha256=hashes.get("sha256", ""),
            sha512=hashes.get("sha512", ""),
            downloads=f.get("downloads", []) or [],
            file_size=int(f.get("fileSize", 0) or 0),
            env=f.get("env", {}) or {},
        )
        if mf.path:
            pack.files.append(mf)

    return pack


# ----------------------------------------------------------------------
# 合并：index + overrides
# ----------------------------------------------------------------------
def merge_files(pack: MRPack) -> list[dict]:
    """
    合并 index 和 overrides 的文件列表。

    返回 list[dict]，每项：
      {
        "path": "mods/xxx.jar",
        "source": "override" | "index",   # 数据来源
        "sha1": "...", "sha256": "...", "sha512": "...",
        "downloads": [...],
        "file_size": 12345,
      }

    合并规则：
      - mods/、resourcepacks/、shaderpacks/、tacz/：合并两处
      - 其他文件夹：只取 overrides
      - 同一路径两处都有 → overrides 优先，但保留 index 的下载链接
    """
    # 索引 map
    index_map: dict[str, MRFile] = {f.path: f for f in pack.files}

    # overrides 集合
    override_set = set(pack.overrides_files)

    result: list[dict] = []
    seen: set[str] = set()

    # 1) overrides 优先
    for path in sorted(override_set):
        entry = {
            "path": path,
            "source": "override",
            "sha1": "", "sha256": "", "sha512": "",
            "downloads": [],
            "file_size": 0,
        }
        # 若 index 里有同路径，保留下载信息
        if path in index_map:
            mf = index_map[path]
            entry["sha1"] = mf.sha1
            entry["sha256"] = mf.sha256
            entry["sha512"] = mf.sha512
            entry["downloads"] = list(mf.downloads)
            entry["file_size"] = mf.file_size
        result.append(entry)
        seen.add(path)

    # 2) index 中独有的
    for path, mf in index_map.items():
        if path in seen:
            continue
        top = path.split("/", 1)[0] if "/" in path else ""
        # 只对合并文件夹保留 index 独有项；其他文件夹的 index 项丢弃
        if top not in MERGE_FOLDERS:
            continue
        result.append({
            "path": path,
            "source": "index",
            "sha1": mf.sha1,
            "sha256": mf.sha256,
            "sha512": mf.sha512,
            "downloads": list(mf.downloads),
            "file_size": mf.file_size,
        })

    return result


def top_level_folders(merged: list[dict]) -> list[str]:
    """从合并结果中提取顶层文件夹（去重、排序）"""
    folders: set[str] = set()
    for item in merged:
        path = item.get("path", "")
        if "/" in path:
            folders.add(path.split("/", 1)[0])
    return sorted(folders)


def index_top_folders(pack: MRPack) -> set[str]:
    """
    从**原始 index 条目**中提取所有顶层文件夹名。

    与 top_level_folders(merged) 的区别：
      - 不受 MERGE_FOLDERS 过滤影响
      - 不管有没有下载链接
      - 只要 index 里声明了该路径，就纳入

    用途：白名单推导。保证 index 里写了文件的文件夹（如 tacz/）
    一定走文件级比对，即使更新包自带 whitelist 缺失（老版本更新包）。
    """
    tops: set[str] = set()
    for f in pack.files:
        p = f.path
        if "/" in p:
            top = p.split("/", 1)[0]
            if top:
                tops.add(top)
    return tops


# ----------------------------------------------------------------------
# 解压
# ----------------------------------------------------------------------
def extract_to(zip_path: Path, dest_dir: Path) -> bool:
    """解压导出包到指定目录"""
    try:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest_dir)
        return True
    except Exception:  # noqa: BLE001
        return False