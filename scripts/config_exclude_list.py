"""
config_exclude_list.py — 查看「智能剔除」归属表 / 体检一个整合包
------------------------------------------------
用法：

    python scripts/config_exclude_list.py
        列出整张归属表（哪些模组的 config 会被保护）

    python scripts/config_exclude_list.py <整合包目录>
        体检这个整合包的 config/ 与 mods/：
          - 哪些配置会被「智能剔除」保护（按模组归类）
          - 哪些配置会照常更新
          - mods/ 里装了这个表覆盖的哪些优化/渲染模组
          - 表里没有、但 config 文件名像"优化/渲染"的可疑项（人工判断）

    python scripts/config_exclude_list.py <整合包目录> --json
        同上，输出 JSON（便于脚本化）

不联网、不写任何文件。
"""

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from app.core import config_exclude as ce  # noqa: E402


def _table_text() -> str:
    lines = [f"「智能剔除」归属表：{len(ce.TABLE)} 个模组"
             f"（核对日期 {ce.LAST_CHECKED}）"]
    for kind in (ce.KIND_RENDER, ce.KIND_PERF):
        owners = [o for o in ce.TABLE if o.kind == kind]
        lines.append("")
        lines.append(f"── {kind}（{len(owners)}）" + "─" * 30)
        for o in owners:
            extra = f"  别名：{', '.join(o.aliases)}" if o.aliases else ""
            note = f"  # {o.note}" if o.note else ""
            lines.append(f"  {o.key:<26} {o.name}{extra}{note}")
    return "\n".join(lines)


def _iter_config_files(pack: Path) -> list[str]:
    base = pack / "config"
    if not base.is_dir():
        return []
    out: list[str] = []
    for p in base.rglob("*"):
        try:
            if p.is_file():
                out.append(p.relative_to(pack).as_posix())
        except (OSError, ValueError):
            continue
    return sorted(out)


def _audit(pack: Path) -> dict:
    files = _iter_config_files(pack)
    protected: dict[str, list[str]] = {}
    plain: list[str] = []
    for rel in files:
        owner = ce.owner_of(rel)
        if owner is None:
            plain.append(rel)
        else:
            protected.setdefault(owner.name, []).append(rel)

    mods_dir = pack / "mods"
    hit_mods: list[str] = []
    if mods_dir.is_dir():
        for jar in sorted(mods_dir.glob("*.jar")):
            name = ce.normalize(jar.stem)
            for owner in ce.TABLE:
                ids = [ce.normalize(owner.key)] + [ce.normalize(a)
                                                   for a in owner.aliases]
                if any(name.startswith(i) for i in ids):
                    hit_mods.append(f"{owner.name}  ←  {jar.name}")
                    break

    return {
        "pack": str(pack),
        "config_files": len(files),
        "protected_files": sum(len(v) for v in protected.values()),
        "protected": protected,
        "plain": plain,
        "installed": hit_mods,
    }


def _audit_text(data: dict) -> str:
    lines = [f"整合包：{data['pack']}",
             f"config 文件 {data['config_files']} 个，其中 "
             f"{data['protected_files']} 个会被「智能剔除」保护"]
    lines.append("")
    lines.append("── 会被保护的配置 " + "─" * 30)
    if not data["protected"]:
        lines.append("  （没有）")
    for name, rels in sorted(data["protected"].items()):
        lines.append(f"  {name}：{len(rels)} 个")
        for rel in rels[:4]:
            lines.append(f"      {rel}")
        if len(rels) > 4:
            lines.append(f"      …… 另有 {len(rels) - 4} 个")
    lines.append("")
    lines.append("── 照常更新的配置 " + "─" * 30)
    if not data["plain"]:
        lines.append("  （没有）")
    for rel in data["plain"][:15]:
        lines.append(f"  {rel}")
    if len(data["plain"]) > 15:
        lines.append(f"  …… 另有 {len(data['plain']) - 15} 个")
    lines.append("")
    lines.append("── mods/ 里装到的表内模组 " + "─" * 20)
    if not data["installed"]:
        lines.append("  （没有认出来；jar 名可能不带模组 id）")
    for line in data["installed"]:
        lines.append(f"  {line}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    as_json = "--json" in argv

    if not args:
        print(_table_text())
        return 0

    pack = Path(args[0]).expanduser()
    if not pack.is_dir():
        print(f"不是目录：{pack}", file=sys.stderr)
        return 2
    data = _audit(pack)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_audit_text(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
