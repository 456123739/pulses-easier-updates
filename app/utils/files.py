"""
files.py — 通用文件工具
------------------------------------------------
SHA-256 计算、目录遍历、文件大小格式化、安全路径拼接。
"""

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path


def sha256_of_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """
    计算文件的 SHA-256 摘要。
    分块读取，避免大文件一次性载入内存。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def iter_files(root: Path) -> Iterator[Path]:
    """
    递归遍历目录下所有文件（不含目录本身）。
    返回相对于 root 的 Path，便于两侧比对。
    """
    root = Path(root)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            abs_path = Path(dirpath) / name
            yield abs_path.relative_to(root)


def human_size(num_bytes: int) -> str:
    """将字节数格式化为易读字符串，如 3.2 MB"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def safe_join(root: Path, rel: Path) -> Path:
    """
    将相对路径安全地拼接到 root，防止 ../ 越权。
    若结果逃出 root，抛出 ValueError。
    """
    root = Path(root).resolve()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"非法路径: {rel}")
    return target