"""给 e2e 造夹具：玩家整合包 + 更新包，打印 JSON 路径。

夹具刻意做小但"什么都有"：
  mods/old.jar            本地旧版 → 应被替换
  mods/gone.jar           本地多出来的 → 应被删除（mods 在白名单里）
  config/mine.toml        玩家自己的配置 → 不能动
  config/sodium-options.json  智能剔除保护 → 不能动
  resourcepacks/keep.zip  两边一致 → 出现在「无需更新」折叠区
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import database as db               # noqa: E402
from app.core import eapack as eapack_mod         # noqa: E402
from app.core.mrpack import merge_files, parse_mrpack  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(out_dir: Path) -> dict:
    root = Path(out_dir)
    dbdir = root / "db"
    dbdir.mkdir(parents=True, exist_ok=True)
    if not (dbdir / "pulses-easier.db").exists():
        db.create_database(dbdir)
    db.set_db_path(dbdir)

    pack = root / "versions" / "E2EPack"
    (pack / "mods").mkdir(parents=True, exist_ok=True)
    (pack / "config").mkdir(parents=True, exist_ok=True)
    (pack / "resourcepacks").mkdir(parents=True, exist_ok=True)
    _write(pack / "mods" / "old.jar", "OLD")
    _write(pack / "mods" / "gone.jar", "LOCAL")
    _write(pack / "config" / "mine.toml", "mine = 1\n")
    _write(pack / "config" / "sodium-options.json", '{"render_distance": 8}\n')
    # 两边一模一样的资源包：用来验证「无需更新」折叠区
    (pack / "resourcepacks" / "keep.zip").write_bytes(b"PK-KEEP")

    src = root / "src"
    _write(src / "modrinth.index.json", json.dumps({
        "formatVersion": 1, "game": "minecraft", "versionId": "3.0.0",
        "name": "E2EPack", "files": [],
        "dependencies": {"minecraft": "1.20.1"},
    }))
    _write(src / "overrides" / "mods" / "old.jar", "NEW")
    _write(src / "overrides" / "mods" / "added.jar", "ADDED")
    _write(src / "overrides" / "config" / "sodium-options.json",
           '{"render_distance": 16}\n')
    (src / "overrides" / "resourcepacks").mkdir(parents=True, exist_ok=True)
    (src / "overrides" / "resourcepacks" / "keep.zip").write_bytes(b"PK-KEEP")

    src_zip = root / "E2EPack.zip"
    with zipfile.ZipFile(src_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())

    parsed, err = parse_mrpack(src_zip)
    assert parsed is not None, err
    update = root / "E2EPack-3.0.0.eapack"
    assert eapack_mod.export_eapack(
        source_dir=src, source_zip=src_zip, out_path=update,
        pack=parsed, merged_files=merge_files(parsed),
        strategies={"mods": "完全匹配"}, changelog_md="# 3.0.0\n\n- e2e\n")
    return {"kind": "basic", "pack": str(pack), "update": str(update),
            "zip": str(src_zip), "db": str(dbdir)}


def main(out_dir: str) -> int:
    root = Path(out_dir)
    info = build(root)
    (root / "fixture.json").write_text(json.dumps(info), encoding="utf-8")
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
