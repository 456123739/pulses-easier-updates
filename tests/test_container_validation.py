"""EAPK2 读取器的结构与资源上限校验（恶意/损坏输入不能撑爆内存）。"""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

import zstandard as zstd

from app.core import container as ct


def _write(path: Path, header: dict, *, raw_len=None, comp_len=None,
           header_off=None, version=2, extra: bytes = b"") -> None:
    hb = json.dumps(header).encode("utf-8")
    comp = zstd.ZstdCompressor().compress(hb)
    head_off = len(ct.MAGIC) + 16 if header_off is None else header_off
    with open(path, "wb") as f:
        f.write(ct.MAGIC)
        f.write(struct.pack(">IIII",
                            head_off,
                            len(comp) if comp_len is None else comp_len,
                            len(hb) if raw_len is None else raw_len,
                            version))
        f.write(comp)
        f.write(extra)


BASE = {
    "block_size": 64 * 1024 * 1024,
    "blocks": [],
    "inline": {},
    "entries": [],
}


class ReaderValidationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pe-cval-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def _open(self, **kw):
        p = self.tmp / "bad.eapack"
        _write(p, kw.pop("header", dict(BASE)), **kw)
        with self.assertRaises(ct.ContainerError):
            ct.open_container(p)

    def test_valid_roundtrip(self):
        p = self.tmp / "ok.eapack"
        src = self.tmp / "a.bin"; src.write_bytes(b"hello" * 100)
        w = ct.ContainerWriter(p, level=3)
        w.add_bytes("meta.json", b"{}")
        w.add_file("overrides/a.bin", src)
        w.close()
        with ct.open_container(p) as r:
            self.assertEqual(r.read("overrides/a.bin"), b"hello" * 100)
            self.assertEqual(r.read("meta.json"), b"{}")

    def test_header_raw_len_cap_checked_before_decompress(self):
        self._open(raw_len=(1 << 32) - 1)

    def test_header_comp_len_cap(self):
        self._open(comp_len=(ct.MAX_HEADER_COMP + 1))

    def test_header_offset_out_of_file(self):
        self._open(header_off=1 << 30)

    def test_header_declared_len_mismatch(self):
        self._open(raw_len=999999)

    def test_block_size_limits(self):
        self._open(header={**BASE, "block_size": 1024})
        self._open(header={**BASE, "block_size": 1 << 30})

    def test_block_lengths_capped(self):
        self._open(header={**BASE, "blocks": [[1, ct.MAX_BLOCK_RAW + 1]]})
        self._open(header={**BASE, "blocks": [[ct.MAX_BLOCK_COMP + 1, 1]]})

    def test_block_beyond_file(self):
        self._open(header={**BASE, "blocks": [[1000, 10]]})

    def test_unknown_transform_rejected(self):
        self._open(header={**BASE, "blocks": [[1, 10]],
                           "entries": [{"name": "x", "transform": "zipcrypt",
                                        "chunks": [[0, 0, 1]]}]},
                   extra=b"\x00")

    def test_duplicate_entries_rejected(self):
        self._open(header={**BASE, "entries": [
            {"name": "x", "chunks": []}, {"name": "x", "chunks": []}]})

    def test_chunk_out_of_bounds(self):
        self._open(header={**BASE, "blocks": [[1, 10]],
                           "entries": [{"name": "x", "chunks": [[0, 5, 10]]}]},
                   extra=b"\x00")

    def test_chunk_block_index(self):
        self._open(header={**BASE, "entries": [
            {"name": "x", "chunks": [[3, 0, 1]]}]})

    def test_inline_missing_data(self):
        self._open(header={**BASE, "entries": [
            {"name": "x", "inline": True, "chunks": []}]})

    def test_chunks_total_must_equal_stored(self):
        self._open(header={**BASE, "blocks": [[1, 10]],
                           "entries": [{"name": "x", "stored": 9,
                                        "chunks": [[0, 0, 1]]}]},
                   extra=b"\x00")

    def test_inline_length_must_match_size(self):
        import base64
        self._open(header={**BASE, "inline": {"x": base64.b64encode(b"a").decode()},
                           "entries": [{"name": "x", "inline": True,
                                        "size": 2, "stored": 1, "chunks": []}]})

    def test_header_frame_size_precheck(self):
        # 声明 raw_len 很小、但 zstd 帧自己写了更大的 content size：
        # 必须在解压前拒绝，而不是信 max_output_size
        p = self.tmp / "big.eapack"
        hb = b"{}" + b" " * 4096
        comp = zstd.ZstdCompressor().compress(hb)
        with open(p, "wb") as f:
            f.write(ct.MAGIC)
            f.write(struct.pack(">IIII", len(ct.MAGIC) + 16,
                                len(comp), 64, 2))   # raw_len=64
            f.write(comp)
        old = ct.MAX_HEADER_RAW
        ct.MAX_HEADER_RAW = 1024
        try:
            with self.assertRaises(ct.ContainerError):
                ct.open_container(p)
        finally:
            ct.MAX_HEADER_RAW = old

    def test_block_window_limit(self):
        p = self.tmp / "win.eapack"
        params = zstd.ZstdCompressionParameters.from_level(3, window_log=30)
        data = zstd.ZstdCompressor(compression_params=params).compress(b"x" * 4096)
        header = {**BASE, "blocks": [[len(data), 4096]],
                  "entries": [{"name": "x", "stored": 4096,
                               "chunks": [[0, 0, 4096]]}]}
        hb = json.dumps(header).encode()
        hc = zstd.ZstdCompressor().compress(hb)
        with open(p, "wb") as f:
            f.write(ct.MAGIC)
            f.write(struct.pack(">IIII", len(ct.MAGIC) + 16,
                                len(hc), len(hb), 2))
            f.write(hc); f.write(data)
        old = ct.MAX_WINDOW
        ct.MAX_WINDOW = 1 << 20
        try:
            with ct.open_container(p) as r:
                with self.assertRaises(ct.ContainerError):
                    r.read("x")
        finally:
            ct.MAX_WINDOW = old

    def test_chunk_negative(self):
        self._open(header={**BASE, "blocks": [[1, 10]],
                           "entries": [{"name": "x", "chunks": [[0, -1, 5]]}]},
                   extra=b"\x00")


if __name__ == "__main__":
    unittest.main()
