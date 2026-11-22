"""
container.py — 更新包容器：块压缩流（v2）+ ZIP 兼容读取
================================================================
**为什么不用 zip**

zip 每个成员独立 deflate，窗口只有 32 KB，看不到"这个 jar 和那个 jar 里有
同一份库"这种跨文件冗余。整合包里 80% 的字节是已经压过的 jar，zip 只能把
它们再压掉几个百分点；换成"整包 solid + 大窗口"能再多省一截。

实测（真实文件拼的 211 MB 整合包）：

    zip deflate-6    182.12 MB   解压 3.3 s     ← 现状 = 启动器导出的做法
    zip LZMA         178.43 MB   解压 15.5 s
    solid xz -9e     173.28 MB   （要专用工具）
    solid zstd -19   170.36 MB   解压 0.54 s    ← 更小且更快

但"一整个 solid 流"没法随机读取。所以这里用**分块 solid**：

    文件 = 头部(8B) + 定长指针 + 若干 zstd 压缩块 + 尾部 JSON 索引

* 块大小默认 8 MB，块与块独立 → 可以只解压需要的那几块、可以并行解压、
  可以在解压中途失败时精确重试。
* 索引在**文件尾部**，开头只放 8 字节指针 → 写入时不需要预留头部空间，
  也不需要二次拷贝整个包。
* 元数据文件（index / settings / hashes / changelog）总是排在流的最前面，
  所以"导入时只想读元数据"只需要解压第一块。

**格式（小端不算，全部大端）**

    0   .. 8    magic  "EAPK2\\r\\n\\x1a\\n"（9 字节）
    9   .. 12   u32    header_offset      （索引块在文件里的绝对偏移）
    13  .. 16   u32    header_comp_len
    17  .. 20   u32    header_raw_len
    21  .. 24   u32    format_version     （=2）
    25  ..      blocks   （每块一个独立 zstd 帧，长度记在索引里）
    header_off  zstd(json 索引)

索引 JSON：

    {
      "created_by": "pulses-easier 0.12.0",
      "block_size": 8388608,
      "blocks": [[comp_len, raw_len], ...],
      "entries": [
         {"name": "overrides/mods/a.jar", "size": 123, "stored": 123,
          "sha1": "...", "transform": null,
          "chunks": [[block, offset, length], ...]},
         ...
      ]
    }

**兼容**

`open_container()` 先看 magic：不是 EAPK2 就当成 zip 用 `zipfile` 读，
所以老包（0.11.x 及更早）仍然能打开。写新包时若环境里没有 zstandard，
自动退回 zip，不会让导出失败。
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

try:                                  # 允许没装 zstandard 的旧环境
    import zstandard as _zstd
except Exception:                     # pragma: no cover - 环境相关
    _zstd = None

MAGIC = b"EAPK2\r\n\x1a\n"
FORMAT_VERSION = 2
_FIXED_HEAD = len(MAGIC) + 16         # magic + 4 × u32
# 块大小：64 MB。块越大越"接近 solid"，压缩率更好；代价是解压单块时的内存
# 峰值（64 MB）与随机访问粒度。实测 8/32/64/128/单块 之间体积差 <1%，
# 64 MB 是"好压缩率 + 可接受内存"的平衡点。
DEFAULT_BLOCK = 64 * 1024 * 1024
# zstd 等级：17 在 228 MB 素材上比 15 小 0.7%、比 19 慢一半但只差 0.5%
DEFAULT_LEVEL = 17
_CHUNK = 1 << 20                      # 读文件时每次喂 1 MB


class ContainerError(Exception):
    """容器损坏 / 不支持的版本。"""


def available() -> bool:
    """当前环境能否写 v2 容器。"""
    return _zstd is not None


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------
@dataclass
class WriterStats:
    files: int = 0
    logical_bytes: int = 0
    stored_bytes: int = 0
    compressed_bytes: int = 0
    transformed: int = 0
    level: int = DEFAULT_LEVEL
    fallback_zip: bool = False
    entries: list[str] = field(default_factory=list)


class ContainerWriter:
    """
    写 v2 容器。

    用法::

        with ContainerWriter(path) as w:
            w.add_bytes("modrinth.index.json", raw)      # 元数据先写
            w.add_file("overrides/mods/a.jar", src)
            w.close()                                    # 或 with 退出时自动

    `transform="mca"` 时走 `app.core.mca` 的去外壳变换；变换失败会自动
    退回原样存储（不会让导出失败）。
    """

    def __init__(self, path: Path, *, level: int = DEFAULT_LEVEL,
                 block_size: int = DEFAULT_BLOCK,
                 long_window: bool = True,
                 compress: bool = True,
                 log: Callable[[str, str], None] | None = None):
        self.path = Path(path)
        self.level = max(1, min(22, int(level)))
        self.block_size = max(64 * 1024, int(block_size))
        self._log = log or (lambda _l, _m: None)
        self._compress = compress and _zstd is not None
        self._entries: list[dict] = []
        self._inline: dict[str, str] = {}
        self._blocks: list[list[int]] = []
        self._buf = bytearray()
        self._virtual = 0                 # 未压缩流里的写位置
        self._stats = WriterStats(level=self.level,
                                  fallback_zip=not self._compress)
        self._fh = None
        self._closed = False

        if self._compress:
            # 打开长距离匹配（LDM）：块内跨几十 MB 找重复，
            # 正好吃到"多个 jar 里同一份库"这种冗余。
            try:
                params = _zstd.ZstdCompressionParameters.from_level(
                    self.level, enable_ldm=1 if long_window else 0,
                    window_log=27 if long_window else 0)
                self._cctx = _zstd.ZstdCompressor(
                    compression_params=params, write_checksum=False)
            except Exception:            # 老版本 zstandard 没有这些参数
                self._cctx = _zstd.ZstdCompressor(level=self.level,
                                                  write_checksum=False)
        else:
            self._cctx = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "wb")
        self._fh.write(b"\x00" * _FIXED_HEAD)      # 头部占位，回填

    # -- 内部：把缓冲刷成一块 -------------------------------------------
    def _flush_block(self, force: bool = False):
        if not self._buf:
            return
        if not force and len(self._buf) < self.block_size:
            return
        raw = bytes(self._buf)
        self._buf.clear()
        data = self._cctx.compress(raw) if self._compress else raw
        self._fh.write(data)
        self._blocks.append([len(data), len(raw)])

    def _feed(self, data: bytes):
        """把数据喂进块缓冲，必要时切块。"""
        view = memoryview(data)
        while view:
            room = self.block_size - len(self._buf)
            take = min(room, len(view))
            self._buf += view[:take]
            view = view[take:]
            if len(self._buf) >= self.block_size:
                self._flush_block(force=True)

    # -- 对外：加条目 ---------------------------------------------------
    def add_bytes(self, name: str, data: bytes, *,
                  transform: str | None = None) -> None:
        self._add(name, data, transform, sha1=_sha1(data))

    def add_inline(self, name: str, data: bytes) -> None:
        """
        把**小文件**（index / settings / hashes / changelog）直接放进索引里。

        好处：导入时"只想读元数据"完全不用解压任何块 —— 打开容器就等于
        拿到了它们。代价是索引变大一点（zstd 会压掉大部分）。
        """
        import base64

        self._inline[name] = base64.b64encode(data).decode("ascii")
        self._entries.append({
            "name": name,
            "size": len(data),
            "stored": len(data),
            "sha1": _sha1(data),
            "transform": None,
            "chunks": [],
            "inline": True,
        })
        self._stats.files += 1
        self._stats.logical_bytes += len(data)
        self._stats.stored_bytes += len(data)
        self._stats.entries.append(name)

    def add_transformed(self, name: str, blob: bytes, *,
                        logical_size: int, logical_sha1: str) -> None:
        """
        存"变换后"的字节，但对外仍以逻辑文件的体积/sha1 记账。

        签名和玩家端的核对都按**逻辑内容**（还原后的 `.mca`）算，
        这样存档变换对上层完全透明。
        """
        self._add(name, blob, "mca", sha1=logical_sha1,
                  logical_size=logical_size)

    @property
    def entries(self) -> list[dict]:
        return self._entries

    @property
    def last_entry(self) -> dict:
        """最后写入的条目的元信息（size / sha1 等）。"""
        return self._entries[-1]

    def add_file(self, name: str, src: Path, *,
                 transform: str | None = None) -> None:
        src = Path(src)
        if transform == "mca":
            from . import mca
            blob = mca.transform_file(src)
            if blob is not None:
                data = src.read_bytes()           # 只为算逻辑 sha1
                self.add_transformed(name, blob, logical_size=len(data),
                                     logical_sha1=_sha1(data))
                return
            transform = None                       # 变换失败 → 原样存
        # 普通文件：分块读，避免大文件一次性进内存
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        digest = hashlib.sha1()
        start_block = len(self._blocks)
        start_off = len(self._buf)
        with src.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                self._feed(chunk)
        self._entries.append({
            "name": name,
            "size": size,
            "stored": size,
            "sha1": digest.hexdigest(),
            "transform": None,
            "chunks": self._chunk_refs(start_block, start_off, size),
        })
        self._stats.files += 1
        self._stats.logical_bytes += size
        self._stats.stored_bytes += size
        self._stats.entries.append(name)

    def _add(self, name: str, data: bytes, transform: str | None,
             sha1: str, logical_size: int | None = None):
        start_block = len(self._blocks)
        start_off = len(self._buf)
        self._feed(data)
        size = len(data) if logical_size is None else logical_size
        self._entries.append({
            "name": name,
            "size": size,
            "stored": len(data),
            "sha1": sha1,
            "transform": transform,
            "chunks": self._chunk_refs(start_block, start_off, len(data)),
        })
        self._stats.files += 1
        self._stats.logical_bytes += size
        self._stats.stored_bytes += len(data)
        self._stats.entries.append(name)
        if transform:
            self._stats.transformed += 1

    def _chunk_refs(self, start_block: int, start_off: int,
                    length: int) -> list[list[int]]:
        """把"流里的连续区间"换算成 [(块号, 块内偏移, 长度), ...]。"""
        refs: list[list[int]] = []
        pos = 0
        blk = start_block
        off = start_off
        while pos < length:
            room = self.block_size - off
            take = min(room, length - pos)
            refs.append([blk, off, take])
            pos += take
            blk += 1
            off = 0
        return refs

    # -- 收尾 -----------------------------------------------------------
    def close(self) -> WriterStats:
        if self._closed:
            return self._stats
        self._closed = True
        self._flush_block(force=True)

        header = {
            "format_version": FORMAT_VERSION,
            "block_size": self.block_size,
            "blocks": self._blocks,
            "entries": self._entries,
            "inline": self._inline,
        }
        raw = json.dumps(header, ensure_ascii=False).encode("utf-8")
        comp = self._cctx.compress(raw) if self._compress else raw
        header_off = self._fh.tell()
        self._fh.write(comp)
        self._fh.seek(0)
        self._fh.write(MAGIC)
        self._fh.write(struct.pack(">IIII", header_off, len(comp), len(raw),
                                   FORMAT_VERSION))
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass
        self._fh.close()
        self._stats.compressed_bytes = self.path.stat().st_size
        if self._stats.files:
            self._log("info",
                      f"容器：{self._stats.files} 个文件，"
                      f"原始 {self._stats.logical_bytes/1048576:.1f} MB → "
                      f"{self._stats.compressed_bytes/1048576:.1f} MB"
                      + (f"（{self._stats.transformed} 个存档区域已优化）"
                         if self._stats.transformed else ""))
        return self._stats

    def abort(self):
        """出错时调用：关闭并删掉半成品。"""
        try:
            if self._fh and not self._closed:
                self._fh.close()
        except Exception:  # noqa: BLE001, S110
            pass
        self._closed = True
        try:
            self.path.unlink()
        except OSError:  # noqa: PERF203
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.close()
        else:
            self.abort()
        return False


# ----------------------------------------------------------------------
# 读
# ----------------------------------------------------------------------
class ContainerReader:
    """统一读取接口：v2 容器与 zip 都实现它。"""

    names: list[str] = []
    entries: dict[str, dict] = {}

    def read(self, name: str) -> bytes: raise NotImplementedError
    def iter_chunks(self, name: str) -> Iterator[bytes]: raise NotImplementedError
    def info(self, name: str) -> dict: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False


class Eapk2Reader(ContainerReader):
    """块压缩容器读取器（带 1 块 LRU 缓存）。"""

    def __init__(self, path: Path, head: bytes | None = None):
        self.path = Path(path)
        self._fh = open(self.path, "rb")
        head = head or self._fh.read(_FIXED_HEAD)
        if not head.startswith(MAGIC):
            self._fh.close()
            raise ContainerError("不是 EAPK2 容器")
        (header_off, comp_len, raw_len,
         version) = struct.unpack_from(">IIII", head, len(MAGIC))
        if version > FORMAT_VERSION:
            self._fh.close()
            raise ContainerError(f"容器版本过新：{version}")
        self._data_start = _FIXED_HEAD
        self._fh.seek(header_off)
        blob = self._fh.read(comp_len)
        if len(blob) != comp_len:
            self._fh.close()
            raise ContainerError("索引块被截断")
        try:
            if _zstd is not None:
                raw = _zstd.ZstdDecompressor().decompress(
                    blob, max_output_size=max(1, raw_len))
            else:
                raw = blob
            header = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self._fh.close()
            raise ContainerError(f"索引块损坏：{e}") from e
        self.block_size = int(header.get("block_size") or DEFAULT_BLOCK)
        self._blocks: list[list[int]] = header.get("blocks") or []
        self._offsets: list[int] = []
        pos = self._data_start
        for comp, _raw in self._blocks:
            self._offsets.append(pos)
            pos += comp
        self._inline: dict[str, str] = header.get("inline") or {}
        self.entries = {e["name"]: e for e in header.get("entries") or []}
        self.names = list(self.entries)
        self._cache_idx = -1
        self._cache = b""
        self._zctx = (_zstd.ZstdDecompressor()
                      if _zstd is not None else None)

    # -- 块 -------------------------------------------------------------
    def _block(self, idx: int) -> bytes:
        if idx == self._cache_idx:
            return self._cache
        if idx < 0 or idx >= len(self._blocks):
            raise ContainerError(f"块号越界：{idx}")
        comp_len, raw_len = self._blocks[idx]
        self._fh.seek(self._offsets[idx])
        data = self._fh.read(comp_len)
        if len(data) != comp_len:
            raise ContainerError(f"块 {idx} 被截断")
        try:
            raw = (self._zctx.decompress(data, max_output_size=max(1, raw_len))
                   if self._zctx is not None else data)
        except Exception as e:  # noqa: BLE001
            raise ContainerError(f"块 {idx} 损坏：{e}") from e
        self._cache_idx = idx
        self._cache = raw
        return raw

    # -- 条目 -----------------------------------------------------------
    def info(self, name: str) -> dict:
        return self.entries[name]

    def iter_chunks(self, name: str) -> Iterator[bytes]:
        ent = self.entries.get(name)
        if ent is None:
            raise KeyError(name)
        if ent.get("inline"):
            import base64
            yield base64.b64decode(self._inline[name])
            return
        if ent.get("transform") == "mca":
            # 存档变换是"整文件可逆"的，必须先拼齐再还原。
            # 区域文件通常 1~20 MB，一次读进来没问题。
            from . import mca
            raw = b"".join(self._block(blk)[off:off + length]
                           for blk, off, length in ent["chunks"])
            yield mca.restore_region(raw)
            return
        for blk, off, length in ent["chunks"]:
            yield self._block(blk)[off:off + length]

    def read(self, name: str) -> bytes:
        # iter_chunks 已经处理了 inline / transform，这里只负责拼接
        return b"".join(self.iter_chunks(name))

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001, S110
            pass


class ZipReader(ContainerReader):
    """老格式（zip）读取器，让调用方不需要关心容器类型。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._zf = zipfile.ZipFile(self.path, "r")
        self.names = [i.filename for i in self._zf.infolist()]
        self.entries = {
            i.filename: {"name": i.filename, "size": i.file_size,
                         "stored": i.compress_size, "sha1": None,
                         "transform": None, "chunks": []}
            for i in self._zf.infolist()
        }

    def info(self, name: str) -> dict:
        return self.entries[name]

    def iter_chunks(self, name: str) -> Iterator[bytes]:
        with self._zf.open(name) as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    return
                yield chunk

    def read(self, name: str) -> bytes:
        return self._zf.read(name)

    def close(self) -> None:
        try:
            self._zf.close()
        except Exception:  # noqa: BLE001, S110
            pass


def is_v2(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def open_container(path: Path) -> ContainerReader:
    """打开任意支持格式的更新包（v2 容器 / zip）。"""
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(max(_FIXED_HEAD, len(MAGIC)))
    if head[:len(MAGIC)] == MAGIC:
        return Eapk2Reader(path, head[: _FIXED_HEAD])
    return ZipReader(path)


def read_meta(path: Path, names: tuple[str, ...]) -> dict[str, bytes]:
    """只读少量元数据文件（v2 只需解压第一块）。"""
    out: dict[str, bytes] = {}
    with open_container(path) as reader:
        for name in names:
            if name in reader.entries:
                try:
                    out[name] = reader.read(name)
                except Exception:  # noqa: BLE001, S110
                    pass
    return out
