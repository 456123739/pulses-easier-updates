"""给 e2e 造夹具：玩家整合包 + 更新包，打印 JSON 路径。"""
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


def main(out_dir: str) -> int:
    root = Path(out_dir)
    dbdir = root / "db"
    dbdir.mkdir(parents=True, exist_ok=True)
    if not (dbdir / "pulses-easier.db").exists():
        db.create_database(dbdir)
    db.set_db_path(dbdir)

    pack = root / "versions" / "E2EPack"
    (pack / "mods").mkdir(parents=True, exist_ok=True)
    (pack / "config").mkdir(parents=True, exist_ok=True)
    (pack / "mods" / "old.jar").write_text("OLD", encoding="utf-8")
    (pack / "mods" / "gone.jar").write_text("LOCAL", encoding="utf-8")
    (pack / "config" / "mine.toml").write_text("mine = 1\n", encoding="utf-8")
    (pack / "config" / "sodium-options.json").write_text(
        '{"render_distance": 8}\n', encoding="utf-8")

    src = root / "src"
    (src / "overrides" / "mods").mkdir(parents=True, exist_ok=True)
    (src / "overrides" / "config").mkdir(parents=True, exist_ok=True)
    (src / "modrinth.index.json").write_text(json.dumps({
        "formatVersion": 1, "game": "minecraft", "versionId": "3.0.0",
        "name": "E2EPack", "files": [],
        "dependencies": {"minecraft": "1.20.1"},
    }), encoding="utf-8")
    (src / "overrides" / "mods" / "old.jar").write_text("NEW", encoding="utf-8")
    (src / "overrides" / "mods" / "added.jar").write_text("ADDED", encoding="utf-8")
    (src / "overrides" / "config" / "sodium-options.json").write_text(
        '{"render_distance": 16}\n', encoding="utf-8")

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
    print(json.dumps({"pack": str(pack), "update": str(update), "db": str(dbdir)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
