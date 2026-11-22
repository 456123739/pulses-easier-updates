"""
packsign.py — 更新包完整性签名的算法与口径
================================================================
单独一个模块，避免 `eapack` 与 `packwriter` 互相 import。

签名不是密钥 MAC，挡不住"有权重打包的人"伪造；它的用途是**发现包在
传输/编辑后被改坏或被第三方改过**，比"写个没人校验的签名"有意义。

两代口径：

* v1（0.10.x 及更早）：签名载荷里是 (文件名, 解压后大小, CRC32)。
  CRC 能被"构造同 CRC 的不同内容"绕过，所以只用于校验老包。
* v2（当前）：载荷里是 (文件名, 大小, 内容 sha1)。写着写着顺手就能算出来，
  不需要把包读第二遍。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_FILE = "ea_manifest.json"


def signature_payload(settings: dict, manifest: dict,
                      entries: list[tuple[str, int, int]]) -> str:
    return json.dumps({
        "settings": settings,
        "manifest": {k: v for k, v in manifest.items() if k != "signature"},
        "entries": [[n, int(s), int(c)] for n, s, c in entries],
    }, ensure_ascii=False, sort_keys=True)


def compute_signature(settings: dict, manifest: dict,
                      entries: list[tuple[str, int, int]]) -> str:
    payload = signature_payload(settings, manifest, entries)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def payload_v2(settings: dict, manifest: dict,
               entries: list[tuple[str, int, str]]) -> str:
    return json.dumps({
        "settings": settings,
        "manifest": {k: v for k, v in manifest.items() if k != "signature"},
        "entries": [[n, int(s), h] for n, s, h in entries],
        "scheme": "sha1-content",
    }, ensure_ascii=False, sort_keys=True)


def compute_signature_v2(settings: dict, manifest: dict,
                         entries: list[tuple[str, int, str]]) -> str:
    return hashlib.sha256(payload_v2(settings, manifest, entries)
                          .encode("utf-8")).hexdigest()


def content_entries(path: Path) -> list[tuple[str, int, str]]:
    """逐条读包内文件算内容 sha1（v2 校验口径，含存档还原后的逻辑内容）。"""
    from . import container as ct

    out: list[tuple[str, int, str]] = []
    with ct.open_container(path) as reader:
        for name in reader.names:
            if name == MANIFEST_FILE or name.endswith("/"):
                continue
            info = reader.entries.get(name) or {}
            if info.get("transform"):
                data = reader.read(name)          # 逻辑内容
                out.append((name, len(data),
                            hashlib.sha1(data).hexdigest()))
                continue
            digest = hashlib.sha1()
            size = 0
            for chunk in reader.iter_chunks(name):
                digest.update(chunk)
                size += len(chunk)
            out.append((name, size, digest.hexdigest()))
    return out
