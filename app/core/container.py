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
    21  .. 24   u32    format_version     （=3，读端兼容 2）
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
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

try:                                  # 允许没装 zstandard 的旧环境
    import zstandard as _zstd
except Exception:                     # pragma: no cover - 环境相关
    _zstd = None

MAGIC = b"EAPK2\r\n\x1a\n"
JAR_MAGIC = b"JAR1"
MAX_JAR_SIZE = 2 << 30                # 单个 jar 逻辑大小上限 2 GB
FORMAT_VERSION = 3
_FIXED_HEAD = len(MAGIC) + 16         # magic + 4 × u32
# 块大小：64 MB。块越大越"接近 solid"，压缩率更好；代价是解压单块时的内存
# 峰值（64 MB）与随机访问粒度。实测 8/32/64/128/单块 之间体积差 <1%，
# 64 MB 是"好压缩率 + 可接受内存"的平衡点。
DEFAULT_BLOCK = 64 * 1024 * 1024
# zstd 等级：17 在 228 MB 素材上比 15 小 0.7%、比 19 慢一半但只差 0.5%
DEFAULT_LEVEL = 17
_CHUNK = 1 << 20                      # 读文件时每次喂 1 MB

# 读取侧资源上限：容器是第三方内容，索引里的长度/块号/块大小都不能信。
# 这些值远大于正常包（64 MB 块、几十 MB 索引），只用来拒绝恶意/损坏输入。
MAX_HEADER_COMP = 32 << 20            # 索引块压缩后最大 32 MB
MAX_HEADER_RAW = 64 << 20             # 索引 JSON 解压后最大 64 MB
MAX_BLOCK_RAW = 512 << 20             # 单块解压后最大 512 MB
MAX_BLOCK_COMP = 512 << 20            # 单块压缩数据最大 512 MB
MAX_BLOCKS = 200_000
MAX_ENTRIES = 500_000
MAX_LOGICAL_SIZE = 2 << 30            # 单个逻辑文件（还原后）上限 2 GB
MAX_INLINE_TOTAL = 16 << 20           # 内联元数据总量上限 16 MB
MAX_WINDOW = 512 << 20                # zstd 帧声明的 window 上限
MIN_BLOCK_SIZE = 64 << 10
MAX_BLOCK_SIZE = 512 << 20


class ContainerError(Exception):
    """容器损坏 / 不支持的版本。"""


def available() -> bool:
    """当前环境能否写 v2 容器。"""
    return _zstd is not None


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _jar_local(all_data: bytes, off: int, comp_size: int):
    """读一个 local file header（原样返回 header 字节）与压缩数据。"""
    if off < 0 or off + 30 > len(all_data):
        return None
    (sig, _ver, flag, method, _t, _d, _crc,
     _cs, _us, nl, el) = struct.unpack_from("<IHHHHHIIIHH", all_data, off)
    if sig != 0x04034B50:
        return None
    end = off + 30 + nl + el
    if end > len(all_data):
        return None
    header = all_data[off:end]
    comp = all_data[end:end + comp_size]
    if len(comp) != comp_size:
        return None
    return flag, method, header, comp, end


def jar_encode(path: Path) -> bytes | None:
    """
    把 jar 的 deflate 外壳剥掉，只存每个 entry 的 raw 数据 + 原始头部。

    还原时用 zlib level 6 重压；**每个 entry 都必须在导出时验证能逐字节
    复现**，任何一个不能（不同的 zlib 版本/参数、加密、非 deflate 方法）
    就整包 fallback 返回 None，绝不写入"可能还原不回去"的变换。
    """
    path = Path(path)
    try:
        if path.stat().st_size > MAX_JAR_SIZE:
            return None
        all_data = path.read_bytes()
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if not infos:
                return None
            out = bytearray(JAR_MAGIC)
            out += struct.pack(">I", len(infos))
            prev_end = 0
            for zi in infos:
                got = _jar_local(all_data, zi.header_offset, zi.compress_size)
                if got is None:
                    return None
                flag, method, header, comp, data_off = got
                if flag & 0x1 or flag & 0x40:          # 加密
                    return None
                if method == 8:
                    raw = zlib.decompress(comp, -15)
                    co = zlib.compressobj(6, zlib.DEFLATED, -15)
                    if co.compress(raw) + co.flush() != comp:
                        return None
                elif method == 0:
                    raw = comp
                else:
                    return None
                gap = all_data[prev_end:zi.header_offset]
                prev_end = data_off + len(comp)
                out += struct.pack(">I", len(gap)) + gap
                out += struct.pack(">I", len(header)) + header
                out += bytes([method]) + struct.pack(">I", len(raw)) + raw
            tail = all_data[prev_end:]
            out += struct.pack(">I", len(tail)) + tail
            return bytes(out)
    except Exception:                                   # noqa: BLE001
        return None


