"""
packwriter.py — 导出更新包时统一的"写入口"
================================================================
导出有两条路径（从启动器导出的 ZIP 重新打包、直接从本地实例打包），
它们必须写出**同样结构**的包。这里把"写容器 + 逐条算哈希 + 收尾签名"
收敛到一处，避免两边再次走偏（历史上就漏过字段）。

支持两种容器：

* **v2（默认）**：`app.core.container` 的块压缩流（zstd）。
  比 zip 小 6% 左右，解压还更快；存档区域文件会顺带做"去 zlib 外壳"
  变换（见 `app.core.mca`），玩家端解压时自动还原成原始字节。
* **zip（兼容）**：环境里没有 zstandard，或导出选项要求兼容时使用。

签名在收尾时算：用的是"写的时候顺手记下来的每个文件 sha1"，
不需要再把包读一遍（老的实现要重读整个包才能签名）。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from . import container as ct
from . import mca
from .packsign import (MANIFEST_FILE, compute_signature_v2,
                       zip_content_entries_v2)


# 平衡档：黑盒实测无存档内容用 22 档只多省 0.08%，耗时约 9 倍；
# 17 档基本拿到全部收益，保留可接受的速度。
SUPER_LEVEL = 17


def resolve_export_options(opts: dict | None = None) -> dict:
    """
    把"导出选项"归一成写包参数（两条导出路径共用）。

    - super_compress（beta）是**唯一**对外的格式开关：
      开 = 新版 EAPK2 + 存档去外壳；关 = 旧版 zip（老客户端能读）。
      环境里没有 zstandard 时 super 也会自动退回 zip。
    - compact_container / world_transform 是 0.13.0 的旧键，仅作兼容保留；
      这里不再读它们，避免老数据库里 compact_container=True 把
      默认导出悄悄变成新格式。
    """
    try:
        from .database import get_export_options
        base = dict(get_export_options())
    except Exception:                                  # noqa: BLE001
        base = {}
    base.update({k: v for k, v in (opts or {}).items() if v is not None})
    sup = bool(base.get("super_compress"))
    # 私有调参（不暴露 UI）：只在 super 打开时才有意义
    raw_level = base.get("zstd_level")
    try:
        level = int(raw_level) if raw_level else SUPER_LEVEL
    except (TypeError, ValueError):
        level = SUPER_LEVEL
    return {
        "super": sup,
        # 只有 super 才写新容器；环境缺 zstandard 时如实回退 zip，
        # 哈希清单也要按 zip 的口径写（否则旧客户端拿不到 saves 聚合哈希）。
        "use_v2": bool(sup and ct.available()),
        "level": max(1, min(22, level)) if sup else ct.DEFAULT_LEVEL,
        "mca": sup,                    # 存档变换只随 super 开启
    }


class PackWriter:
    """写一个更新包（v2 容器或 zip），并在收尾时写好签名清单。"""

    def __init__(self, path: Path, *, use_v2: bool = True,
                 level: int = ct.DEFAULT_LEVEL,
                 mca_transform: bool = True,
                 log=None):
        self.path = Path(path)
        self.use_v2 = bool(use_v2 and ct.available())
        self.mca_transform = bool(mca_transform)
        self._level = level
        self._log = log or (lambda _l, _m: None)
        self.entries: list[tuple[str, int, str]] = []
        self._closed = False
        self._wrote_file = False

        if self.use_v2:
            self._v2 = ct.ContainerWriter(self.path, level=level, log=self._log)
            self._zf = None
        else:
            self._v2 = None
            # 兼容模式：deflate，等级交给 zipfile 默认（与旧包一致）
            self._zf = zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED)

    # ------------------------------------------------------------------
    @property
    def container_label(self) -> str:
        return "zstd 块压缩" if self.use_v2 else "zip"

    def _transform_for(self, name: str) -> str | None:
        if self.mca_transform and mca.is_region_name(name):
            return "mca"
        return None

    # ------------------------------------------------------------------
    def add_bytes(self, name: str, data: bytes, *,
                  inline: bool = False) -> None:
        """
        `inline=True`：写进容器索引（适用于小元数据），导入时无需解压。
        zip 模式没有这个概念，照常作为普通成员写入。
        """
        if self._v2 is not None:
            if inline:
                self._v2.add_inline(name, data)
            else:
                self._v2.add_bytes(name, data)
        else:
            self._zf.writestr(name, data)
        self.entries.append((name, len(data), hashlib.sha1(data).hexdigest()))

    def add_file(self, name: str, src: Path) -> None:
        src = Path(src)
        transform = self._transform_for(name)
        if transform and self._v2 is not None:
            blob = mca.transform_file(src)
            if blob is not None:
                logical = src.read_bytes()
                digest = hashlib.sha1(logical).hexdigest()
                self._v2.add_transformed(name, blob,
                                         logical_size=len(logical),
                                         logical_sha1=digest)
                self.entries.append((name, len(logical), digest))
                self._wrote_file = True
                return
        if self._v2 is not None:
            self._v2.add_file(name, src)          # 流式读，不整文件进内存
            meta = self._v2.last_entry
            self.entries.append((name, meta["size"], meta["sha1"]))
        else:
            digest = hashlib.sha256()
            sha1 = hashlib.sha1()
            size = 0
            with src.open("rb") as fh, \
                    self._zf.open(name, "w") as out:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    sha1.update(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            self.entries.append((name, size, sha1.hexdigest()))
        self._wrote_file = True

    def add_dir_marker(self, name: str) -> None:
        """写一个空目录条目（zip 用它表示空目录；v2 用它做布局标记）。"""
        if self._v2 is not None:
            self._v2.add_bytes(name, b"")
        else:
            self._zf.writestr(zipfile.ZipInfo(name), b"")
        # 目录条目不参与签名（老实现里 zip 的目录条目是参与过的，
        # 但那种条目在 v2 里并不存在，为了让两种容器签名口径一致，统一排除）
        self._wrote_file = True

    # ------------------------------------------------------------------
    def finish(self, settings: dict, manifest: dict, *,
               tamper_proof: bool) -> dict:
        """写签名 + ea_manifest.json，然后关闭容器。"""
        if self._v2 is not None:
            if tamper_proof:
                manifest["signature_version"] = 2
                manifest["signature"] = compute_signature_v2(
                    settings, manifest, list(self.entries))
            body = json.dumps(manifest, ensure_ascii=False, indent=2)
            self._v2.add_bytes(MANIFEST_FILE, body.encode("utf-8"))
            stats = self._v2.close()
            self._log("info",
                      f"容器（{self.container_label}）："
                      f"{stats.files + 1} 个条目，"
                      f"{stats.compressed_bytes/1048576:.1f} MB"
                      + (f"，{stats.transformed} 个存档区域已去外壳"
                         if stats.transformed else ""))
        else:
            # 旧版兼容 zip：先关包（签名载荷不包含 manifest），
            # 用 v0.12.0 的 v2 内容口径算签名（zip infolist，含目录 marker），
            # 再把 manifest 追加进去 —— 老客户端可直接验签，且保留
            # "同 CRC 改写也能发现"的强度。
            self._zf.close()
            if tamper_proof:
                manifest["signature_version"] = 2
                entries = zip_content_entries_v2(self.path)
                manifest["signature"] = compute_signature_v2(
                    settings, manifest, entries)
            body = json.dumps(manifest, ensure_ascii=False, indent=2)
            with zipfile.ZipFile(self.path, "a", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(MANIFEST_FILE, body)
        self._closed = True
        return manifest

    def abort(self) -> None:
        try:
            if self._v2 is not None and not self._closed:
                self._v2.abort()
            elif self._zf is not None and not self._closed:
                self._zf.close()
                Path(self.path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001, S110
            pass
        self._closed = True
