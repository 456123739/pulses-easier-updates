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
    """
    是不是可变换的存档区域文件。

    范围收紧到 **saves/ 下的 region|poi/*.mca|.mcr**：包内路径可能是
    `overrides/saves/World/region/r.0.0.mca`，也可能没有 overrides 前缀。
    其他目录（例如 mods 里自带的 region 文件、资源包结构）一律不动，
    避免把不相干的字节做变换。
    """
    low = str(name).replace("\\", "/").lower()
    if not low.endswith((".mca", ".mcr")):
        return False
    parts = [p for p in low.split("/") if p]
    if parts and parts[0] == "overrides":
        parts = parts[1:]
    if not parts or parts[0] != "saves":
        return False
    return any(p in ("region", "poi") for p in parts[1:-1])


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


# 恶意/损坏输入的上限：超过一律 ValueError，绝不按声明值预分配。
MAX_FILE_BYTES = 1 << 30          # 单个 region 文件上限 1 GiB
MAX_RAW_CHUNK = 64 << 20          # 单个区块裸 NBT 上限 64 MiB
MAX_BLOCK_COUNT = 1024            # 头部最多 1024 个槽位
MAX_GAP_COUNT = 65536


def restore_region(blob: bytes) -> bytes:
    """
    `strip_region` 的逆操作；结果与原始 `.mca` 逐字节相同。

    这是玩家端解包时会走的路径，输入可能来自第三方更新包，所以所有
    长度/偏移都先校验再分配：file_len、裸区块长度、空洞数量/范围、
    扇区跨度都要在 blob 与 file_len 的范围内，否则直接 ValueError
    （调用方按"原样存/拒绝"处理，不会巨量分配）。
    """
    if not blob.startswith(_MAGIC):
        raise ValueError("不是 mca 变换流")
    pos = len(_MAGIC)
    if len(blob) < pos + 8 + _HEADER_SIZE:
        raise ValueError("mca 变换流头部不完整")
    file_len, count = struct.unpack_from(">II", blob, pos)
    pos += 8
    if file_len < _HEADER_SIZE or file_len > MAX_FILE_BYTES:
        raise ValueError("mca 声明长度不合法")
    # 合法变换流的原始大小与输入大小同量级（去壳只会更小）；给 64 倍余量，
    # 防止 20 字节的恶意头声明 1 GiB 触发巨量分配。
    if file_len > max(1 << 20, len(blob) * 64):
        raise ValueError("mca 声明长度与数据量不符")
    if count > MAX_BLOCK_COUNT:
        raise ValueError("mca 区块数过多")
    header = blob[pos:pos + _HEADER_SIZE]
    pos += _HEADER_SIZE
    if len(header) != _HEADER_SIZE:
        raise ValueError("mca 变换流头部不完整")

    slots = list(_iter_slots(header))
    if len(slots) != count:
        raise ValueError(f"mca 区块数不符：头部 {len(slots)} / 流 {count}")
    spans: list[tuple[int, int, int]] = []
    for _idx, start, sectors in slots:
        span = (sectors or 1) * _SECTOR
        if start < _HEADER_SIZE or span <= 0 or start + span > file_len:
            raise ValueError("mca 扇区越界")
        spans.append((start, sectors, span))

    out = bytearray(file_len)
    out[0:_HEADER_SIZE] = header

    for start, _sectors, span in spans:
        if pos >= len(blob):
            raise ValueError("mca 数据不完整")
        ctype = blob[pos]
        pos += 1
        if ctype not in (_TYPE_ZLIB, 1, 3):
            raise ValueError("mca 压缩类型不支持")
        try:
            n, pos = _read_vint(blob, pos)
        except (IndexError, ValueError) as exc:
            raise ValueError("mca 区块长度损坏") from exc
        if n < 0 or n > MAX_RAW_CHUNK or pos + n > len(blob):
            raise ValueError("mca 区块裸数据越界")
        raw = blob[pos:pos + n]
        pos += n
        try:
            m, pos = _read_vint(blob, pos)
        except (IndexError, ValueError) as exc:
            raise ValueError("mca 扇区尾巴长度损坏") from exc
        if m < 0 or pos + m > len(blob) or m > span:
            raise ValueError("mca 扇区尾巴越界")
        tail = blob[pos:pos + m]
        pos += m

        payload = chunk_compress(raw, ctype)
        rec = struct.pack(">I", len(payload) + 1) + bytes([ctype]) + payload
        if len(rec) > span:
            raise ValueError("mca 区块超出扇区跨度（数据损坏）")
        out[start:start + len(rec)] = rec
        out[start + len(rec): start + span] = tail

    try:
        gap_count, pos = _read_vint(blob, pos)
    except (IndexError, ValueError) as exc:
        raise ValueError("mca 空洞数量损坏") from exc
    if gap_count < 0 or gap_count > MAX_GAP_COUNT:
        raise ValueError("mca 空洞数量过多")
    for _ in range(gap_count):
        try:
            start, pos = _read_vint(blob, pos)
            n, pos = _read_vint(blob, pos)
        except (IndexError, ValueError) as exc:
            raise ValueError("mca 空洞损坏") from exc
        if start < 0 or n < 0 or start + n > file_len or pos + n > len(blob):
            raise ValueError("mca 空洞越界")
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
