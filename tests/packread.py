"""tests/packread.py — 测试里读更新包的统一入口（不关心是 zip 还是 v2 容器）"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.core.container import open_container


def names(path: Path) -> list[str]:
    with open_container(path) as reader:
        return list(reader.names)


def read(path: Path, name: str) -> bytes:
    with open_container(path) as reader:
        return reader.read(name)


def all_entries(path: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with open_container(path) as reader:
        for name in reader.names:
            if name.endswith("/"):
                continue
            out[name] = reader.read(name)
    return out


def rewrite_as_zip(src: Path, dst: Path,
                   patch: dict[str, bytes] | None = None,
                   drop: set[str] | None = None) -> None:
    """把任意格式的包读出来，写成一个（可篡改的）zip，用于签名/损坏测试。"""
    entries = all_entries(src)
    for name in (drop or set()):
        entries.pop(name, None)
    entries.update(patch or {})
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def extract_all(path: Path, dest: Path) -> int:
    """把包按逻辑内容解压到 dest（v2 会还原存档变换）。"""
    from app.core.mrpack import is_safe_rel_path
    count = 0
    dest.mkdir(parents=True, exist_ok=True)
    with open_container(path) as reader:
        for name in reader.names:
            rel = str(name).replace("\\", "/").lstrip("/")
            if not rel or rel.endswith("/") or not is_safe_rel_path(rel):
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                for chunk in reader.iter_chunks(name):
                    out.write(chunk)
            count += 1
    return count
