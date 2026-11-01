"""
index_build.py — 生成 modrinth.index.json（PCL 语义）
------------------------------------------------
字段与顺序按 docs/PCL-整合包导出-技术规范.md §4：

    game / formatVersion / versionId / name / summary / files / dependencies

- `files[].downloads` 里 **CurseForge 直链排第一**，Modrinth 兜底（2.13.1.1 起）；
- `hashes` 至少带 sha1（PCL 只写 sha1；我们额外补 sha512，兼容 m1 校验）；
- `env` 不写（PCL 也不写；写了反而会改变服务端安装器的取舍）。
"""

from __future__ import annotations

import json

FORMAT_VERSION = 1


def dependencies(loader: str, loader_version: str,
                 minecraft: str) -> dict:
    """把扫描出来的加载器信息转成 index 的 dependencies。"""
    deps: dict[str, str] = {}
    if minecraft:
        deps["minecraft"] = str(minecraft)
    key = {"forge": "forge", "neoforge": "neoforge",
           "fabric": "fabric-loader"}.get(str(loader).lower())
    if key and loader_version:
        deps[key] = str(loader_version)
    return deps


def build_index(*, name: str, version_id: str, summary: str = "",
                files: list[dict] | None = None,
                deps: dict | None = None) -> dict:
    """files: [{path, sha1, sha512, downloads, size}]。"""
    entries = []
    for item in files or ():
        path = str(item.get("path") or "").replace("\\", "/").strip("/")
        if not path:
            continue
        hashes = {}
        if item.get("sha1"):
            hashes["sha1"] = str(item["sha1"]).lower()
        if item.get("sha512"):
            hashes["sha512"] = str(item["sha512"]).lower()
        urls = [str(u) for u in (item.get("downloads") or []) if u]
        if not hashes.get("sha1") or not urls:
            continue
        entries.append({
            "path": path,
            "hashes": hashes,
            "downloads": urls,
            "fileSize": int(item.get("size") or 0),
        })
    return {
        "game": "minecraft",
        "formatVersion": FORMAT_VERSION,
        "versionId": str(version_id),
        "name": str(name),
        "summary": str(summary or ""),
        "files": entries,
        "dependencies": dict(deps or {}),
    }


def dumps(index: dict) -> bytes:
    return json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")
