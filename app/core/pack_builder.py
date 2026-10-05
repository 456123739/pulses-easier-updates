"""
pack_builder.py — 开发者端：生成更新包
------------------------------------------------
将整合包目录与元数据（推荐策略、更新日志）打包为标准更新包 ZIP。
"""

import zipfile
from pathlib import Path

from ..config import META_FILENAME
from .metadata import build_meta


def build_update_package(pack_root: Path, out_zip: Path,
                         version: str,
                         checked: dict, strategies: dict,
                         changelog_md: str,
                         log=None) -> bool:
    """
    将 pack_root 下所有内容 + meta.json 打包为 out_zip。
    log(level, message) 可选。
    """
    pack_root, out_zip = Path(pack_root), Path(out_zip)
    meta = build_meta(version, checked, strategies, changelog_md)

    try:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            # 写入元数据
            zf.writestr(META_FILENAME,
                        __import__("json").dumps(meta, ensure_ascii=False, indent=2))

            # 写入整合包内容
            for p in pack_root.rglob("*"):
                if p.is_file():
                    arcname = p.relative_to(pack_root).as_posix()
                    if arcname == META_FILENAME:
                        continue  # 避免覆盖
                    zf.write(p, arcname)
                    if log:
                        log("info", f"打包 {arcname}")
        if log:
            log("info", f"更新包已生成：{out_zip}")
        return True
    except Exception as e:
        if log:
            log("error", f"生成失败：{e}")
        return False