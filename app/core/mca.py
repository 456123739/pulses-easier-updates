"""
mca.py — Minecraft 区域文件（.mca / .mcr）的去压缩外壳变换
================================================================
**这是做什么的**

Minecraft 把每个区块单独用 zlib 压一次再写进 `.mca`。这层压缩对存档本身
是必要的，但对"把存档打包进更新包"是有害的：压缩器看到的是已经被 zlib
打散的字节，跨区块的相似性被外壳隔断，只能吃到很少的冗余。

实测（真实 1.20.1 地形世界，24.7 MB / 4093 区块）：

    原始 .mca  ——zip -9→  57.4%
    剥掉外壳   ——xz  -9→  30.5%     （zstd -19 → 34.7%）

**怎么保证安全**

1. 只做"去掉 zlib 外壳"，不解析、不改写 NBT 语义。
2. **逐字节可逆**：`restore(strip(x)) == x` 对整个文件成立，玩家端继续用
   原来的 sha1 校验，不需要任何"特殊文件"分支。
3. `.mca` 的扇区里除了压缩块，还可能残留"上次写入留下的陈旧字节"
   （MC 只覆盖新记录的长度，扇区尾巴不清理）。这些字节一起存，
   否则复原出来的不是同一份字节。
4. 导出时**自校验**：真跑一遍 restore 比对 sha1，不一致就原样存
   （`strip_region` 返回 None）。不同 MC 版本 / 不同
   `region-file-compression-level` 的存档因此不会被打坏。

**为什么不顺手删空 section / Heightmaps**

那能再小 2~7%，但会改变存档内容（虽然游戏会重算）。这里只做绝对安全的
那一层，NBT 级优化留给将来的可选开关。
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

_MAGIC = b"MCAR1"
_HEADER_SIZE = 8192          # region 头：1024 个偏移 + 1024 个时间戳
_SECTOR = 4096
_TYPE_ZLIB = 2
_DEFAULT_LEVEL = 6           # MC 客户端 Deflater 默认档；实测与存档一致


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------
def is_region_name(name: str) -> bool:
    """是不是可变换的区域文件（region/ 与 poi/ 下的 .mca/.mcr）。"""
    low = str(name).replace("\\", "/").lower()
    if not low.endswith((".mca", ".mcr")):
        return False
    parts = low.split("/")
    return any(p in ("region", "poi") for p in parts[:-1])


def _vint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_vint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 63:
            raise ValueError("变长整数溢出")


def chunk_decompress(payload: bytes, ctype: int) -> bytes:
    """按 region 记录里的类型字节解压（1=gzip, 2=zlib, 3=裸）。"""
    if ctype == 1:
        return zlib.decompress(payload, 16 + zlib.MAX_WBITS)
    if ctype == 2:
        return zlib.decompress(payload)
    if ctype == 3:
        return payload
    raise ValueError(f"不支持的区块压缩类型：{ctype}")


def chunk_compress(raw: bytes, ctype: int,
                   level: int = _DEFAULT_LEVEL) -> bytes:
    if ctype == 3:
        return raw
    if ctype == 1:
        co = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        return co.compress(raw) + co.flush()
    co = zlib.compressobj(level, zlib.DEFLATED, zlib.MAX_WBITS)
    return co.compress(raw) + co.flush()


def _iter_slots(header: bytes):
    """按偏移表顺序产出 (idx, sector_offset, sector_count)，只含已分配项。"""
    for idx in range(1024):
        off = struct.unpack_from(">I", header, idx * 4)[0]
        if off == 0:
            continue
        yield idx, (off >> 8) * _SECTOR, off & 0xFF


# ----------------------------------------------------------------------
# 变换
# ----------------------------------------------------------------------
def strip_region(data: bytes) -> bytes | None:
    """
    把 `.mca` 变成"头 + 逐区块裸 NBT + 扇区冗余"的可压流。

    返回 None 表示不适合变换（结构不认识 / 自校验不通过），调用方原样存。
    """
    if len(data) < _HEADER_SIZE:
        return None
    header = data[:_HEADER_SIZE]

    slots = list(_iter_slots(header))
    if not slots:
        return None

    body = bytearray()
    count = 0
    spans: list[tuple[int, int]] = []
    for _idx, start, sectors in slots:
        if start + 5 > len(data):
            return None
        length = struct.unpack_from(">I", data, start)[0]
        if length < 1 or start + 4 + length > len(data):
            return None
        ctype = data[start + 4]
        payload = data[start + 5: start + 4 + length]
        try:
            raw = chunk_decompress(payload, ctype)
        except Exception:  # noqa: BLE001
            return None

        span = (sectors or 1) * _SECTOR
        used = 4 + length
        if used > span:
            return None
        tail = data[start + used: start + span]

        body += bytes([ctype])
        body += _vint(len(raw))
        body += raw
        body += _vint(len(tail))
        body += tail
        spans.append((start, start + span))
        count += 1

    # 空洞：被释放的旧扇区 / 文件尾部的多余字节。它们不是"逻辑内容"，
    # 但为了**逐字节可逆**必须一起存（MC 不会清理这些位置）。
    gaps: list[tuple[int, bytes]] = []
    pos = _HEADER_SIZE
    for start, end in sorted(spans):
        if start > pos:
            gaps.append((pos, data[pos:start]))
        pos = max(pos, end)
    if pos < len(data):
        gaps.append((pos, data[pos:]))

    body += _vint(len(gaps))
    for start, chunk in gaps:
        body += _vint(start)
        body += _vint(len(chunk))
        body += chunk

    if not count:
        return None
    return (_MAGIC
            + struct.pack(">II", len(data), count)
            + header
            + bytes(body))


def restore_region(blob: bytes) -> bytes:
    """`strip_region` 的逆操作；结果与原始 `.mca` 逐字节相同。"""
    if not blob.startswith(_MAGIC):
        raise ValueError("不是 mca 变换流")
    pos = len(_MAGIC)
    file_len, count = struct.unpack_from(">II", blob, pos)
    pos += 8
    header = blob[pos:pos + _HEADER_SIZE]
    pos += _HEADER_SIZE
    if len(header) != _HEADER_SIZE:
        raise ValueError("mca 变换流头部不完整")

    out = bytearray(file_len)
    out[0:_HEADER_SIZE] = header

    slots = list(_iter_slots(header))
    if len(slots) != count:
        raise ValueError(f"mca 区块数不符：头部 {len(slots)} / 流 {count}")

    for _idx, start, sectors in slots:
        ctype = blob[pos]
        pos += 1
        n, pos = _read_vint(blob, pos)
        raw = blob[pos:pos + n]
        pos += n
        m, pos = _read_vint(blob, pos)
        tail = blob[pos:pos + m]
        pos += m

        payload = chunk_compress(raw, ctype)
        rec = struct.pack(">I", len(payload) + 1) + bytes([ctype]) + payload
        span = (sectors or 1) * _SECTOR
        if len(rec) > span:
            raise ValueError("mca 区块超出扇区跨度（数据损坏）")
        out[start:start + len(rec)] = rec
        out[start + len(rec): start + span] = tail

    gap_count, pos = _read_vint(blob, pos)
    for _ in range(gap_count):
        start, pos = _read_vint(blob, pos)
        n, pos = _read_vint(blob, pos)
        out[start:start + n] = blob[pos:pos + n]
        pos += n
    return bytes(out)


def transform_file(path: Path) -> bytes | None:
    """
    读文件 → strip；并**自校验**（restore 后 sha1 必须与原文一致）。
    不一致 / 结构不认识 → None（调用方原样存）。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    blob = strip_region(data)
    if blob is None:
        return None
    try:
        if hashlib.sha1(restore_region(blob)).digest() != \
                hashlib.sha1(data).digest():
            return None
    except Exception:  # noqa: BLE001
        return None
    return blob


def looks_transformed(blob: bytes) -> bool:
    return blob[:len(_MAGIC)] == _MAGIC
