"""
make_download_fixture.py — "必须真下载"的端到端夹具
------------------------------------------------
基本夹具（make_fixture.py）的文件都在 `overrides/` 里，`update.apply` 走的是
"复制"，下载引擎根本不会被调用。这个夹具专门喂下载引擎：

  <dir>/downloads/*.jar      真的被 HTTP 拉取的文件（file_server.py 提供）
  更新包 A：modrinth.index.json 的 files[] 里放 3 个 http URL
            （其中一个 5MiB，超过 4MiB 分片阈值 → 走文件内分片）
  更新包 B：一个 8MiB 的大文件，只用来测"下载中点中止 → 继续"

文件内容用固定种子伪随机字节生成：不可压缩（模拟真实 jar），但每次跑出来
一样，方便断点/续传后按 sha256 复核。
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import database as db               # noqa: E402
from app.core import eapack as eapack_mod         # noqa: E402
from app.core.mrpack import merge_files, parse_mrpack  # noqa: E402

MIB = 1024 * 1024


def _blob(name: str, size: int) -> bytes:
    """固定种子 → 不可压缩字节（同一个 name 每次生成都一样）。"""
    rnd = random.Random(f"pulses-e2e:{name}")
    block = bytes(rnd.getrandbits(8) for _ in range(64 * 1024))
    return (block * (size // len(block) + 1))[:size]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sha(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _pack_dir(root: Path, name: str) -> Path:
    pack = root / "versions" / name
    (pack / "mods").mkdir(parents=True, exist_ok=True)
    (pack / "config").mkdir(parents=True, exist_ok=True)
    _write(pack / "mods" / "local-only.jar", "LOCAL")
    _write(pack / "config" / "player.toml", "player = 1\n")
    return pack


def _index(version: str, entries: list[dict]) -> dict:
    return {
        "formatVersion": 1, "game": "minecraft", "versionId": version,
        "name": "E2EPackDL", "files": entries,
        "dependencies": {"minecraft": "1.20.1"},
    }


def _build_update(root: Path, tag: str, version: str,
                  entries: list[dict], base: str,
                  override_files: dict[str, str]) -> Path:
    src = root / f"src-{tag}"
    _write(src / "modrinth.index.json", json.dumps(_index(version, entries)))
    for rel, text in override_files.items():
        _write(src / "overrides" / rel, text)
    src_zip = root / f"E2EPackDL-{tag}.zip"
    with zipfile.ZipFile(src_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    parsed, err = parse_mrpack(src_zip)
    assert parsed is not None, err
    out = root / f"E2EPackDL-{tag}.eapack"
    assert eapack_mod.export_eapack(
        source_dir=src, source_zip=src_zip, out_path=out,
        pack=parsed, merged_files=merge_files(parsed),
        strategies={"mods": "完全匹配"},
        changelog_md=f"# {version}\n\n- 真下载夹具（{tag}）\n")
    return out


def build(out_dir: Path, port: int) -> dict:
    root = Path(out_dir)
    dbdir = root / "db"
    dbdir.mkdir(parents=True, exist_ok=True)
    if not (dbdir / "pulses-easier.db").exists():
        db.create_database(dbdir)
    db.set_db_path(dbdir)

    served = root / "downloads"
    served.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{port}"

    # 3 个"要下载"的文件：小、中、大（大文件触发文件内分片）
    plan = [("e2e-a.jar", 64 * 1024),
            ("e2e-b.jar", 256 * 1024),
            ("e2e-big.jar", 5 * MIB)]
    files = []
    entries = []
    for name, size in plan:
        path = served / name
        path.write_bytes(_blob(name, size))
        sha1, sha256 = _sha(path, "sha1"), _sha(path, "sha256")
        entries.append({
            "path": f"mods/{name}",
            "downloads": [f"{base}/{name}"],
            "fileSize": size,
            "hashes": {"sha1": sha1, "sha256": sha256},
        })
        files.append({"name": name, "path": f"mods/{name}", "size": size,
                      "sha1": sha1, "sha256": sha256})

    pack = _pack_dir(root, "E2EPackDL")
    update = _build_update(root, "a", "3.1.0", entries, base,
                           {"mods/e2e-inpack.jar": "INPACK",
                            "config/e2e-dl.toml": "e2e = 1\n"})

    # 中止/续传用：一个大文件，单独一个包（避免命中上一轮的缓存）
    abort_name = "e2e-abort-big.jar"
    abort_path = served / abort_name
    abort_path.write_bytes(_blob(abort_name, 8 * MIB))
    abort_sha1 = _sha(abort_path, "sha1")
    abort_sha256 = _sha(abort_path, "sha256")
    abort_entries = [{
        "path": f"mods/{abort_name}",
        "downloads": [f"{base}/{abort_name}"],
        "fileSize": abort_path.stat().st_size,
        "hashes": {"sha1": abort_sha1, "sha256": abort_sha256},
    }]
    abort_pack = _pack_dir(root, "E2EPackDL2")
    abort_update = _build_update(root, "b", "3.1.1", abort_entries, base,
                                 {"mods/e2e-abort-inpack.jar": "INPACK2"})

    return {
        "kind": "download",
        "pack": str(pack), "update": str(update),
        "abortPack": str(abort_pack), "abortUpdate": str(abort_update),
        "serverDir": str(served), "baseUrl": base,
        "files": files,
        "abortFile": {"name": abort_name, "path": f"mods/{abort_name}",
                      "size": abort_path.stat().st_size,
                      "sha1": abort_sha1, "sha256": abort_sha256},
        "db": str(dbdir),
    }


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：make_download_fixture.py <dir> --port <port>",
              file=sys.stderr)
        return 2
    out_dir = Path(argv[0])
    port = 8232
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    info = build(out_dir, port)
    (out_dir / "fixture.json").write_text(json.dumps(info), encoding="utf-8")
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
