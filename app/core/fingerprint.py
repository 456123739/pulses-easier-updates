"""
fingerprint.py — 文件指纹（sha1 / sha512 / CurseForge murmur2）
------------------------------------------------
三者用途不同，别混：
- sha1：Modrinth `version_files` 查询键，也是 index 里的 `hashes.sha1`
- sha512：index 里的 `hashes.sha512`
- murmur2：CurseForge `fingerprints/432` 查询键

CurseForge 的指纹不是对原文件算的：**先去掉 \\t \\n \\r 空格四种字节**，
再用 MurmurHash2（seed=1）算 32 位整数。PCL 与 PCL-CE 都是这个算法，
2026-11-01 用真实文件对过 CurseForge 官方接口（见 docs 里的实测记录）。
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

_CHUNK = 1 << 20
_SKIP_BYTES = bytes((9, 10, 13, 32))
_M = 0x5BD1E995
_MASK = 0xFFFFFFFF


@dataclass(frozen=True)
class Fingerprints:
    sha1: str
    sha512: str
    curseforge: int
    size: int


_MEM_CAP = 32 << 20          # 32 MiB：小文件一次读进内存，大文件走两遍


def _murmur2_known(data: bytes, length: int, seed: int = 1) -> int:
    """已知总长度时的 MurmurHash2（data 必须已过滤空白字节）。"""
    h = (seed ^ length) & _MASK
    n = len(data) & ~3
    if n:
        words = struct.unpack_from(f"<{n // 4}I", data, 0)
        for k in words:
            k = (k * _M) & _MASK
            k ^= k >> 24
            k = (k * _M) & _MASK
            h = (h * _M) & _MASK
            h ^= k
    rest = len(data) - n
    if rest == 3:
        h ^= data[n] | data[n + 1] << 8
        h ^= data[n + 2] << 16
        h = (h * _M) & _MASK
    elif rest == 2:
        h ^= data[n] | data[n + 1] << 8
        h = (h * _M) & _MASK
    elif rest == 1:
        h ^= data[n]
        h = (h * _M) & _MASK
    h ^= h >> 13
    h = (h * _M) & _MASK
    h ^= h >> 15
    return h & _MASK


def curseforge_fingerprint(data: bytes) -> int:
    """一次性算（测试与小块用）。"""
    filtered = data.translate(None, _SKIP_BYTES)
    return _murmur2_known(filtered, len(filtered))


def _murmur2_file(path: Path, length: int) -> int:
    """第二遍：只知道过滤后的总长度，重新读文件算哈希。"""
    h = (1 ^ length) & _MASK
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            chunk = chunk.translate(None, _SKIP_BYTES)
            if not chunk:
                continue
            data = tail + chunk
            n = len(data) & ~3
            if n:
                for k in struct.unpack_from(f"<{n // 4}I", data, 0):
                    k = (k * _M) & _MASK
                    k ^= k >> 24
                    k = (k * _M) & _MASK
                    h = (h * _M) & _MASK
                    h ^= k
            tail = data[n:]
    rest = len(tail)
    if rest >= 3:
        h ^= tail[0] | tail[1] << 8
        h ^= tail[2] << 16
        h = (h * _M) & _MASK
    elif rest == 2:
        h ^= tail[0] | tail[1] << 8
        h = (h * _M) & _MASK
    elif rest == 1:
        h ^= tail[0]
        h = (h * _M) & _MASK
    h ^= h >> 13
    h = (h * _M) & _MASK
    h ^= h >> 15
    return h & _MASK


def compute(path: str | Path, *, curseforge: bool = True) -> Fingerprints:
    """算哈希；`curseforge=False` 时跳过 murmur2（大文件且非候选时省一遍 IO）。"""
    path = Path(path)
    sha1 = hashlib.sha1()
    sha512 = hashlib.sha512()
    size = 0
    filtered_len = 0
    buf = bytearray()
    buffering = True
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            sha1.update(chunk)
            sha512.update(chunk)
            if not curseforge:
                continue
            filtered = chunk.translate(None, _SKIP_BYTES)
            filtered_len += len(filtered)
            if buffering:
                buf += filtered
                if len(buf) > _MEM_CAP:
                    buffering = False
                    buf = bytearray()
    if not curseforge:
        cf = 0
    elif buffering:
        cf = _murmur2_known(bytes(buf), filtered_len)
    else:
        cf = _murmur2_file(path, filtered_len)
    return Fingerprints(sha1=sha1.hexdigest(), sha512=sha512.hexdigest(),
                        curseforge=cf, size=size)


# ----------------------------------------------------------------------
# 缓存：同一个文件反复导出时不重复读盘
# ----------------------------------------------------------------------
class FingerprintCache:
    """`{path: {mtime, size, sha1, sha512, cf}}`，原子写盘。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else (
            Path.home() / ".pulses_easier" / "cache" / "fingerprints.json")
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._pending = 0
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
        except Exception:  # noqa: BLE001
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass

    def get(self, path: str | Path) -> Fingerprints | None:
        path = Path(path)
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            return None
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            if item.get("mtime") != st.st_mtime_ns or item.get("size") != st.st_size:
                return None
            try:
                return Fingerprints(item["sha1"], item["sha512"],
                                    int(item["curseforge"]), int(item["size"]))
            except Exception:  # noqa: BLE001
                return None

    def put(self, path: str | Path, fp: Fingerprints) -> None:
        path = Path(path)
        try:
            st = path.stat()
        except OSError:
            return
        with self._lock:
            self._data[str(path)] = {"mtime": st.st_mtime_ns, "size": fp.size,
                                     **{k: v for k, v in asdict(fp).items()
                                        if k != "size"}}
            self._pending += 1
            if self._pending >= 200:        # 批量落盘：避免 O(n²) 重写
                self._pending = 0
                self._save()

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                self._pending = 0
                self._save()

    def compute(self, path: str | Path, *,
                curseforge: bool = True) -> Fingerprints:
        cached = self.get(path)
        if cached is not None and (cached.curseforge or not curseforge):
            return cached
        fp = compute(path, curseforge=curseforge)
        if fp.curseforge:              # 没算 CF 的结果不落盘，避免污染缓存
            self.put(path, fp)
        return fp