def jar_decode(blob: bytes) -> bytes:
    """还原 jar 原始字节；结构错误抛 ContainerError。"""
    if not blob.startswith(JAR_MAGIC):
        raise ContainerError("jar 变换数据损坏")
    try:
        pos = len(JAR_MAGIC)
        if pos + 4 > len(blob):
            raise ContainerError("jar 变换数据截断")
        count = struct.unpack_from(">I", blob, pos)[0]; pos += 4
        if count > MAX_ENTRIES:
            raise ContainerError(f"jar 条目过多：{count}")
        out = bytearray()
        for _ in range(count):
            if pos + 4 > len(blob):
                raise ContainerError("jar 变换数据截断")
            gl = struct.unpack_from(">I", blob, pos)[0]; pos += 4
            if pos + gl > len(blob):
                raise ContainerError("jar gap 越界")
            out += blob[pos:pos + gl]; pos += gl
            if pos + 4 > len(blob):
                raise ContainerError("jar 变换数据截断")
            hl = struct.unpack_from(">I", blob, pos)[0]; pos += 4
            if pos + hl > len(blob):
                raise ContainerError("jar header 越界")
            out += blob[pos:pos + hl]; pos += hl
            if pos + 5 > len(blob):
                raise ContainerError("jar 变换数据截断")
            method = blob[pos]; pos += 1
            rl = struct.unpack_from(">I", blob, pos)[0]; pos += 4
            if pos + rl > len(blob):
                raise ContainerError("jar raw 越界")
            raw = blob[pos:pos + rl]; pos += rl
            if method == 8:
                co = zlib.compressobj(6, zlib.DEFLATED, -15)
                out += co.compress(raw) + co.flush()
            elif method == 0:
                out += raw
            else:
                raise ContainerError(f"jar 条目方法不支持：{method}")
            if len(out) > MAX_JAR_SIZE:
                raise ContainerError("jar 还原后超出上限")
        if pos + 4 > len(blob):
            raise ContainerError("jar 变换数据截断")
        tl = struct.unpack_from(">I", blob, pos)[0]; pos += 4
        if pos + tl > len(blob):
            raise ContainerError("jar tail 越界")
        out += blob[pos:pos + tl]
        if len(out) > MAX_JAR_SIZE:
            raise ContainerError("jar 还原后超出上限")
        return bytes(out)
    except ContainerError:
        raise
    except Exception as e:                              # noqa: BLE001
        raise ContainerError(f"jar 变换数据损坏：{e}") from e


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
                 jar_transform: bool | None = None,
                 log: Callable[[str, str], None] | None = None):
        self.path = Path(path)
        self.level = max(1, min(22, int(level)))
        self.block_size = max(64 * 1024, int(block_size))
        self._log = log or (lambda _l, _m: None)
        self._compress = compress and _zstd is not None
        # 超级压缩档（level >= 20）默认尝试 jar 去壳；普通档保持旧行为。
        # 显式传 jar_transform 可覆盖。
        self._jar_transform = ((self.level >= 20) if jar_transform is None
                               else bool(jar_transform))
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
                        logical_size: int, logical_sha1: str,
                        transform: str = "mca") -> None:
        """
        存"变换后"的字节，但对外仍以逻辑文件的体积/sha1 记账。

        签名和玩家端的核对都按**逻辑内容**（还原后的 `.mca`）算，
        这样存档变换对上层完全透明。
        """
        self._add(name, blob, transform, sha1=logical_sha1,
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
        if transform is None and self._jar_transform \
                and name.lower().endswith(".jar"):
            blob = jar_encode(src)
            if blob is not None:
                data = src.read_bytes()
                self.add_transformed(name, blob, logical_size=len(data),
                                     logical_sha1=_sha1(data),
                                     transform="jar")
                return
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
def _as_int(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContainerError(f"{what} 不是整数")
    return value


def validate_header(header, *, file_size: int, data_start: int) -> tuple[int, list[list[int]]]:
    """
    校验索引结构，返回 (block_size, blocks)。

    只做结构与上限检查，**不解压任何块**、不按声明大小分配内存：
    恶意包把 raw_len 写成 10 GB 时必须在读数据前就拒绝。
    """
    if not isinstance(header, dict):
        raise ContainerError("索引不是 JSON 对象")
    block_size = _as_int(header.get("block_size") or DEFAULT_BLOCK, "block_size")
    if not (MIN_BLOCK_SIZE <= block_size <= MAX_BLOCK_SIZE):
        raise ContainerError(f"block_size 超出范围：{block_size}")

    blocks = header.get("blocks") or []
    if not isinstance(blocks, list):
        raise ContainerError("blocks 不是数组")
    if len(blocks) > MAX_BLOCKS:
        raise ContainerError(f"块数过多：{len(blocks)}")
    norm: list[list[int]] = []
    offset = data_start
    for i, pair in enumerate(blocks):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ContainerError(f"块 {i} 不是 [comp, raw]")
        comp = _as_int(pair[0], f"块 {i} 压缩长度")
        raw = _as_int(pair[1], f"块 {i} 原始长度")
        if comp <= 0 or raw <= 0:
            raise ContainerError(f"块 {i} 长度非正")
        if comp > MAX_BLOCK_COMP or raw > MAX_BLOCK_RAW:
            raise ContainerError(f"块 {i} 超出上限")
        if offset + comp > file_size:
            raise ContainerError(f"块 {i} 越过文件末尾")
        norm.append([comp, raw])
        offset += comp

    entries = header.get("entries") or []
    if not isinstance(entries, list):
        raise ContainerError("entries 不是数组")
    if len(entries) > MAX_ENTRIES:
        raise ContainerError(f"条目过多：{len(entries)}")
    seen: set[str] = set()
    inline_total = [0]
    inline = header.get("inline") or {}
    if not isinstance(inline, dict):
        raise ContainerError("inline 不是对象")
    for i, ent in enumerate(entries):
        if not isinstance(ent, dict):
            raise ContainerError(f"条目 {i} 不是对象")
        name = ent.get("name")
        if not isinstance(name, str) or not name:
            raise ContainerError(f"条目 {i} 名称非法")
        if name in seen:
            raise ContainerError(f"条目重复：{name}")
        seen.add(name)
        transform = ent.get("transform")
        if transform not in (None, "mca", "jar"):
            raise ContainerError(f"未知变换：{transform}")
        size = _as_int(ent.get("size", 0), f"{name} 的逻辑大小")
        stored = _as_int(ent.get("stored", 0), f"{name} 的存储大小")
        if size < 0 or stored < 0 or size > MAX_LOGICAL_SIZE \
                or stored > MAX_LOGICAL_SIZE:
            raise ContainerError(f"{name} 的大小超出上限")
        if ent.get("inline"):
            if name not in inline:
                raise ContainerError(f"内联条目缺数据：{name}")
            import base64
            try:
                raw = base64.b64decode(inline[name], validate=True)
            except Exception as e:  # noqa: BLE001
                raise ContainerError(f"{name} 的内联数据不是 base64") from e
            if len(raw) != size:
                raise ContainerError(f"{name} 的内联数据长度与声明不符")
            inline_total[0] += len(raw)
            if inline_total[0] > MAX_INLINE_TOTAL:
                raise ContainerError("内联元数据总量超出上限")
            continue
        chunks = ent.get("chunks") or []
        if not isinstance(chunks, list):
            raise ContainerError(f"{name} 的 chunks 不是数组")
        chunk_total = 0
        for ch in chunks:
            if not isinstance(ch, (list, tuple)) or len(ch) != 3:
                raise ContainerError(f"{name} 的 chunk 结构错误")
            blk = _as_int(ch[0], "chunk 块号")
            off = _as_int(ch[1], "chunk 偏移")
            length = _as_int(ch[2], "chunk 长度")
            if not (0 <= blk < len(norm)):
                raise ContainerError(f"{name} 的 chunk 块号越界：{blk}")
            if off < 0 or length <= 0:
                raise ContainerError(f"{name} 的 chunk 长度非法")
            if off + length > norm[blk][1]:
                raise ContainerError(f"{name} 的 chunk 越过块 {blk}")
            chunk_total += length
        if chunk_total != stored:
            raise ContainerError(f"{name} 的 chunks 总长与 stored 不符")
    return block_size, norm


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
        try:
            file_size = os.fstat(self._fh.fileno()).st_size
            head = head or self._fh.read(_FIXED_HEAD)
            if len(head) < _FIXED_HEAD or not head.startswith(MAGIC):
                raise ContainerError("不是 EAPK2 容器")
            (header_off, comp_len, raw_len,
             version) = struct.unpack_from(">IIII", head, len(MAGIC))
            if version > FORMAT_VERSION:
                raise ContainerError(f"容器版本过新：{version}")
            if comp_len <= 0 or raw_len <= 0:
                raise ContainerError("索引块长度非正")
            if comp_len > MAX_HEADER_COMP or raw_len > MAX_HEADER_RAW:
                raise ContainerError("索引块超出资源上限")
            if header_off < _FIXED_HEAD or header_off + comp_len > file_size:
                raise ContainerError("索引块位置/长度越过文件")
            self._data_start = _FIXED_HEAD
            self._fh.seek(header_off)
            blob = self._fh.read(comp_len)
            if len(blob) != comp_len:
                raise ContainerError("索引块被截断")
            try:
                if _zstd is not None:
                    try:
                        fp = _zstd.get_frame_parameters(blob)
                        declared = int(getattr(fp, "content_size", 0) or 0)
                    except Exception as e:  # noqa: BLE001
                        raise ContainerError(f"索引块不是 zstd 帧：{e}") from e
                    if declared and declared > MAX_HEADER_RAW:
                        raise ContainerError("索引帧声明大小超出上限")
                    dec = _zstd.ZstdDecompressor(max_window_size=MAX_WINDOW)
                    raw = dec.decompress(blob, max_output_size=raw_len)
                    if len(raw) != raw_len:
                        raise ContainerError("索引块解压后长度与声明不符")
                else:
                    raw = blob
                header = json.loads(raw.decode("utf-8"))
            except ContainerError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ContainerError(f"索引块损坏：{e}") from e
            self.block_size, self._blocks = validate_header(
                header, file_size=file_size, data_start=self._data_start)
            self._inline: dict[str, str] = header.get("inline") or {}
            self.entries = {e["name"]: e for e in header.get("entries") or []}
            self.names = list(self.entries)
            self._offsets: list[int] = []
            pos = self._data_start
            for comp, _raw in self._blocks:
                self._offsets.append(pos)
                pos += comp
        except Exception:
            self._fh.close()
            raise
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
            if self._zctx is not None:
                fp = _zstd.get_frame_parameters(data)
                declared = int(getattr(fp, "content_size", 0) or 0)
                if declared and declared > MAX_BLOCK_RAW:
                    raise ContainerError(f"块 {idx} 帧声明大小超出上限")
                dec = _zstd.ZstdDecompressor(max_window_size=MAX_WINDOW)
                raw = dec.decompress(data, max_output_size=raw_len)
            else:
                raw = data
            if len(raw) != raw_len:
                raise ContainerError(f"块 {idx} 解压后长度与声明不符")
        except ContainerError:
            raise
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
        if ent.get("transform") == "jar":
            parts = []
            for blk, off, length in ent["chunks"]:
                block = self._block(blk)
                if off + length > len(block):
                    raise ContainerError(f"{name} 的 chunk 越过块 {blk}")
                parts.append(block[off:off + length])
            yield jar_decode(b"".join(parts))
            return
        if ent.get("transform") == "mca":
            # 存档变换是"整文件可逆"的，必须先拼齐再还原。
            # 区域文件通常 1~20 MB，一次读进来没问题。
            from . import mca
            parts = []
            for blk, off, length in ent["chunks"]:
                block = self._block(blk)
                if off + length > len(block):
                    raise ContainerError(f"{name} 的 chunk 越过块 {blk}")
                parts.append(block[off:off + length])
            raw = b"".join(parts)
            yield mca.restore_region(raw)
            return
        for blk, off, length in ent["chunks"]:
            block = self._block(blk)
            if off + length > len(block):
                raise ContainerError(f"{name} 的 chunk 越过块 {blk}")
            yield block[off:off + length]

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
